from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.auth.security import get_current_user
from app.db.models import (
    Evidence,
    Decision,
    Incident,
    IncidentState,
    User,
)
from app.incidents.schemas import (
    AlertOccurrenceDetail,
    AuditEventDetail,
    DecisionDetail,
    EvidenceItem,
    ExecutionDetail,
    IncidentDetailResponse,
    IncidentListItem,
    IncidentListResponse,
    IncidentStatsResponse,
    VerificationDetail,
)

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


@router.get("", response_model=IncidentListResponse)
async def list_incidents(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    state: IncidentState | None = None,
    severity: str | None = None,
    namespace: str | None = None,
    environment: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> IncidentListResponse:
    del user
    async with request.app.state.session_factory() as session:
        query = select(Incident, Decision).outerjoin(
            Decision, Decision.incident_id == Incident.id
        )
        count_query = select(func.count(Incident.id))

        if state is not None:
            query = query.where(Incident.state == state)
            count_query = count_query.where(Incident.state == state)

        if severity is not None:
            query = query.where(Incident.severity == severity)
            count_query = count_query.where(Incident.severity == severity)

        if namespace is not None:
            query = query.where(Incident.namespace == namespace)
            count_query = count_query.where(Incident.namespace == namespace)

        if environment is not None:
            query = query.where(Incident.environment == environment)
            count_query = count_query.where(Incident.environment == environment)

        if search is not None and search.strip():
            term = f"%{search.strip()}%"
            filter_expr = Incident.alert_name.ilike(term) | Incident.target_name.ilike(term)
            query = query.where(filter_expr)
            count_query = count_query.where(filter_expr)

        total = await session.scalar(count_query) or 0

        query = query.order_by(Incident.created_at.desc()).offset(offset).limit(limit)
        rows = (await session.execute(query)).all()

        items = [
            IncidentListItem(
                id=incident.id,
                fingerprint=incident.fingerprint,
                alert_name=incident.alert_name,
                severity=incident.severity,
                namespace=incident.namespace,
                environment=incident.environment,
                priority=incident.priority,
                target_kind=incident.target_kind,
                target_name=incident.target_name,
                state=incident.state.value if hasattr(incident.state, "value") else str(incident.state),
                created_at=incident.created_at,
                resolved_at=incident.resolved_at,
                has_rca=incident.rca_json is not None,
                recommended_action=decision.recommended_action if decision else None,
                confidence=decision.confidence if decision else None,
                risk_score=decision.risk_score if decision else None,
            )
            for incident, decision in rows
        ]

        return IncidentListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/stats", response_model=IncidentStatsResponse)
async def get_incident_stats(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> IncidentStatsResponse:
    del user
    async with request.app.state.session_factory() as session:
        # Group count by state
        rows = (
            await session.execute(
                select(Incident.state, func.count(Incident.id)).group_by(Incident.state)
            )
        ).all()

        counts = {
            state.value if hasattr(state, "value") else str(state): count
            for state, count in rows
        }

        total = sum(counts.values())
        terminal_states = {"RESOLVED", "UNSCOPED", "ESCALATED"}
        active = sum(
            count for state_name, count in counts.items() if state_name not in terminal_states
        )

        return IncidentStatsResponse(
            total=total,
            active=active,
            awaiting_approval=counts.get("AWAITING_APPROVAL", 0),
            executing=counts.get("EXECUTING", 0),
            verifying=counts.get("VERIFYING", 0),
            resolved=counts.get("RESOLVED", 0),
            escalated=counts.get("ESCALATED", 0),
            failed=counts.get("FAILED", 0),
        )


@router.get("/{incident_id}", response_model=IncidentDetailResponse)
async def get_incident_detail(
    incident_id: UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> IncidentDetailResponse:
    del user
    async with request.app.state.session_factory() as session:
        stmt = (
            select(Incident)
            .options(
                selectinload(Incident.decision),
                selectinload(Incident.execution),
                selectinload(Incident.verification),
                selectinload(Incident.occurrences),
                selectinload(Incident.audit_events),
            )
            .where(Incident.id == incident_id)
        )
        incident = await session.scalar(stmt)
        if incident is None:
            raise HTTPException(status_code=404, detail="Incident not found")

        # Fetch evidence for this incident
        evidence_rows = (
            await session.scalars(
                select(Evidence)
                .where(Evidence.incident_id == incident.id)
                .order_by(Evidence.created_at.asc())
            )
        ).all()

        decision_data = None
        if incident.decision:
            d = incident.decision
            decision_data = DecisionDetail(
                id=d.id,
                recommended_action=d.recommended_action,
                outcome=d.outcome,
                confidence=d.confidence,
                llm_confidence=d.llm_confidence,
                grounding_score=d.grounding_score,
                claim_grounding=d.claim_grounding or [],
                runbook_score=d.runbook_score,
                risk_score=d.risk_score,
                risk_reason=d.risk_reason,
                reason=d.reason,
                created_at=d.created_at,
            )

        execution_data = None
        if incident.execution:
            e = incident.execution
            execution_data = ExecutionDetail(
                id=e.id,
                action_id=e.action_id,
                status=e.status,
                job_name=e.job_name,
                target_snapshot=e.target_snapshot,
                snapshot_hash=e.snapshot_hash,
                applied_spec_hash=e.applied_spec_hash,
                patch_json=e.patch_json,
                logs=e.logs or "",
                error=e.error,
                started_at=e.started_at,
                completed_at=e.completed_at,
            )

        verification_data = None
        if incident.verification:
            v = incident.verification
            verification_data = VerificationDetail(
                id=v.id,
                status=v.status,
                checks=v.checks or [],
                effectiveness_score=v.effectiveness_score,
                resolution_summary=v.resolution_summary or "",
                started_at=v.started_at,
                completed_at=v.completed_at,
            )

        evidence_items = [
            EvidenceItem(
                id=ev.id,
                tool_name=ev.tool_name,
                args_json=ev.args_json or {},
                result_text=ev.result_text,
                error=ev.error,
                created_at=ev.created_at,
            )
            for ev in evidence_rows
        ]

        audit_items = [
            AuditEventDetail(
                id=ae.id,
                from_state=ae.from_state,
                to_state=ae.to_state,
                actor=ae.actor,
                detail_json=ae.detail_json or {},
                created_at=ae.created_at,
            )
            for ae in sorted(incident.audit_events, key=lambda a: a.created_at)
        ]

        occurrence_items = [
            AlertOccurrenceDetail(
                id=occ.id,
                status=occ.status,
                payload_json=occ.payload_json or {},
                received_at=occ.received_at,
            )
            for occ in sorted(incident.occurrences, key=lambda o: o.received_at)
        ]

        return IncidentDetailResponse(
            id=incident.id,
            fingerprint=incident.fingerprint,
            alert_name=incident.alert_name,
            severity=incident.severity,
            namespace=incident.namespace,
            environment=incident.environment,
            priority=incident.priority,
            target_kind=incident.target_kind,
            target_name=incident.target_name,
            state=incident.state.value if hasattr(incident.state, "value") else str(incident.state),
            created_at=incident.created_at,
            resolved_at=incident.resolved_at,
            rca_json=incident.rca_json,
            decision=decision_data,
            execution=execution_data,
            verification=verification_data,
            evidence=evidence_items,
            audit_events=audit_items,
            occurrences=occurrence_items,
        )
