import asyncio
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AlertOccurrence, AuditEvent, Incident, IncidentState
from app.enrichment.service import EnrichmentResult, WorkloadTarget
from app.ingest.service import (
    derive_priority,
    parse_target_labels,
    process_alert,
)


class FakeResolver:
    def __init__(self, result: EnrichmentResult | None = None) -> None:
        self.result = result or EnrichmentResult(
            "dev", WorkloadTarget("Deployment", "checkout")
        )
        self.calls: list[tuple[str, str, str]] = []

    async def resolve(self, namespace: str, kind: str, name: str) -> EnrichmentResult:
        self.calls.append((namespace, kind, name))
        return self.result


def run_scenario(scenario) -> None:
    async def run() -> None:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await scenario(session)
        finally:
            await engine.dispose()

    asyncio.run(run())


def alert_labels(**overrides: str) -> dict[str, str]:
    return {
        "alertname": "PodCrashLooping",
        "severity": "critical",
        "namespace": "dev",
        "pod": "checkout-abc123",
        **overrides,
    }


def test_firing_alert_is_enriched_and_audited() -> None:
    async def scenario(session) -> None:
        resolver = FakeResolver()
        incident, outcome = await process_alert(
            session,
            resolver,
            status="firing",
            fingerprint="fingerprint-1",
            labels=alert_labels(),
            payload={"labels": alert_labels()},
        )

        assert incident is not None
        assert incident.state == IncidentState.ENRICHED
        assert incident.environment == "dev"
        assert incident.priority == "P2"
        assert incident.target_kind == "Deployment"
        assert incident.target_name == "checkout"
        assert outcome == "created"
        assert resolver.calls == [("dev", "Pod", "checkout-abc123")]

        audit = (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.incident_id == incident.id)
                .order_by(AuditEvent.id)
            )
        ).all()
        assert [(item.from_state, item.to_state) for item in audit] == [
            (None, "RECEIVED"),
            ("RECEIVED", "ENRICHED"),
        ]
        assert await session.scalar(select(func.count()).select_from(AlertOccurrence)) == 1

    run_scenario(scenario)


def test_repeated_firing_alert_appends_occurrence_to_open_incident() -> None:
    async def scenario(session) -> None:
        resolver = FakeResolver()
        labels = alert_labels()
        first, first_outcome = await process_alert(
            session,
            resolver,
            status="firing",
            fingerprint="fingerprint-2",
            labels=labels,
            payload={"occurrence": 1},
        )
        second, second_outcome = await process_alert(
            session,
            resolver,
            status="firing",
            fingerprint="fingerprint-2",
            labels=labels,
            payload={"occurrence": 2},
        )

        assert first is not None and second is not None
        assert first.id == second.id
        assert first_outcome == "created"
        assert second_outcome == "deduplicated"
        assert resolver.calls == [("dev", "Pod", "checkout-abc123")]
        assert await session.scalar(select(func.count()).select_from(Incident)) == 1
        assert await session.scalar(select(func.count()).select_from(AlertOccurrence)) == 2

    run_scenario(scenario)


def test_missing_target_is_recorded_unscoped_without_cluster_lookup() -> None:
    async def scenario(session) -> None:
        resolver = FakeResolver()
        incident, outcome = await process_alert(
            session,
            resolver,
            status="firing",
            fingerprint="fingerprint-3",
            labels={"alertname": "UnscopedAlert", "namespace": "dev"},
            payload={},
        )

        assert incident is not None
        assert incident.state == IncidentState.UNSCOPED
        assert incident.target_kind is None
        assert incident.environment is None
        assert incident.priority is None
        assert outcome == "unscoped"
        assert resolver.calls == []
        last_event = await session.scalar(
            select(AuditEvent)
            .where(AuditEvent.incident_id == incident.id)
            .order_by(AuditEvent.id.desc())
        )
        assert last_event is not None
        assert last_event.detail_json == {"reason": "missing_target_labels"}

    run_scenario(scenario)


def test_unlabeled_namespace_target_defaults_to_production() -> None:
    async def scenario(session) -> None:
        resolver = FakeResolver(
            EnrichmentResult("production", WorkloadTarget("StatefulSet", "ledger"))
        )
        incident, _ = await process_alert(
            session,
            resolver,
            status="firing",
            fingerprint="fingerprint-4",
            labels=alert_labels(namespace="prod", statefulset="ledger", pod=""),
            payload={},
        )

        assert incident is not None
        assert incident.environment == "production"
        assert incident.priority == "P1"
        assert incident.target_kind == "StatefulSet"

    run_scenario(scenario)


def test_resolved_alert_is_recorded_and_linked_without_closing_incident() -> None:
    async def scenario(session) -> None:
        incident = Incident(
            id=uuid.uuid4(),
            fingerprint="fingerprint-5",
            alert_name="PodCrashLooping",
            state=IncidentState.ENRICHED,
        )
        session.add(incident)
        await session.commit()
        resolver = FakeResolver()

        matched, outcome = await process_alert(
            session,
            resolver,
            status="resolved",
            fingerprint="fingerprint-5",
            labels=alert_labels(),
            payload={"status": "resolved"},
        )

        assert matched is not None and matched.id == incident.id
        assert matched.state == IncidentState.ENRICHED
        assert outcome == "resolved_recorded"
        occurrence = await session.scalar(select(AlertOccurrence))
        assert occurrence is not None
        assert occurrence.incident_id == incident.id
        assert occurrence.status == "resolved"
        assert resolver.calls == []

    run_scenario(scenario)


def test_priority_defaults_are_conservative() -> None:
    assert derive_priority("critical", "production") == "P1"
    assert derive_priority("critical", "dev") == "P2"
    assert derive_priority("warning", "production") == "P3"
    assert derive_priority(None, "dev") == "P4"


def test_conflicting_target_labels_are_rejected() -> None:
    result = parse_target_labels(
        {
            "target_kind": "Deployment",
            "target_name": "checkout",
            "pod": "different-pod",
        }
    )

    assert result.target is None
    assert result.reason == "conflicting_target_labels"


def test_resolved_fingerprint_can_start_a_new_incident() -> None:
    async def scenario(session) -> None:
        resolver = FakeResolver()
        first, _ = await process_alert(
            session,
            resolver,
            status="firing",
            fingerprint="fingerprint-6",
            labels=alert_labels(),
            payload={"occurrence": 1},
        )
        assert first is not None
        first_id = first.id
        first.state = IncidentState.RESOLVED
        await session.commit()

        second, outcome = await process_alert(
            session,
            resolver,
            status="firing",
            fingerprint="fingerprint-6",
            labels=alert_labels(),
            payload={"occurrence": 2},
        )

        assert second is not None
        assert second.id != first_id
        assert second.state == IncidentState.ENRICHED
        assert outcome == "created"

    run_scenario(scenario)
