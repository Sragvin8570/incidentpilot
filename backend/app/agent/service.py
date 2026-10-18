from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.runner import AgentRunResult, AgentRunner
from app.agent.tools.base import ToolRegistry
from app.config import Settings
from app.db.models import AlertOccurrence, AuditEvent, Incident, IncidentState
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
        async with session_factory() as session:
            incident = await session.scalar(
                select(Incident)
                .where(Incident.id == incident_id)
                .with_for_update()
            )
            if incident is not None and incident.state == IncidentState.INVESTIGATING:
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
                            },
                        )
                    )
                    await session.commit()
                    return result
                await transition_incident(
                    session,
                    incident,
                    IncidentState.ESCALATED,
                    detail={
                        "stage": "investigation",
                        "reason": result.error or "investigation_failed",
                        "evidence_count": len(result.evidence_ids),
                    },
                )
                await session.commit()
    return result


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
    )
