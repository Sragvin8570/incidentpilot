from datetime import datetime, timedelta, timezone
from uuid import UUID

from openai import AsyncOpenAI, OpenAIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.actions import ActionCatalog
from app.agent.rca import RCAValidationError, generate_rca
from app.agent.runner import AgentRunResult, AgentRunner
from app.agent.tools.base import ToolRegistry
from app.config import Settings
from app.db.models import (
    AlertOccurrence,
    AuditEvent,
    Evidence,
    Incident,
    IncidentState,
)
from app.incidents.state_machine import transition_incident


class AgentConfigurationError(RuntimeError):
    pass


async def run_investigation(
    incident_id: UUID,
    runner: AgentRunner,
    tools: ToolRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> AgentRunResult | None:
    async with session_factory() as session:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .with_for_update()
        )
        if incident is None:
            raise LookupError(f"Incident '{incident_id}' does not exist")
        if incident.state != IncidentState.ENRICHED:
            return None

        occurrence = await session.scalar(
            select(AlertOccurrence)
            .where(
                AlertOccurrence.incident_id == incident_id,
                AlertOccurrence.status == "firing",
            )
            .order_by(AlertOccurrence.received_at)
            .limit(1)
        )
        context = {
            "incident_id": str(incident.id),
            "alert_name": incident.alert_name,
            "severity": incident.severity,
            "namespace": incident.namespace,
            "environment": incident.environment,
            "priority": incident.priority,
            "target": {
                "kind": incident.target_kind,
                "name": incident.target_name,
            },
            "alert": occurrence.payload_json if occurrence else {},
        }
        await transition_incident(
            session,
            incident,
            IncidentState.INVESTIGATING,
            detail={"stage": "read_only_evidence_collection"},
        )
        await session.commit()

    result = await runner.run(incident_id, context, tools)
    if result.status in {"completed", "escalated"}:
        rca = None
        action_error = None
        if result.status == "completed":
            try:
                async with session_factory() as session:
                    evidence = (
                        await session.scalars(
                            select(Evidence)
                            .where(Evidence.incident_id == incident_id)
                            .order_by(Evidence.created_at)
                        )
                    ).all()
                catalog = ActionCatalog.load()
                rca = await generate_rca(
                    runner.client,
                    model=runner.model,
                    incident_context=context,
                    evidence=evidence,
                    catalog=catalog,
                    deadline=result.deadline,
                    rate_limit_retries=result.rate_limit_retries,
                )
                action_error = catalog.validate_recommendation(
                    rca.recommended_action,
                    rca.action_parameters.model_dump(exclude_none=True),
                    rca.target.kind,
                    evidence,
                )
            except (RCAValidationError, OpenAIError, OSError, ValueError) as error:
                result = AgentRunResult(
                    status="escalated",
                    evidence_ids=result.evidence_ids,
                    error=str(error) or type(error).__name__,
                )
        async with session_factory() as session:
            incident = await session.scalar(
                select(Incident)
                .where(Incident.id == incident_id)
                .with_for_update()
            )
            if incident is not None and incident.state == IncidentState.INVESTIGATING:
                if rca is not None:
                    incident.rca_json = rca.model_dump(
                        mode="json", exclude_none=True
                    )
                if result.status == "escalated" or action_error is not None:
                    reason = (
                        action_error
                        or result.error
                        or "investigation_failed"
                    )
                    detail = {
                        "stage": "rca",
                        "reason": reason,
                        "evidence_count": len(result.evidence_ids),
                        "rca_attached": rca is not None,
                    }
                    if rca is not None:
                        detail["recommended_action"] = rca.recommended_action
                    await transition_incident(
                        session,
                        incident,
                        IncidentState.ESCALATED,
                        detail=detail,
                    )
                    await session.commit()
                    return result
                if result.status == "completed":
                    session.add(
                        AuditEvent(
                            incident_id=incident_id,
                            from_state=IncidentState.INVESTIGATING.value,
                            to_state=IncidentState.INVESTIGATING.value,
                            actor="system",
                            detail_json={
                                "stage": "evidence_investigation",
                                "status": "completed",
                                "evidence_count": len(result.evidence_ids),
                                "recommended_action": rca.recommended_action,
                            },
                        )
                    )
                    await transition_incident(
                        session,
                        incident,
                        IncidentState.ANALYZED,
                        detail={
                            "stage": "rca",
                            "recommended_action": rca.recommended_action,
                            "llm_confidence": rca.llm_confidence,
                            "evidence_count": len(result.evidence_ids),
                        },
                    )
                    await session.commit()
                    return result
    return result


async def escalate_stuck_investigations(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    timeout_seconds: int,
) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Incident, AuditEvent)
                .join(AuditEvent, AuditEvent.incident_id == Incident.id)
                .where(
                    Incident.state == IncidentState.INVESTIGATING,
                    AuditEvent.from_state == IncidentState.ENRICHED.value,
                    AuditEvent.to_state == IncidentState.INVESTIGATING.value,
                )
            )
        ).all()
        stale_ids = set()
        for incident, event in rows:
            started_at = event.created_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            if started_at >= cutoff or incident.id in stale_ids:
                continue
            stale_ids.add(incident.id)
            incident = await session.scalar(
                select(Incident).where(Incident.id == incident.id).with_for_update()
            )
            if incident is not None and incident.state == IncidentState.INVESTIGATING:
                await transition_incident(
                    session,
                    incident,
                    IncidentState.ESCALATED,
                    detail={
                        "stage": "investigation_watchdog",
                        "reason": "investigation_timeout",
                        "timeout_seconds": timeout_seconds,
                    },
                )
        await session.commit()
    return len(stale_ids)


async def escalate_unstarted_investigation(
    incident_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reason: str,
) -> None:
    async with session_factory() as session:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .with_for_update()
        )
        if incident is None or incident.state != IncidentState.ENRICHED:
            return
        await transition_incident(
            session,
            incident,
            IncidentState.ESCALATED,
            detail={"stage": "investigation_startup", "reason": reason},
        )
        await session.commit()


def create_agent_runner(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> AgentRunner:
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
        raise AgentConfigurationError("LLM_API_KEY is not configured")
    client = AsyncOpenAI(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        timeout=300,
        max_retries=1,
    )
    return AgentRunner(
        client,
        session_factory,
        model=settings.llm_model,
        timeout_seconds=settings.investigation_timeout_seconds,
    )
