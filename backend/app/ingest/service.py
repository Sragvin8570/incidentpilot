from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AlertOccurrence, Incident, IncidentState
from app.enrichment.service import (
    EnrichmentResult,
    ResourceResolver,
    WorkloadTarget,
)
from app.incidents.state_machine import (
    record_incident_creation,
    transition_incident,
)

TARGET_LABELS = {
    "pod": "Pod",
    "deployment": "Deployment",
    "statefulset": "StatefulSet",
    "pvc": "PersistentVolumeClaim",
    "persistentvolumeclaim": "PersistentVolumeClaim",
}


@dataclass(frozen=True)
class TargetLabels:
    target: WorkloadTarget | None
    reason: str | None = None


def parse_target_labels(labels: dict[str, str]) -> TargetLabels:
    named_targets = [
        (label, value.strip())
        for label, value in labels.items()
        if label.lower() in TARGET_LABELS and value.strip()
    ]
    explicit_kind = labels.get("target_kind", "").strip()
    explicit_name = labels.get("target_name", "").strip()

    if explicit_kind or explicit_name:
        if not explicit_kind or not explicit_name:
            return TargetLabels(None, "incomplete_target_labels")
        normalized_kind = TARGET_LABELS.get(explicit_kind.lower())
        if normalized_kind is None:
            return TargetLabels(None, "unsupported_target_kind")
        if any(
            TARGET_LABELS[label.lower()] != normalized_kind or value != explicit_name
            for label, value in named_targets
        ):
            return TargetLabels(None, "conflicting_target_labels")
        return TargetLabels(WorkloadTarget(normalized_kind, explicit_name))

    if len(named_targets) != 1:
        reason = "missing_target_labels" if not named_targets else "conflicting_target_labels"
        return TargetLabels(None, reason)

    label, name = named_targets[0]
    return TargetLabels(WorkloadTarget(TARGET_LABELS[label.lower()], name))


def derive_priority(severity: str | None, environment: str) -> str:
    normalized_severity = (severity or "").strip().lower()
    production = environment == "production"
    if normalized_severity == "critical":
        return "P1" if production else "P2"
    if normalized_severity == "high":
        return "P2" if production else "P3"
    if normalized_severity in {"warning", "warn", "medium"}:
        return "P3" if production else "P4"
    return "P4"


async def process_alert(
    session: AsyncSession,
    resolver: ResourceResolver,
    *,
    status: str,
    fingerprint: str,
    labels: dict[str, str],
    payload: dict,
) -> tuple[Incident | None, str]:
    if status == "resolved":
        previous = await session.scalar(
            select(Incident)
            .where(Incident.fingerprint == fingerprint)
            .order_by(Incident.created_at.desc())
            .limit(1)
        )
        session.add(
            AlertOccurrence(
                incident_id=previous.id if previous else None,
                status="resolved",
                payload_json=payload,
            )
        )
        await session.commit()
        return previous, "resolved_recorded"

    existing = await session.scalar(
        select(Incident).where(
            Incident.fingerprint == fingerprint,
            Incident.state != IncidentState.RESOLVED,
        )
    )
    if existing is not None:
        session.add(
            AlertOccurrence(
                incident_id=existing.id,
                status="firing",
                payload_json=payload,
            )
        )
        await session.commit()
        return existing, "deduplicated"
    await session.rollback()

    namespace = labels.get("namespace", "").strip()
    target_labels = parse_target_labels(labels)
    enrichment: EnrichmentResult | None = None
    unscoped_reason: str | None = None
    if not namespace:
        unscoped_reason = "missing_namespace"
    elif target_labels.target is None:
        unscoped_reason = target_labels.reason
    else:
        enrichment = await resolver.resolve(
            namespace,
            target_labels.target.kind,
            target_labels.target.name,
        )
        if enrichment.target is None:
            unscoped_reason = "target_not_found"

    environment = enrichment.environment if enrichment else None
    target = enrichment.target if enrichment else None
    severity = labels.get("severity")
    incident = Incident(
        fingerprint=fingerprint,
        alert_name=labels["alertname"].strip(),
        severity=severity,
        namespace=namespace or None,
        environment=environment,
        priority=derive_priority(severity, environment) if environment else None,
        target_kind=target.kind if target else None,
        target_name=target.name if target else None,
        state=IncidentState.RECEIVED,
    )
    session.add(incident)
    await session.flush()
    await record_incident_creation(
        session,
        incident,
        detail={"alert_fingerprint": fingerprint},
    )
    if unscoped_reason:
        await transition_incident(
            session,
            incident,
            IncidentState.UNSCOPED,
            detail={"reason": unscoped_reason},
        )
    else:
        await transition_incident(
            session,
            incident,
            IncidentState.ENRICHED,
            detail={"environment": environment, "priority": incident.priority},
        )
    session.add(
        AlertOccurrence(
            incident_id=incident.id,
            status="firing",
            payload_json=payload,
        )
    )
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        constraint_name = getattr(
            getattr(error.orig, "diag", None), "constraint_name", None
        )
        duplicate_fingerprint = (
            constraint_name == "uq_incidents_open_fingerprint"
            or "UNIQUE constraint failed: incidents.fingerprint" in str(error.orig)
        )
        if not duplicate_fingerprint:
            raise
        existing = await session.scalar(
            select(Incident).where(
                Incident.fingerprint == fingerprint,
                Incident.state != IncidentState.RESOLVED,
            )
        )
        if existing is None:
            raise
        session.add(
            AlertOccurrence(
                incident_id=existing.id,
                status="firing",
                payload_json=payload,
            )
        )
        await session.commit()
        return existing, "deduplicated"
    return incident, "unscoped" if unscoped_reason else "created"
