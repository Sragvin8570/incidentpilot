from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.actions import ActionCatalog
from app.db.models import Decision, Execution, Incident, IncidentState
from app.executor.kubernetes import KubernetesExecutionClient
from app.incidents.state_machine import transition_incident


NAMESPACE_ENVIRONMENTS = {
    "dev": "dev",
    "staging": "staging",
    "prod": "production",
}


async def execute_ready_incident(
    incident_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    executor_image: str,
    timeout_seconds: int,
    kubernetes_client: KubernetesExecutionClient | None = None,
) -> bool:
    async with session_factory() as session:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .with_for_update()
        )
        if incident is None:
            raise LookupError(f"Incident '{incident_id}' does not exist")
        if incident.state not in {IncidentState.READY, IncidentState.APPROVED}:
            return False
        initial_state = incident.state
        existing = await session.scalar(
            select(Execution).where(Execution.incident_id == incident_id)
        )
        if existing is not None:
            return False
        decision = await session.scalar(
            select(Decision).where(Decision.incident_id == incident_id)
        )
        action_id = decision.recommended_action if decision is not None else "unknown"
        execution = Execution(
            incident_id=incident_id,
            action_id=action_id,
            status="PREPARING",
        )
        session.add(execution)
        await session.commit()
        execution_id = execution.id

    try:
        expected_outcome = (
            "READY" if initial_state == IncidentState.READY else "AWAITING_APPROVAL"
        )
        if decision is None or decision.outcome != expected_outcome:
            raise ValueError("Incident has no matching execution policy decision")
        action_id, target_kind, parameters = _execution_input(
            incident, decision, allow_approved=initial_state == IncidentState.APPROVED
        )
        manager = kubernetes_client or KubernetesExecutionClient()
        snapshot, snapshot_hash, patch = manager.prepare(
            action_id=action_id,
            target_kind=target_kind,
            parameters=parameters,
        )
    except Exception as error:
        await _finish_failure(
            incident_id,
            execution_id,
            session_factory,
            status="FAILED",
            error=f"preflight_failed:{type(error).__name__}:{error}",
            logs=f"Preflight failed: {type(error).__name__}: {error}",
            expected_state=initial_state,
        )
        return False

    job_name = f"incidentpilot-exec-{incident_id.hex[:12]}"
    service_account = f"incidentpilot-executor-{action_id.replace('_', '-')}"
    async with session_factory() as session:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .with_for_update()
        )
        execution = await session.get(Execution, execution_id)
        if (
            incident is None
            or execution is None
            or incident.state != initial_state
        ):
            return False
        execution.target_snapshot = snapshot
        execution.snapshot_hash = snapshot_hash
        execution.patch_json = patch
        execution.job_name = job_name
        execution.status = "RUNNING"
        await transition_incident(
            session,
            incident,
            IncidentState.EXECUTING,
            detail={
                "stage": "execution",
                "action_id": action_id,
                "snapshot_hash": snapshot_hash,
                "job_name": job_name,
            },
        )
        await session.commit()

    logs = ""
    try:
        manager.create_job(
            name=job_name,
            incident_id=str(incident_id),
            namespace=parameters["namespace"],
            service_account=service_account,
            image=executor_image,
            action_id=action_id,
            target_kind=target_kind,
            parameters=parameters,
            patch=patch,
            timeout_seconds=timeout_seconds,
        )
        succeeded, job_error = manager.wait_for_job(
            job_name,
            parameters["namespace"],
            timeout_seconds,
        )
        logs = manager.read_job_logs(job_name, parameters["namespace"])
        if not succeeded:
            raise RuntimeError(job_error or "Kubernetes Job failed")
    except Exception as error:
        await _finish_failure(
            incident_id,
            execution_id,
            session_factory,
            status="FAILED",
            error=f"job_failed:{type(error).__name__}:{error}",
            logs=_append_error(logs, error),
            expected_state=IncidentState.EXECUTING,
        )
        return False

    async with session_factory() as session:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .with_for_update()
        )
        execution = await session.get(Execution, execution_id)
        if (
            incident is None
            or execution is None
            or incident.state != IncidentState.EXECUTING
        ):
            return False
        execution.status = "SUCCEEDED"
        execution.logs = logs
        execution.completed_at = datetime.now(timezone.utc)
        await transition_incident(
            session,
            incident,
            IncidentState.VERIFYING,
            detail={
                "stage": "execution",
                "action_id": action_id,
                "job_name": job_name,
                "snapshot_hash": snapshot_hash,
            },
        )
        await session.commit()
    return True


def _execution_input(
    incident: Incident,
    decision: Decision,
    *,
    allow_approved: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    namespace = incident.namespace
    if namespace not in NAMESPACE_ENVIRONMENTS:
        raise ValueError("Execution is restricted to dev, staging, and prod")
    if incident.environment != NAMESPACE_ENVIRONMENTS[namespace]:
        raise ValueError("Incident environment does not match its execution namespace")
    rca = incident.rca_json or {}
    raw_parameters = rca.get("action_parameters")
    if not isinstance(raw_parameters, dict):
        raise ValueError("Incident RCA has no action parameters")
    parameters = dict(raw_parameters)
    if (
        parameters.get("namespace") != namespace
        or parameters.get("name") != incident.target_name
    ):
        raise ValueError("Action parameters do not match the incident target")
    catalog = ActionCatalog.load()
    action = catalog.actions.get(decision.recommended_action)
    if action is None:
        raise ValueError("Recommended action is not configured")
    if action.risk_level == "high" and not allow_approved:
        raise ValueError("High-risk actions require approval")
    if (
        incident.target_kind in {"StatefulSet", "Node", "PersistentVolumeClaim"}
        and not allow_approved
    ):
        raise ValueError("This target kind requires approval")
    if incident.target_kind not in action.applies_to_kinds:
        raise ValueError("Configured action does not support the incident target")
    errors = sorted(
        Draft202012Validator(action.parameters).iter_errors(parameters),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(f"Invalid action parameters: {errors[0].message}")
    return decision.recommended_action, incident.target_kind or "", parameters


async def _finish_failure(
    incident_id: UUID,
    execution_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: str,
    error: str,
    logs: str,
    expected_state: IncidentState,
) -> None:
    async with session_factory() as session:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .with_for_update()
        )
        execution = await session.get(Execution, execution_id)
        if execution is None:
            raise LookupError(f"Execution '{execution_id}' disappeared")
        execution.status = status
        execution.error = error
        execution.logs = logs
        execution.completed_at = datetime.now(timezone.utc)
        if incident is not None and incident.state == expected_state:
            if expected_state == IncidentState.EXECUTING:
                await transition_incident(
                    session,
                    incident,
                    IncidentState.FAILED,
                    detail={"stage": "execution", "error": error},
                )
                expected_state = IncidentState.FAILED
            if expected_state == IncidentState.FAILED:
                await transition_incident(
                    session,
                    incident,
                    IncidentState.ESCALATED,
                    detail={"stage": "execution", "error": error},
                )
            elif expected_state in {
                IncidentState.READY,
                IncidentState.APPROVED,
            }:
                await transition_incident(
                    session,
                    incident,
                    IncidentState.ESCALATED,
                    detail={"stage": "execution_preflight", "error": error},
                )
        await session.commit()


def _append_error(logs: str, error: Exception) -> str:
    suffix = f"Job failed: {type(error).__name__}: {error}"
    return f"{logs.rstrip()}\n{suffix}".strip()
