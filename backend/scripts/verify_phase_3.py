from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.models import IncidentState  # noqa: E402
from scripts.verify_phases_1_3 import (  # noqa: E402
    API_URL,
    KUBECTL,
    PROMETHEUS_URL,
    Gate,
    VerificationFailure,
    database_is_ready,
    load_settings,
    print_report,
    request_json,
    start_local_process,
    start_port_forward,
    wait_for_celery_worker,
    wait_for_investigation,
    wait_for_incident,
)


def submit_test_alert(settings, fingerprint: str) -> dict:
    token = settings.alertmanager_webhook_token
    if token is None or not token.get_secret_value():
        raise VerificationFailure("ALERTMANAGER_WEBHOOK_TOKEN is not configured")
    payload = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": fingerprint,
                "labels": {
                    "alertname": "IncidentPilotPhase3Verification",
                    "namespace": "dev",
                    "target_kind": "Deployment",
                    "target_name": "incidentpilot-demo",
                    "severity": "warning",
                    "team": "incidentpilot",
                },
                "annotations": {
                    "summary": "Phase 3 live investigation verification"
                },
                "startsAt": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    request = Request(
        API_URL + "/api/v1/webhooks/alertmanager",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token.get_secret_value()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
    except (OSError, TimeoutError, json.JSONDecodeError) as error:
        raise VerificationFailure("Could not submit the phase 3 test alert") from error
    results = result.get("results", []) if isinstance(result, dict) else []
    matching = [
        item
        for item in results
        if item.get("fingerprint") == fingerprint and item.get("state") == "ENRICHED"
    ]
    if len(matching) != 1:
        raise VerificationFailure(
            "Phase 3 test alert did not create exactly one ENRICHED incident"
        )
    return matching[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live verification of the phase 3 LLM investigation only."
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Confirm that local services and the Kind environment are ready.",
    )
    args = parser.parse_args()
    gate = Gate(
        "Phase 3",
        "LLM chooses real read-only tools, collects evidence, and records investigation completion.",
    )
    children: list[subprocess.Popen] = []
    try:
        if not args.confirm_live:
            raise VerificationFailure(
                "Live changes are gated. Rerun with --confirm-live."
            )
        if not shutil.which("kubectl"):
            raise VerificationFailure("kubectl is not available")
        settings = load_settings()
        if not settings.llm_api_key or not settings.llm_api_key.get_secret_value():
            raise VerificationFailure("LLM_API_KEY is not configured")

        prometheus_forward = start_port_forward(
            "monitoring-kube-prometheus-prometheus", 9090, 9090
        )
        if prometheus_forward is not None:
            children.append(prometheus_forward)
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
            env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(worker)
        wait_for_celery_worker(worker)

        health = request_json(API_URL + "/healthz")
        ready = request_json(API_URL + "/readyz")
        if (
            not isinstance(health, dict)
            or health.get("status") != "ok"
            or not isinstance(ready, dict)
            or ready.get("status") != "ready"
        ):
            raise VerificationFailure("API, PostgreSQL, or Redis is not ready")
        asyncio.run(database_is_ready(settings))

        fingerprint = f"phase3-{uuid4().hex}"
        submit_test_alert(settings, fingerprint)
        incidents, _, _ = wait_for_incident(settings, fingerprint)
        if len(incidents) != 1:
            raise VerificationFailure(
                f"Expected one phase 3 incident; found {len(incidents)}"
            )
        incident = incidents[0]
        if (
            incident.state != IncidentState.ENRICHED
            or incident.namespace != "dev"
            or incident.target_kind != "Deployment"
            or incident.target_name != "incidentpilot-demo"
        ):
            raise VerificationFailure(
                "Phase 3 test incident has unexpected enrichment or target"
            )

        incidents, evidence_by_id, audit_by_id = wait_for_investigation(
            settings, fingerprint
        )
        if len(incidents) != 1:
            raise VerificationFailure("Phase 3 test incident was not retained")
        incident = incidents[0]
        evidence = evidence_by_id[incident.id]
        audit = audit_by_id[incident.id]
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
        if not successful_evidence:
            raise VerificationFailure(
                "Gemini produced no successful real-tool evidence"
            )
        if not completed:
            escalation = next(
                (
                    event
                    for event in reversed(audit)
                    if event.to_state == IncidentState.ESCALATED.value
                ),
                None,
            )
            reason = (
                escalation.detail_json.get("reason")
                if escalation is not None
                else "no completion audit recorded"
            )
            raise VerificationFailure(
                f"LLM investigation did not complete: {reason}"
            )

        gate.outcome = "PASS"
        gate.evidence = (
            f"model={settings.llm_model}; {len(successful_evidence)} successful "
            f"evidence rows from {len({item.tool_name for item in successful_evidence})} "
            "LLM-selected real tools; completion audit recorded"
        )
    except VerificationFailure as error:
        gate.evidence = str(error)
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    print_report([gate])
    return 0 if gate.outcome == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
