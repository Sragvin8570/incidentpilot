from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import UUID

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.db.models import AuditEvent, Evidence, Incident, IncidentState  # noqa: E402

POLL_INTERVAL_SECONDS = 5
ALERT_TIMEOUT_SECONDS = 240
INVESTIGATION_TIMEOUT_SECONDS = 360
PROMETHEUS_URL = "http://127.0.0.1:9090"
ALERTMANAGER_URL = "http://127.0.0.1:9093"
API_URL = "http://127.0.0.1:8000"
KUBECTL = ["kubectl", "--context", "kind-incidentpilot"]


@dataclass
class Gate:
    phase: str
    done_when: str
    outcome: str = "FAIL"
    evidence: str = "Not run"


class VerificationFailure(RuntimeError):
    pass


def run_command(
    args: list[str],
    *,
    cwd: Path = ROOT,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerificationFailure(
            f"Could not run {args[0]} successfully"
        ) from error
    if result.returncode:
        raise VerificationFailure(
            f"{args[0]} exited with status {result.returncode}"
        )
    return result


def request_json(url: str, *, timeout: float = 5) -> Any:
    try:
        with urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def endpoint_responds(url: str) -> bool:
    try:
        with urlopen(url, timeout=1) as response:
            return 200 <= response.status < 400
    except (OSError, URLError, TimeoutError):
        return False


def start_port_forward(
    service: str,
    local_port: int,
    remote_port: int,
) -> subprocess.Popen | None:
    url = (
        PROMETHEUS_URL + "/-/ready"
        if remote_port == 9090
        else ALERTMANAGER_URL + "/-/ready"
    )
    if endpoint_responds(url):
        return None
    try:
        process = subprocess.Popen(
            [
                *KUBECTL,
                "port-forward",
                "-n",
                "monitoring",
                f"svc/{service}",
                f"{local_port}:{remote_port}",
                "--address=127.0.0.1",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise VerificationFailure("Could not start a Kubernetes port-forward") from error
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VerificationFailure(
                f"Port-forward for {service} exited with status {process.returncode}"
            )
        if endpoint_responds(url):
            return process
        time.sleep(1)
    process.terminate()
    process.wait(timeout=10)
    raise VerificationFailure(f"Port-forward for {service} did not become ready")


def start_local_process(
    url: str,
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.Popen | None:
    if endpoint_responds(url):
        return None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise VerificationFailure(
            f"Could not start local service at {url.split('/')[2]}"
        ) from error
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VerificationFailure(
                f"Local service exited with status {process.returncode}"
            )
        if endpoint_responds(url):
            return process
        time.sleep(1)
    process.terminate()
    process.wait(timeout=10)
    raise VerificationFailure(f"Local service at {url.split('/')[2]} did not become ready")


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        raise VerificationFailure(
            "Required local environment settings are missing or invalid"
        ) from error


def require_runtime_tools() -> None:
    missing = [
        name
        for name in ("docker", "kind", "helm", "kubectl")
        if shutil.which(name) is None
    ]
    if missing:
        raise VerificationFailure(
            "Required commands are unavailable: " + ", ".join(missing)
        )
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise VerificationFailure("Docker engine is not reachable")


def ensure_cluster_and_monitoring() -> None:
    clusters = run_command(["kind", "get", "clusters"], timeout=30).stdout.split()
    if "incidentpilot" not in clusters:
        run_command(
            [
                "kind",
                "create",
                "cluster",
                "--name",
                "incidentpilot",
                "--config",
                "deploy/kind/cluster.yaml",
                "--wait",
                "3m",
            ],
            timeout=240,
        )
    run_command(
        [
            *KUBECTL,
            "cluster-info",
            "--context",
            "kind-incidentpilot",
        ],
        timeout=30,
    )
    run_command(
        [
            "helm",
            "repo",
            "add",
            "prometheus-community",
            "https://prometheus-community.github.io/helm-charts",
            "--force-update",
        ],
        timeout=60,
    )
    run_command(["helm", "repo", "update"], timeout=120)
    run_command(
        [
            "helm",
            "upgrade",
            "--install",
            "monitoring",
            "prometheus-community/kube-prometheus-stack",
            "--namespace",
            "monitoring",
            "--kube-context",
            "kind-incidentpilot",
            "--create-namespace",
            "--values",
            "deploy/prometheus/values.yaml",
            "--wait",
            "--timeout",
            "10m",
        ],
        timeout=660,
    )
    run_command([*KUBECTL, "apply", "-f", "deploy/kubernetes/namespaces.yaml"])


def install_webhook_secret(token: SecretStr) -> None:
    if not token.get_secret_value():
        raise VerificationFailure("ALERTMANAGER_WEBHOOK_TOKEN is not configured")
    try:
        config.load_kube_config(context="kind-incidentpilot")
        api = client.CoreV1Api()
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name="incidentpilot-alertmanager-webhook-token",
                namespace="monitoring",
            ),
            type="Opaque",
            string_data={"token": token.get_secret_value()},
        )
        try:
            api.read_namespaced_secret(
                secret.metadata.name,
                secret.metadata.namespace,
            )
        except ApiException as error:
            if error.status != 404:
                raise
            api.create_namespaced_secret("monitoring", secret)
        else:
            api.replace_namespaced_secret(
                secret.metadata.name,
                secret.metadata.namespace,
                secret,
            )
    except ApiException as error:
        raise VerificationFailure(
            f"Could not safely configure the Alertmanager webhook Secret (HTTP {error.status})"
        ) from error
    except (OSError, ConfigException) as error:
        raise VerificationFailure(
            "Could not load the Kind Kubernetes credentials"
        ) from error


def ensure_infrastructure(settings: Settings) -> None:
    require_runtime_tools()
    run_command(
        ["docker", "compose", "up", "-d", "postgres", "redis"],
        timeout=180,
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "backend")
    migration = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "backend/alembic.ini",
            "upgrade",
            "head",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if migration.returncode:
        raise VerificationFailure("Database migrations did not complete")
    ensure_cluster_and_monitoring()
    run_command(["docker", "build", "-t", "incidentpilot-demo:local", "demo"])
    run_command(
        [
            "kind",
            "load",
            "docker-image",
            "incidentpilot-demo:local",
            "--name",
            "incidentpilot",
        ],
        timeout=180,
    )
    run_command([*KUBECTL, "apply", "-f", "deploy/kubernetes/demo.yaml"])
    run_command(
        [
            *KUBECTL,
            "apply",
            "-f",
            "deploy/prometheus/demo-servicemonitor.yaml",
        ]
    )
    run_command(
        [*KUBECTL, "apply", "-f", "deploy/prometheus/crashloop-rule.yaml"]
    )
    install_webhook_secret(settings.alertmanager_webhook_token or SecretStr(""))
    run_command(
        [
            *KUBECTL,
            "apply",
            "-f",
            "deploy/prometheus/incidentpilot-alertmanager-config.yaml",
        ]
    )


def wait_for_celery_worker(process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VerificationFailure(
                f"Celery worker exited with status {process.returncode}"
            )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "celery",
                "--workdir",
                "backend",
                "-A",
                "app.workers.celery_app:celery_app",
                "inspect",
                "ping",
                "--timeout=2",
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and "pong" in result.stdout.lower():
            return
        time.sleep(2)
    raise VerificationFailure("Celery worker did not become responsive")


def prom_query(query: str) -> list[dict[str, Any]]:
    payload = request_json(
        f"{PROMETHEUS_URL}/api/v1/query?{urlencode({'query': query})}"
    )
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return []
    result = payload.get("data", {}).get("result", [])
    return result if isinstance(result, list) else []


async def database_is_ready(settings: Settings) -> bool:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
        return True
    except SQLAlchemyError as error:
        raise VerificationFailure("PostgreSQL is not ready") from error
    finally:
        await engine.dispose()


def crashloop_manifest(deployment_name: str) -> str:
    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": deployment_name,
            "namespace": "dev",
            "labels": {"app.kubernetes.io/name": deployment_name},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": deployment_name}},
            "template": {
                "metadata": {
                    "labels": {"app.kubernetes.io/name": deployment_name}
                },
                "spec": {
                    "containers": [
                        {
                            "name": "crash",
                            "image": "busybox:1.36",
                            "command": ["sh", "-c", "echo incidentpilot-test; exit 1"],
                        }
                    ]
                },
            },
        },
    }
    return yaml.safe_dump(manifest, sort_keys=False)


def apply_crashloop(deployment_name: str) -> None:
    result = subprocess.run(
        [*KUBECTL, "apply", "-f", "-"],
        cwd=ROOT,
        input=crashloop_manifest(deployment_name),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise VerificationFailure("Could not deploy the dev CrashLoop demo")


def wait_for_crashloop_alert(deployment_name: str) -> dict[str, Any]:
    deadline = time.monotonic() + ALERT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        payload = request_json(f"{ALERTMANAGER_URL}/api/v2/alerts")
        if isinstance(payload, list):
            for alert in payload:
                labels = alert.get("labels", {})
                if (
                    labels.get("alertname") == "IncidentPilotPodCrashLooping"
                    and labels.get("namespace") == "dev"
                    and deployment_name in labels.get("pod", "")
                    and alert.get("status", {}).get("state") == "active"
                ):
                    return alert
        time.sleep(POLL_INTERVAL_SECONDS)
    raise VerificationFailure("Timed out waiting for the dev CrashLoop alert")


async def incident_evidence(
    settings: Settings,
    fingerprint: str,
) -> tuple[list[Incident], dict[UUID, list[Evidence]], dict[UUID, list[AuditEvent]]]:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            incidents = (
                await session.scalars(
                    select(Incident).where(Incident.fingerprint == fingerprint)
                )
            ).all()
            evidence: dict[UUID, list[Evidence]] = {}
            audit: dict[UUID, list[AuditEvent]] = {}
            for incident in incidents:
                evidence[incident.id] = (
                    await session.scalars(
                        select(Evidence)
                        .where(Evidence.incident_id == incident.id)
                        .order_by(Evidence.created_at)
                    )
                ).all()
                audit[incident.id] = (
                    await session.scalars(
                        select(AuditEvent)
                        .where(AuditEvent.incident_id == incident.id)
                        .order_by(AuditEvent.id)
                    )
                ).all()
            return list(incidents), evidence, audit
    finally:
        await engine.dispose()


def wait_for_investigation(
    settings: Settings,
    fingerprint: str,
) -> tuple[list[Incident], dict[UUID, list[Evidence]], dict[UUID, list[AuditEvent]]]:
    deadline = time.monotonic() + INVESTIGATION_TIMEOUT_SECONDS
    latest = ([], {}, {})
    while time.monotonic() < deadline:
        latest = asyncio.run(incident_evidence(settings, fingerprint))
        incidents, evidence_by_id, audit_by_id = latest
        if len(incidents) == 1:
            incident = incidents[0]
            has_completion = any(
                event.detail_json.get("stage") == "evidence_investigation"
                and event.detail_json.get("status") == "completed"
                for event in audit_by_id[incident.id]
            )
            has_escalation = any(
                event.to_state == IncidentState.ESCALATED.value
                for event in audit_by_id[incident.id]
            )
            if has_completion or has_escalation:
                return latest
        time.sleep(POLL_INTERVAL_SECONDS)
    return latest


def wait_for_incident(
    settings: Settings,
    fingerprint: str,
) -> tuple[list[Incident], dict[UUID, list[Evidence]], dict[UUID, list[AuditEvent]]]:
    deadline = time.monotonic() + ALERT_TIMEOUT_SECONDS
    latest = ([], {}, {})
    while time.monotonic() < deadline:
        latest = asyncio.run(incident_evidence(settings, fingerprint))
        if latest[0]:
            return latest
        time.sleep(POLL_INTERVAL_SECONDS)
    return latest


def print_report(gates: list[Gate]) -> None:
    print("| Phase | done_when | Result | Evidence |")
    print("|---|---|---|---|")
    for gate in gates:
        safe_evidence = gate.evidence.replace("|", "\\|").replace("\n", " ")
        print(
            f"| {gate.phase} | {gate.done_when} | "
            f"{gate.outcome} | {safe_evidence} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live verification for IncidentPilot phases 1-3."
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Confirm that Docker Desktop and the Kind environment are ready for changes.",
    )
    args = parser.parse_args()
    gates = [
        Gate(
            "Phase 1",
            "Backend /healthz OK; Prometheus scraping the demo app.",
        ),
        Gate(
            "Phase 2",
            "Crash loop creates exactly one ENRICHED incident with correct environment and target.",
        ),
        Gate(
            "Phase 3",
            "Agent collects relevant real-tool evidence within limits and records completion or reasoned escalation.",
        ),
    ]
    children: list[subprocess.Popen] = []
    deployment_name = f"incidentpilot-crash-{int(time.time())}"
    deployment_created = False
    try:
        if not args.confirm_live:
            raise VerificationFailure(
                "Live changes are gated. Rerun with --confirm-live after the team confirms readiness."
            )
        if not shutil.which("kubectl"):
            raise VerificationFailure("kubectl is not available")
        settings = load_settings()
        if not settings.alertmanager_webhook_token:
            raise VerificationFailure("ALERTMANAGER_WEBHOOK_TOKEN is not configured")
        if not settings.llm_api_key or not settings.llm_api_key.get_secret_value():
            raise VerificationFailure("LLM_API_KEY is not configured")
        ensure_infrastructure(settings)

        prometheus_forward = start_port_forward(
            "monitoring-kube-prometheus-prometheus", 9090, 9090
        )
        alertmanager_forward = start_port_forward(
            "monitoring-kube-prometheus-alertmanager", 9093, 9093
        )
        children.extend(
            process
            for process in (prometheus_forward, alertmanager_forward)
            if process is not None
        )

        api_process = start_local_process(
            API_URL + "/healthz",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
            cwd=ROOT / "backend",
            environment={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        )
        if api_process is not None:
            children.append(api_process)

        try:
            worker_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "celery",
                    "--workdir",
                    "backend",
                    "-A",
                    "app.workers.celery_app:celery_app",
                    "worker",
                    "--loglevel=WARNING",
                    "--pool=solo",
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise VerificationFailure("Could not start the Celery worker") from error
        children.append(worker_process)
        wait_for_celery_worker(worker_process)

        health = request_json(API_URL + "/healthz")
        ready = request_json(API_URL + "/readyz")
        database_ready = asyncio.run(database_is_ready(settings))
        scrape_deadline = time.monotonic() + 180
        scraped = []
        while time.monotonic() < scrape_deadline:
            scraped = prom_query('incidentpilot_demo_up{namespace="dev"}')
            if scraped:
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        if (
            not isinstance(health, dict)
            or health.get("status") != "ok"
            or not isinstance(ready, dict)
            or ready.get("status") != "ready"
            or not database_ready
        ):
            raise VerificationFailure(
                "Backend health/readiness did not confirm healthy API, PostgreSQL, and Redis"
            )
        if not scraped:
            raise VerificationFailure(
                "Prometheus is not scraping the demo workload in dev"
            )
        gates[0].outcome = "PASS"
        gates[0].evidence = (
            "API /healthz=ok and /readyz=ready; PostgreSQL query succeeded; "
            f"Prometheus returned {len(scraped)} dev demo series"
        )

        if not endpoint_responds(ALERTMANAGER_URL + "/-/ready"):
            raise VerificationFailure("Alertmanager is not ready")
        apply_crashloop(deployment_name)
        deployment_created = True
        alert = wait_for_crashloop_alert(deployment_name)
        labels = alert.get("labels", {})
        fingerprint = str(alert.get("fingerprint", ""))
        pod_name = labels.get("pod", "")
        if not fingerprint or not pod_name:
            raise VerificationFailure(
                "Alertmanager alert did not contain a fingerprint and pod label"
            )

        incidents, evidence_by_id, audit_by_id = wait_for_incident(
            settings, fingerprint
        )
        if len(incidents) != 1:
            raise VerificationFailure(
                f"Expected exactly one incident for fingerprint; found {len(incidents)}"
            )
        incident = incidents[0]
        audit = audit_by_id[incident.id]
        enriched = any(
            event.from_state == IncidentState.RECEIVED.value
            and event.to_state == IncidentState.ENRICHED.value
            for event in audit
        )
        target_matches = (
            incident.namespace == "dev"
            and incident.environment == "dev"
            and incident.target_kind == "Deployment"
            and incident.target_name == deployment_name
        )
        if enriched and target_matches:
            gates[1].outcome = "PASS"
            gates[1].evidence = (
                f"1 incident; RECEIVED->ENRICHED audit; namespace=dev; "
                f"environment=dev; target=Deployment/{deployment_name}"
            )
        else:
            raise VerificationFailure(
                "Incident did not have the expected enrichment transition and target"
            )

        incidents, evidence_by_id, audit_by_id = wait_for_investigation(
            settings, fingerprint
        )
        if len(incidents) != 1:
            raise VerificationFailure(
                f"Expected exactly one incident for fingerprint; found {len(incidents)}"
            )
        incident = incidents[0]
        audit = audit_by_id[incident.id]
        evidence = evidence_by_id[incident.id]
        real_tool_names = {
            "get_pod_status",
            "get_pod_logs",
            "get_events",
            "describe_workload",
            "get_rollout_history",
            "get_node_status",
            "query_prometheus",
        }
        successful_evidence = [
            item
            for item in evidence
            if item.tool_name in real_tool_names and item.error is None
        ]
        completed = any(
            event.detail_json.get("stage") == "evidence_investigation"
            and event.detail_json.get("status") == "completed"
            for event in audit
        )
        escalation = next(
            (
                event
                for event in reversed(audit)
                if event.to_state == IncidentState.ESCALATED.value
            ),
            None,
        )
        if not successful_evidence:
            raise VerificationFailure(
                "No successful evidence rows from the real read-only tool set"
            )
        if completed:
            gates[2].outcome = "PASS"
            gates[2].evidence = (
                f"{len(successful_evidence)} successful evidence rows from "
                f"{len({item.tool_name for item in successful_evidence})} real tools; "
                "investigation completion audit recorded within configured limits"
            )
        elif escalation is not None and escalation.detail_json.get("reason"):
            gates[2].outcome = "PASS"
            gates[2].evidence = (
                f"{len(successful_evidence)} successful evidence rows; "
                "investigation escalated with reason="
                f"{escalation.detail_json['reason']}"
            )
        else:
            raise VerificationFailure(
                "Investigation has neither a completion audit nor an escalation reason"
            )
    except VerificationFailure as error:
        failed = next((gate for gate in gates if gate.outcome == "FAIL"), gates[0])
        failed.evidence = str(error)
        for gate in gates:
            if gate.outcome == "FAIL" and gate.evidence == "Not run":
                gate.evidence = f"Blocked: {error}"
    except (
        ApiException,
        OSError,
        ValueError,
        SQLAlchemyError,
        subprocess.SubprocessError,
        ConfigException,
    ) as error:
        failed = next((gate for gate in gates if gate.outcome == "FAIL"), gates[0])
        failed.evidence = f"Unexpected verification error: {type(error).__name__}"
        for gate in gates:
            if gate.outcome == "FAIL" and gate.evidence == "Not run":
                gate.evidence = "Blocked by an earlier verification error"
    finally:
        if deployment_created:
            subprocess.run(
                [
                    *KUBECTL,
                    "delete",
                    "deployment",
                    deployment_name,
                    "--namespace",
                    "dev",
                    "--ignore-not-found=true",
                    "--wait=false",
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    print_report(gates)
    return 0 if all(gate.outcome == "PASS" for gate in gates) else 1


if __name__ == "__main__":
    raise SystemExit(main())
