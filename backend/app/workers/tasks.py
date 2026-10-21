import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.service import (
    AgentConfigurationError,
    create_agent_runner,
    escalate_unstarted_investigation,
    escalate_stuck_investigations,
    run_investigation,
)
from app.agent.tools.base import ToolRegistry
from app.agent.tools.kubernetes import KubernetesEvidenceTools
from app.agent.tools.prometheus import PrometheusEvidenceTools
from app.agent.tools.runbooks import RunbookSearchTools
from app.approvals.service import expire_pending_approvals
from app.config import get_settings
from app.db.models import Incident, IncidentState
from app.executor.service import execute_ready_incident
from app.runbooks.embeddings import LocalRunbookEmbedder
from app.verification.service import verify_incident
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
            RunbookSearchTools(
                session_factory,
                LocalRunbookEmbedder(settings.runbook_embedding_model),
            ).registry(),
        )
        await run_investigation(
            incident_id,
            runner,
            registry,
            session_factory,
        )
        async with session_factory() as session:
            incident = await session.scalar(
                select(Incident).where(Incident.id == incident_id)
            )
            if incident is not None and incident.state == IncidentState.READY:
                celery_app.send_task(
                    "incidentpilot.execute_ready_incident",
                    args=[str(incident_id)],
                    task_id=f"execute-{incident_id}",
                )
    finally:
        if runner is not None:
            await runner.client.close()
        await engine.dispose()


@celery_app.task(
    name="incidentpilot.execute_ready_incident",
    ignore_result=True,
    acks_late=True,
)
def execute_ready_incident_task(incident_id: str) -> bool:
    executed = asyncio.run(_execute_ready_incident(UUID(incident_id)))
    if executed:
        celery_app.send_task(
            "incidentpilot.verify_incident",
            args=[incident_id],
            task_id=f"verify-{incident_id}",
        )
    return executed


async def _execute_ready_incident(incident_id: UUID) -> bool:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        return await execute_ready_incident(
            incident_id,
            session_factory,
            executor_image=settings.executor_image,
            timeout_seconds=settings.execution_timeout_seconds,
        )
    finally:
        await engine.dispose()


@celery_app.task(
    name="incidentpilot.verify_incident",
    ignore_result=True,
    acks_late=True,
)
def verify_incident_task(incident_id: str) -> bool:
    return asyncio.run(_verify_incident(UUID(incident_id)))


async def _verify_incident(incident_id: UUID) -> bool:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        return await verify_incident(
            incident_id,
            session_factory,
            settings,
        )
    finally:
        await engine.dispose()


@celery_app.task(
    name="incidentpilot.verify_pending_incidents",
    ignore_result=True,
)
def verify_pending_incidents_task() -> int:
    return asyncio.run(_verify_pending_incidents())


async def _verify_pending_incidents() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            incident_ids = (
                await session.scalars(
                    select(Incident.id).where(
                        Incident.state == IncidentState.VERIFYING
                    )
                )
            ).all()
        verified = 0
        for incident_id in incident_ids:
            verified += await _verify_incident(incident_id)
        return verified
    finally:
        await engine.dispose()


@celery_app.task(
    name="incidentpilot.escalate_stuck_investigations",
    ignore_result=True,
)
def escalate_stuck_investigations_task() -> int:
    return asyncio.run(_escalate_stuck_investigations())


async def _escalate_stuck_investigations() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        return await escalate_stuck_investigations(
            session_factory,
            timeout_seconds=settings.investigation_timeout_seconds,
        )
    finally:
        await engine.dispose()


@celery_app.task(
    name="incidentpilot.expire_pending_approvals",
    ignore_result=True,
)
def expire_pending_approvals_task() -> int:
    return asyncio.run(_expire_pending_approvals())


async def _expire_pending_approvals() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        return await expire_pending_approvals(
            session_factory,
            expiry_minutes=settings.approval_expiry_minutes,
        )
    finally:
        await engine.dispose()
