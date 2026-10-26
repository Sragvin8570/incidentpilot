from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

import httpx
from kubernetes.config.config_exception import ConfigException
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.actions import ActionCatalog, ActionDefinition
from app.config import Settings
from app.db.models import (
    Decision,
    Execution,
    Incident,
    IncidentState,
    Verification,
)
from app.executor.kubernetes import KubernetesExecutionClient
from app.incidents.state_machine import transition_incident

VERIFICATION_CHECKS = (
    "workload_health",
    "alert_resolved",
    "metric_recovery",
    "change_persistence",
)
POLL_INTERVAL_SECONDS = 5


class VerificationDependencyError(RuntimeError):
    pass


class PrometheusVerificationClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def query(self, promql: str) -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    f"{self.base_url}/api/v1/query",
                    params={"query": promql},
                )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if (
                not isinstance(data, dict)
                or payload.get("status") != "success"
                or not isinstance(data.get("result"), list)
            ):
                raise VerificationDependencyError(
                    "Prometheus returned an invalid instant-query response"
                )
            values = []
            for item in data["result"]:
                if not isinstance(item, dict):
                    continue
                sample = item.get("value")
                if isinstance(sample, list) and len(sample) == 2:
                    values.append(float(sample[1]))
            return values
        except VerificationDependencyError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise VerificationDependencyError(
                f"Prometheus recovery query failed: {error}"
            ) from error


class AlertmanagerVerificationClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def is_firing(self, fingerprint: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(f"{self.base_url}/api/v2/alerts")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise VerificationDependencyError(
                    "Alertmanager returned an invalid alerts response"
                )
            return any(
                alert.get("fingerprint") == fingerprint
                for alert in payload
                if isinstance(alert, dict)
            )
        except VerificationDependencyError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            raise VerificationDependencyError(
                f"Alertmanager firing-state query failed: {error}"
            ) from error


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    observed: Any
    expected: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "observed": self.observed,
            "expected": self.expected,
        }


def evaluate_workload_health(samples: list[dict[str, Any]]) -> CheckResult:
    if not samples:
        return CheckResult(
            "workload_health", False, "No workload health samples", "Ready workload"
        )
    increases: list[dict[str, Any]] = []
    previous: dict[str, int] = {}
    for sample in samples:
        counts = sample.get("restart_counts", {})
        for key, count in counts.items():
            if key in previous and count > previous[key]:
                increases.append(
                    {"container": key, "from": previous[key], "to": count}
                )
        previous.update(counts)
    final = samples[-1]
    desired = final.get("desired_replicas")
    ready = final.get("ready_replicas")
    updated = final.get("updated_replicas")
    generation = final.get("generation")
    observed_generation = final.get("observed_generation")
    healthy = (
        isinstance(desired, int)
        and isinstance(ready, int)
        and isinstance(updated, int)
        and isinstance(generation, int)
        and isinstance(observed_generation, int)
        and desired > 0
        and ready >= desired
        and updated >= desired
        and observed_generation >= generation
        and not increases
    )
    return CheckResult(
        "workload_health",
        healthy,
        {
            "desired_replicas": desired,
            "ready_replicas": ready,
            "updated_replicas": updated,
            "generation": generation,
            "observed_generation": observed_generation,
            "restart_increases": increases,
            "target_status": final.get("target_status"),
        },
        {
            "replicas": "desired, ready, and updated replicas match",
            "observed_generation": "at least current generation",
            "restart_increases": [],
        },
    )


def evaluate_alert_resolution(is_firing: bool) -> CheckResult:
    return CheckResult(
        "alert_resolved",
        not is_firing,
        "firing" if is_firing else "resolved",
        "not firing",
    )


def evaluate_metric_recovery(
    values: list[float], threshold: float, operator: str
) -> CheckResult:
    finite_values = [value for value in values if math.isfinite(value)]
    observed = max(finite_values) if finite_values else None
    passed = observed is not None and (
        observed >= threshold if operator == ">=" else observed <= threshold
    )
    return CheckResult(
        "metric_recovery",
        passed,
        observed if observed is not None else "No Prometheus samples",
        {"operator": operator, "threshold": threshold},
    )


def evaluate_change_persistence(
    current_hash: str | None, applied_hash: str | None
) -> CheckResult:
    return CheckResult(
        "change_persistence",
        current_hash is not None and current_hash == applied_hash,
        current_hash,
        applied_hash,
    )


def render_recovery_query(
    action: ActionDefinition,
    *,
    namespace: str,
    name: str,
    parameters: dict[str, Any],
) -> str:
    query = action.recovery_promql
    substitutions = {
        "__NAMESPACE__": json.dumps(namespace),
        "__NAME__": json.dumps(name),
        "__POD_REGEX__": json.dumps(re.escape(name) + "-.*"),
        "__CONTAINER__": json.dumps(str(parameters.get("container", ""))),
    }
    for marker, value in substitutions.items():
        query = query.replace(marker, value)
    if "__" in query:
        raise ValueError("Recovery PromQL contains an unsupported placeholder")
    return query


def recovery_threshold(
    action: ActionDefinition,
    execution: Execution,
    parameters: dict[str, Any],
) -> float:
    if action.recovery_threshold == "desired_replicas":
        if action.id == "scale_deployment":
            value = (execution.patch_json or {}).get("spec", {}).get("replicas")
        else:
            value = (execution.target_snapshot or {}).get("spec", {}).get(
                "replicas"
            )
            value = value or 1
        return float(value)
    if action.recovery_threshold == "memory_limit_bytes":
        containers = (
            (execution.patch_json or {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        container = next(
            (
                item
                for item in containers
                if item.get("name") == parameters.get("container")
            ),
            None,
        )
        memory = (
            container.get("resources", {}).get("limits", {}).get("memory")
            if container
            else None
        )
        if not isinstance(memory, str):
            raise ValueError("Applied memory limit is missing from the execution patch")
        return float(parse_quantity(memory))
    storage = (
        (execution.patch_json or {})
        .get("spec", {})
        .get("resources", {})
        .get("requests", {})
        .get("storage")
    )
    if not isinstance(storage, str):
        raise ValueError("Applied storage request is missing from the execution patch")
    return float(parse_quantity(storage))


async def verify_incident(
    incident_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    kubernetes_client: Any | None = None,
    prometheus_client: PrometheusVerificationClient | None = None,
    alertmanager_client: AlertmanagerVerificationClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    catalog = ActionCatalog.load()
    async with session_factory() as session:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .with_for_update()
        )
        if incident is None:
            raise LookupError(f"Incident '{incident_id}' does not exist")
        if incident.state != IncidentState.VERIFYING:
            return False
        decision = await session.scalar(
            select(Decision).where(Decision.incident_id == incident_id)
        )
        execution = await session.scalar(
            select(Execution).where(Execution.incident_id == incident_id)
        )
        if decision is None or execution is None or execution.status != "SUCCEEDED":
            raise ValueError("VERIFYING incident has no successful execution")
        action = catalog.actions.get(decision.recommended_action)
        if action is None:
            raise ValueError("Executed action is missing from the action catalog")
        parameters = (incident.rca_json or {}).get("action_parameters", {})
        namespace = incident.namespace or ""
        name = incident.target_name or ""
        target_kind = incident.target_kind or ""
        fingerprint = incident.fingerprint
        applied_hash = execution.applied_spec_hash
        verification = await session.scalar(
            select(Verification)
            .where(Verification.incident_id == incident_id)
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        lease_seconds = (
            action.stabilization_seconds + action.alert_grace_seconds + 120
        )
        if verification is not None:
            if verification.status == "COMPLETED":
                return False
            started = verification.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started > now - timedelta(seconds=lease_seconds):
                return False
            verification.started_at = now
        else:
            verification = Verification(
                incident_id=incident_id,
                status="RUNNING",
                checks=[],
                effectiveness_score=0.0,
                resolution_summary="Closed-loop verification is running.",
                started_at=now,
            )
            session.add(verification)
        await session.flush()
        verification_id = verification.id
        await session.commit()

    if kubernetes_client is None:
        try:
            kubernetes_client = KubernetesExecutionClient()
        except (ConfigException, OSError, ValueError):
            kubernetes_client = None
    prometheus_client = prometheus_client or PrometheusVerificationClient(
        settings.prometheus_url
    )
    alertmanager_client = alertmanager_client or AlertmanagerVerificationClient(
        settings.alertmanager_url
    )

    check_results: list[CheckResult] = []
    health_samples: list[dict[str, Any]] = []
    health_error: str | None = None
    started_at = time.monotonic()
    while True:
        if kubernetes_client is None:
            health_error = "Kubernetes client configuration is unavailable"
            break
        try:
            sample = await asyncio.to_thread(
                kubernetes_client.read_workload_health,
                target_kind,
                name,
                namespace,
            )
            health_samples.append(sample)
        except (ApiException, ConfigException, OSError, ValueError) as error:
            health_error = f"{type(error).__name__}: {error}"
            break
        remaining = action.stabilization_seconds - (
            time.monotonic() - started_at
        )
        if remaining <= 0:
            break
        await sleep(min(POLL_INTERVAL_SECONDS, remaining))
    if health_error is None:
        check_results.append(evaluate_workload_health(health_samples))
    else:
        check_results.append(
            CheckResult(
                "workload_health",
                False,
                health_error,
                "Ready workload with no increasing restarts",
            )
        )

    firing = True
    alert_error: str | None = None
    alert_deadline = time.monotonic() + action.alert_grace_seconds
    while True:
        try:
            firing = await alertmanager_client.is_firing(fingerprint)
        except VerificationDependencyError as error:
            alert_error = str(error)
            break
        if not firing or time.monotonic() >= alert_deadline:
            break
        await sleep(min(POLL_INTERVAL_SECONDS, alert_deadline - time.monotonic()))
    if alert_error is None:
        check_results.append(evaluate_alert_resolution(firing))
    else:
        check_results.append(
            CheckResult(
                "alert_resolved", False, alert_error, "original fingerprint not firing"
            )
        )

    try:
        query = render_recovery_query(
            action,
            namespace=namespace,
            name=name,
            parameters=parameters,
        )
        threshold = recovery_threshold(action, execution, parameters)
        threshold *= action.recovery_threshold_factor
        values = await prometheus_client.query(query)
        check_results.append(
            evaluate_metric_recovery(values, threshold, action.recovery_operator)
        )
    except (VerificationDependencyError, ValueError, TypeError) as error:
        check_results.append(
            CheckResult(
                "metric_recovery",
                False,
                f"{type(error).__name__}: {error}",
                "Configured recovery metric meets its threshold",
            )
        )

    if kubernetes_client is None:
        current_hash = None
    else:
        try:
            current_hash = await asyncio.to_thread(
                kubernetes_client.read_current_spec_hash,
                target_kind,
                name,
                namespace,
            )
        except (ApiException, ConfigException, OSError, ValueError) as error:
            current_hash = f"{type(error).__name__}: {error}"
    check_results.append(evaluate_change_persistence(current_hash, applied_hash))

    check_dicts = [check.as_dict() for check in check_results]
    passed_count = sum(check.passed for check in check_results)
    score = passed_count / len(VERIFICATION_CHECKS)
    failed = [check.name for check in check_results if not check.passed]
    summary = (
        "All closed-loop verification checks passed."
        if not failed
        else f"Closed-loop verification failed: {', '.join(failed)}."
    )
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .with_for_update()
        )
        verification = await session.scalar(
            select(Verification)
            .where(Verification.id == verification_id)
            .with_for_update()
        )
        if incident is None:
            raise LookupError(f"Incident '{incident_id}' disappeared during verification")
        if verification is None:
            raise LookupError(
                f"Verification '{verification_id}' disappeared before completion"
            )
        if (
            incident.state != IncidentState.VERIFYING
            or verification.status != "RUNNING"
        ):
            return False
        verification.status = "COMPLETED"
        verification.checks = check_dicts
        verification.effectiveness_score = score
        verification.resolution_summary = summary
        verification.completed_at = now
        if not failed:
            incident.resolved_at = now
            await transition_incident(
                session,
                incident,
                IncidentState.RESOLVED,
                detail={
                    "stage": "verification",
                    "effectiveness_score": score,
                    "summary": summary,
                    "checks": check_dicts,
                },
            )
        else:
            await transition_incident(
                session,
                incident,
                IncidentState.ESCALATED,
                detail={
                    "stage": "verification",
                    "effectiveness_score": score,
                    "summary": summary,
                    "failed_checks": failed,
                    "checks": check_dicts,
                },
            )
        await session.commit()
    return not failed
