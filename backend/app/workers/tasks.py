import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.service import (
    AgentConfigurationError,
    create_agent_runner,
    escalate_unstarted_investigation,
    run_investigation,
)
from app.agent.tools.base import ToolRegistry
from app.agent.tools.kubernetes import KubernetesEvidenceTools
from app.agent.tools.prometheus import PrometheusEvidenceTools
from app.config import get_settings
from app.workers.celery_app import celery_app


@celery_app.task(
    name="incidentpilot.investigate_incident",
    ignore_result=True,
    acks_late=True,
)
def investigate_incident_task(incident_id: str) -> None:
    asyncio.run(_investigate_incident(UUID(incident_id)))


async def _investigate_incident(incident_id: UUID) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    runner = None
    try:
        try:
            runner = create_agent_runner(session_factory, settings)
        except AgentConfigurationError as error:
            await escalate_unstarted_investigation(
                incident_id,
                session_factory,
                reason=str(error),
            )
            return

        registry = ToolRegistry.combine(
            KubernetesEvidenceTools().registry(),
            PrometheusEvidenceTools(settings.prometheus_url).registry(),
        )
        await run_investigation(
            incident_id,
            runner,
            registry,
            session_factory,
        )
    finally:
        if runner is not None:
            await runner.client.close()
        await engine.dispose()

