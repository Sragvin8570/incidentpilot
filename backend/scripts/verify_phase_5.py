from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.agent.actions import ActionCatalog  # noqa: E402
from app.decision.policy import DecisionPolicy  # noqa: E402
from app.db.models import Decision, Incident, IncidentState  # noqa: E402
from app.runbooks.embeddings import LocalRunbookEmbedder  # noqa: E402
from app.runbooks.ingestion import ingest_runbooks  # noqa: E402
from scripts.verify_phase_4 import (  # noqa: E402
    _deployment_manifest,
)
from scripts.verify_phases_1_3 import (  # noqa: E402
    API_URL,
    KUBECTL,
    Gate,
    VerificationFailure,
    ensure_infrastructure,
    incident_evidence,
    load_settings,
    print_report,
    start_local_process,
    wait_for_celery_worker,
    wait_for_investigation,
)


def rollout_manifest(
    name: str, namespace: str, *, failing: bool, ambiguous: bool = False
) -> str:
    if not failing:
        command = "touch /tmp/ready; echo healthy revision; exec sleep 3600"
    elif ambiguous:
        command = (
            "echo 'service health check passed after retry' >&2; "
            "echo 'dependency probe returned connection refused' >&2; exit 1"
        )
    else:
        command = "echo failing rollout; exit 1"
    manifest = yaml.safe_load(
        _deployment_manifest(
            name,
            command,
        )
    )
    manifest["metadata"]["namespace"] = namespace
    manifest["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] = {
        "exec": {"command": ["sh", "-c", "test -f /tmp/ready"]},
        "periodSeconds": 1,
        "failureThreshold": 1,
    }
    return yaml.safe_dump(manifest, sort_keys=False)


def statefulset_manifest(name: str, namespace: str) -> str:
    command = (
        "count=0; if [ -f /state/start-count ]; then count=$(cat /state/start-count); fi; "
        "count=$((count + 1)); echo \"$count\" > /state/start-count; "
        "if [ \"$count\" -le 2 ]; then "
        "echo 'temporary dependency connection refused during startup' >&2; "
        "exit 42; fi; "
        "echo 'startup recovered'; exec sleep 3600"
    )
    manifest = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/name": name},
        },
        "spec": {
            "serviceName": name,
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": name}},
                "spec": {
                    "containers": [
                        {
                            "name": "test-app",
                            "image": "busybox:1.36",
                            "command": ["sh", "-c", command],
                            "volumeMounts": [
                                {"name": "start-state", "mountPath": "/state"}
                            ],
                        }
                    ],
                    "volumes": [{"name": "start-state", "emptyDir": {}}],
                },
            },
        },
    }
    return yaml.safe_dump(manifest, sort_keys=False)


def apply_manifest(manifest: str, name: str) -> None:
    result = subprocess.run(
        [*KUBECTL, "apply", "-f", "-"],
        cwd=ROOT,
        input=manifest,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise VerificationFailure(f"Could not create live test resource {name}")


def wait_for_successful_rollout(namespace: str, name: str) -> None:
    result = subprocess.run(
        [
            *KUBECTL,
            "rollout",
            "status",
            f"deployment/{name}",
            "--namespace",
            namespace,
            "--timeout=120s",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=135,
        check=False,
    )
    if result.returncode:
        raise VerificationFailure(f"Healthy revision did not become ready for {namespace}/{name}")


def delete_test_resource(namespace: str, kind: str, name: str) -> None:
    resource = "deployment" if kind == "Deployment" else "statefulset"
    subprocess.run(
        [
            *KUBECTL,
            "delete",
            resource,
            name,
            "--namespace",
            namespace,
            "--ignore-not-found=true",
            "--wait=false",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def wait_for_crashloop(
    namespace: str, name: str, *, minimum_restarts: int = 2
) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                *KUBECTL,
                "get",
                "pods",
                "--namespace",
                namespace,
                "-l",
                f"app.kubernetes.io/name={name}",
                "-o",
                "json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            try:
                pods = json.loads(result.stdout).get("items", [])
            except json.JSONDecodeError:
                pods = []
            for pod in pods:
                statuses = pod.get("status", {}).get("containerStatuses", [])
                if any(
                    status.get("restartCount", 0) >= minimum_restarts
                    for status in statuses
                ):
                    return
        time.sleep(3)
    raise VerificationFailure(f"{namespace}/{name} did not enter a crash loop")


def send_test_alert(
    settings: Settings,
    namespace: str,
    kind: str,
    name: str,
    *,
    summary: str,
) -> str:
    fingerprint = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "IncidentPilotPhase5PolicyTest",
                    "namespace": namespace,
                    "target_kind": kind,
                    "target_name": name,
                    "severity": "warning",
                    "team": "incidentpilot",
                },
                "annotations": {
                    "summary": summary
                },
                "startsAt": now,
                "fingerprint": fingerprint,
            }
        ],
    }
    token = settings.alertmanager_webhook_token
    if token is None or not token.get_secret_value():
        raise VerificationFailure("ALERTMANAGER_WEBHOOK_TOKEN is not configured")
    request = Request(
        API_URL + "/api/v1/webhooks/alertmanager",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token.get_secret_value()}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
    except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError) as error:
        raise VerificationFailure(
            "Could not submit the live phase 5 alert to the local API"
        ) from error
    if not result.get("results") or result["results"][0].get("outcome") != "created":
        raise VerificationFailure("The live phase 5 alert did not create an incident")
    return fingerprint


async def read_decision(
    settings: Settings, fingerprint: str
) -> tuple[Incident, Decision | None]:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        incidents, _, _ = await incident_evidence(settings, fingerprint)
        if len(incidents) != 1:
            raise VerificationFailure(
                f"Expected one incident for {fingerprint}; found {len(incidents)}"
            )
        async with factory() as session:
            decision = await session.scalar(
                select(Decision).where(Decision.incident_id == incidents[0].id)
            )
        return incidents[0], decision
    finally:
        await engine.dispose()


async def ingest_current_runbooks(settings: Settings) -> int:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        return await ingest_runbooks(
            async_sessionmaker(engine, expire_on_commit=False),
            LocalRunbookEmbedder(settings.runbook_embedding_model),
            runbook_dir=ROOT / "runbooks",
            catalog=ActionCatalog.load(),
        )
    finally:
        await engine.dispose()


def verify_case(
    settings: Settings,
    *,
    namespace: str,
    kind: str,
    name: str,
    expected_state: IncidentState | None,
    expected_outcome: str | None,
    expected_reason: str | None,
    expected_action: str | None,
    confidence_requirement: str,
    summary: str,
    minimum_restarts: int = 2,
) -> tuple[str, str, list[str]]:
    wait_for_crashloop(
        namespace, name, minimum_restarts=minimum_restarts
    )
    fingerprint = send_test_alert(
        settings, namespace, kind, name, summary=summary
    )
    wait_for_investigation(settings, fingerprint)
    incident, decision = asyncio.run(read_decision(settings, fingerprint))
    if decision is None:
        raise VerificationFailure("No persisted Decision row was created")
    threshold = DecisionPolicy.load().config.confidence.threshold
    errors: list[str] = []
    if expected_state is not None and incident.state != expected_state:
        errors.append(
            f"state {incident.state.value}; expected {expected_state.value}"
        )
    if incident.target_kind != kind or incident.target_name != name:
        errors.append("the enriched decision target did not match the test")
    if expected_outcome is not None and decision.outcome != expected_outcome:
        errors.append(
            f"route {decision.outcome}; expected {expected_outcome}"
        )
    if expected_reason is not None and decision.reason != expected_reason:
        errors.append(
            f"reason {decision.reason!r}; expected {expected_reason!r}"
        )
    if expected_action is not None and decision.recommended_action != expected_action:
        errors.append(
            f"action {decision.recommended_action!r}; expected {expected_action!r}"
        )
    if any(
        not 0 <= score <= 1
        for score in (
            decision.confidence,
            decision.risk_score,
            decision.llm_confidence,
            decision.grounding_score,
            decision.runbook_score,
        )
    ):
        errors.append("a persisted decision score is outside [0, 1]")
    if confidence_requirement == "high" and decision.confidence < threshold:
        errors.append(
            f"confidence {decision.confidence:.3f} is below {threshold:.3f}"
        )
    if (
        confidence_requirement == "low"
        and decision.confidence >= threshold
    ):
        errors.append(
            f"confidence {decision.confidence:.3f} is not below {threshold:.3f}"
        )
    if expected_outcome is None:
        if decision.recommended_action == "none":
            if decision.outcome != "ESCALATED" or incident.state != IncidentState.ESCALATED:
                errors.append("none action must escalate with the attached RCA")
            if decision.reason != "no_safe_action":
                errors.append(f"none action reason was {decision.reason!r}")
        else:
            if (
                decision.outcome != "AWAITING_APPROVAL"
                or incident.state != IncidentState.AWAITING_APPROVAL
            ):
                errors.append(
                    f"ambiguous action routed {decision.outcome}/{incident.state.value}; "
                    "expected AWAITING_APPROVAL or no_safe_action escalation"
                )
            if decision.reason != "confidence_below_threshold":
                errors.append(
                    f"ambiguous action approval reason was {decision.reason!r}"
                )
    if decision.outcome == "READY" and incident.state == IncidentState.EXECUTING:
        errors.append("Phase 5 executed a READY action")
    report = (
        f"incident={incident.id}; decision={decision.id}; state={incident.state.value}; "
        f"route={decision.outcome}; action={decision.recommended_action}; "
        f"confidence={decision.confidence:.3f} "
        f"(LLM={decision.llm_confidence:.3f}, grounding={decision.grounding_score:.3f}, "
        f"runbook={decision.runbook_score:.3f}); risk={decision.risk_score:.2f}; "
        f"reason={decision.reason}; risk_reason={decision.risk_reason}; "
        f"claim_grounding={json.dumps(decision.claim_grounding, sort_keys=True)}"
    )
    return report, decision.outcome, errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live phase 5 policy and decision-routing verification."
    )
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    suffix = int(time.time())
    cases = [
        {
            "label": "a",
            "description": "Dev failing rollout routes to READY.",
            "namespace": "dev",
            "kind": "Deployment",
            "prefix": "incidentpilot-p5-dev",
            "state": IncidentState.READY,
            "outcome": "READY",
            "reason": "development_medium_risk_auto_execute",
            "action": "rollback_deployment",
            "confidence": "high",
            "summary": "A clearly failing rollout in dev; prior revision was healthy.",
            "ambiguous": False,
        },
        {
            "label": "b",
            "description": "Production failing rollout routes to READY.",
            "namespace": "prod",
            "kind": "Deployment",
            "prefix": "incidentpilot-p5-prod",
            "state": IncidentState.READY,
            "outcome": "READY",
            "reason": "production_medium_risk_auto_execute",
            "action": "rollback_deployment",
            "confidence": "high",
            "summary": "A clearly failing rollout in production; prior revision was healthy.",
            "ambiguous": False,
        },
        {
            "label": "d",
            "description": "Production StatefulSet target requires approval.",
            "namespace": "prod",
            "kind": "StatefulSet",
            "prefix": "incidentpilot-p5-stateful",
            "state": IncidentState.AWAITING_APPROVAL,
            "outcome": "AWAITING_APPROVAL",
            "reason": "statefulset_requires_approval",
            "action": "restart_workload",
            "confidence": "any",
            "summary": (
                "StatefulSet had a transient startup dependency failure and has now recovered."
            ),
            "ambiguous": False,
            "minimum_restarts": 2,
            "runs": 1,
        },
        {
            "label": "e",
            "description": "Ambiguous production fault routes below threshold or escalates no_safe_action.",
            "namespace": "prod",
            "kind": "Deployment",
            "prefix": "incidentpilot-p5-ambiguous",
            "state": None,
            "outcome": None,
            "reason": None,
            "action": None,
            "confidence": "low",
            "summary": (
                "The deployment has conflicting startup evidence. A dependency probe "
                "failed after a health check passed on retry; determine whether evidence "
                "supports any safe remediation, otherwise recommend none."
            ),
            "ambiguous": True,
            "runs": 2,
        },
    ]
    gates: list[Gate] = []
    scheduled: list[tuple[dict, Gate, str]] = []
    for case in cases:
        for attempt in range(1, case.get("runs", 1) + 1):
            name = f"{case['prefix']}-{suffix}-{attempt}"
            gate = Gate(
                f"Phase 5{case['label']}.{attempt}",
                f"{case['description']} (live run {attempt}/{case.get('runs', 1)})",
            )
            gates.append(gate)
            scheduled.append((case, gate, name))
    children: list[subprocess.Popen] = []
    try:
        if not args.confirm_live:
            raise VerificationFailure(
                "Live test resources require --confirm-live"
            )
        settings = load_settings()
        if settings.llm_model != "gemini-3.5-flash-lite":
            raise VerificationFailure(
                "Phase 5 live verification requires LLM_MODEL=gemini-3.5-flash-lite"
            )
        ensure_infrastructure(settings)
        if asyncio.run(ingest_current_runbooks(settings)) < 5:
            raise VerificationFailure("Runbook ingestion produced too few chunks")
        api = start_local_process(
            API_URL + "/healthz",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            cwd=ROOT / "backend",
            environment={
                **os.environ,
                "PYTHONPATH": str(ROOT / "backend"),
                "LLM_MODEL": settings.llm_model,
            },
        )
        if api is not None:
            children.append(api)

        worker = subprocess.Popen(
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
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "backend"),
                "LLM_MODEL": settings.llm_model,
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(worker)
        wait_for_celery_worker(worker)

        ready = None
        for _ in range(30):
            try:
                with urlopen(API_URL + "/readyz", timeout=3) as response:
                    ready = json.loads(response.read())
            except (OSError, URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(1)
                continue
            break
        if not isinstance(ready, dict) or ready.get("status") != "ready":
            raise VerificationFailure("Backend readiness check failed")

        grouped_routes: dict[str, list[tuple[Gate, str]]] = {
            case["label"]: [] for case in cases
        }
        for case, gate, name in scheduled:
            namespace = case["namespace"]
            kind = case["kind"]
            try:
                if kind == "Deployment":
                    if not case["ambiguous"]:
                        apply_manifest(
                            rollout_manifest(name, namespace, failing=False), name
                        )
                        wait_for_successful_rollout(namespace, name)
                    apply_manifest(
                        rollout_manifest(
                            name,
                            namespace,
                            failing=True,
                            ambiguous=case["ambiguous"],
                        ),
                        name,
                    )
                else:
                    apply_manifest(statefulset_manifest(name, namespace), name)
                    wait_for_crashloop(
                        namespace,
                        name,
                        minimum_restarts=case.get("minimum_restarts", 2),
                    )
                report, route, errors = verify_case(
                    settings,
                    namespace=namespace,
                    kind=kind,
                    name=name,
                    expected_state=case["state"],
                    expected_outcome=case["outcome"],
                    expected_reason=case["reason"],
                    expected_action=case["action"],
                    confidence_requirement=case["confidence"],
                    summary=case["summary"],
                    minimum_restarts=case.get("minimum_restarts", 2),
                )
                gate.evidence = report
                if errors:
                    gate.evidence += "; FAIL: " + "; ".join(errors)
                else:
                    gate.outcome = "PASS"
                grouped_routes[case["label"]].append((gate, route))
            except (VerificationFailure, OSError, ValueError, TypeError) as error:
                gate.evidence = str(error)
            finally:
                delete_test_resource(namespace, kind, name)
        for case in cases:
            attempts = grouped_routes[case["label"]]
            if len(attempts) != case.get("runs", 1):
                for gate, _ in attempts:
                    gate.outcome = "FAIL"
                    gate.evidence += (
                        f"; FAIL: expected {case.get('runs', 1)} live run(s)"
                    )
        print_report(gates)
        return 0 if all(gate.outcome == "PASS" for gate in gates) else 1
    except VerificationFailure as error:
        for gate in gates:
            if gate.evidence == "Not run":
                gate.evidence = str(error)
        print_report(gates)
        return 1
    finally:
        for case, _, name in scheduled:
            namespace = case["namespace"]
            kind = case["kind"]
            delete_test_resource(namespace, kind, name)
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
