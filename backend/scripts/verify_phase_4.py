from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.actions import ActionCatalog  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.models import IncidentState  # noqa: E402
from app.runbooks.embeddings import LocalRunbookEmbedder  # noqa: E402
from app.runbooks.ingestion import ingest_runbooks  # noqa: E402
from scripts.verify_phases_1_3 import (  # noqa: E402
    ALERTMANAGER_URL,
    API_URL,
    KUBECTL,
    Gate,
    VerificationFailure,
    ensure_infrastructure,
    load_settings,
    print_report,
    request_json,
    start_local_process,
    start_port_forward,
    wait_for_celery_worker,
    wait_for_crashloop_alert,
    wait_for_incident,
    wait_for_investigation,
)

def deterministic_failure_manifest(deployment_name: str) -> str:
    return _deployment_manifest(
        deployment_name,
        "echo deterministic application failure; exit 1",
    )


def rollout_manifest(deployment_name: str, *, failing: bool) -> str:
    command = (
        "echo bad rollout; exit 1"
        if failing
        else "touch /tmp/ready; echo healthy revision; exec sleep 3600"
    )
    return _deployment_manifest(deployment_name, command)


def _deployment_manifest(deployment_name: str, command: str) -> str:
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
            "revisionHistoryLimit": 5,
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            },
            "selector": {"matchLabels": {"app.kubernetes.io/name": deployment_name}},
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": deployment_name}},
                "spec": {
                    "containers": [
                        {
                            "name": "test-app",
                            "image": "busybox:1.36",
                            "command": ["sh", "-c", command],
                            "readinessProbe": {
                                "exec": {"command": ["sh", "-c", "test -f /tmp/ready"]},
                                "periodSeconds": 1,
                                "failureThreshold": 1,
                            },
                        }
                    ]
                },
            },
        },
    }
    return yaml.safe_dump(manifest, sort_keys=False)


def apply_manifest(manifest: str, description: str) -> None:
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
        raise VerificationFailure(f"Could not deploy {description}")


def wait_for_successful_rollout(deployment_name: str) -> None:
    result = subprocess.run(
        [
            *KUBECTL,
            "rollout",
            "status",
            f"deployment/{deployment_name}",
            "--namespace",
            "dev",
            "--timeout=120s",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=135,
        check=False,
    )
    if result.returncode:
        raise VerificationFailure(
            f"Healthy prerequisite revision did not become ready for {deployment_name}"
        )


def delete_test_workload(deployment_name: str) -> None:
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


def verify_live_scenario(
    settings: Settings,
    *,
    deployment_name: str,
    expected_state: IncidentState,
    expected_action: str,
    require_two_revisions: bool,
    require_rollout_claim: bool,
) -> str:
    alert = wait_for_crashloop_alert(deployment_name)
    fingerprint = str(alert.get("fingerprint", ""))
    if not fingerprint:
        raise VerificationFailure("Crash-loop alert has no fingerprint")
    incidents, _, _ = wait_for_incident(settings, fingerprint)
    if len(incidents) != 1:
        raise VerificationFailure(
            f"Expected exactly one incident for {deployment_name}; found {len(incidents)}"
        )
    incidents, evidence_by_id, audit_by_id = wait_for_investigation(
        settings, fingerprint
    )
    if len(incidents) != 1:
        raise VerificationFailure(f"Incident for {deployment_name} disappeared")
    incident = incidents[0]
    evidence = evidence_by_id[incident.id]
    rca = incident.rca_json
    if not isinstance(rca, dict):
        raise VerificationFailure("RCA was not attached to the incident")
    if incident.state != expected_state:
        audit = audit_by_id[incident.id]
        reason = next(
            (
                event.detail_json.get("reason")
                for event in reversed(audit)
                if event.to_state == IncidentState.ESCALATED.value
            ),
            "no escalation reason recorded",
        )
        raise VerificationFailure(
            f"Expected {expected_state.value}, got {incident.state.value}; {reason}"
        )
    if rca.get("recommended_action") != expected_action:
        raise VerificationFailure(
            f"Expected action {expected_action!r}, got {rca.get('recommended_action')!r}"
        )
    if incident.target_name != deployment_name or incident.namespace != "dev":
        raise VerificationFailure("Incident target does not match test workload")

    evidence_by_string_id = {str(row.id): row for row in evidence}
    claims = rca.get("claims", [])
    if not claims or any(not claim.get("evidence_ids") for claim in claims):
        raise VerificationFailure("RCA contains a claim without evidence citations")
    cited_ids = {
        evidence_id
        for claim in claims
        for evidence_id in claim.get("evidence_ids", [])
    }
    if not cited_ids.issubset(evidence_by_string_id):
        raise VerificationFailure("RCA cites evidence IDs that do not exist")

    successful = [row for row in evidence if row.error is None]
    if not any(row.tool_name == "search_runbooks" for row in successful):
        raise VerificationFailure("No successful runbook search evidence was stored")
    history_rows = [
        row for row in successful if row.tool_name == "get_rollout_history"
    ]
    if not history_rows:
        raise VerificationFailure("No successful get_rollout_history evidence was stored")
    history_row = history_rows[0]
    history = json.loads(history_row.result_text)
    revisions = history.get("revisions", [])
    if require_two_revisions:
        if len(revisions) < 2 or int(revisions[1].get("ready_replicas") or 0) < 1:
            raise VerificationFailure(
                "Rollout evidence does not show a previous healthy revision"
            )
    elif len(revisions) != 1:
        raise VerificationFailure(
            f"Expected one Deployment revision; rollout evidence has {len(revisions)}"
        )
    if require_rollout_claim and str(history_row.id) not in cited_ids:
        raise VerificationFailure(
            "RCA does not cite its get_rollout_history evidence"
        )
    if expected_action in ActionCatalog.load().actions:
        if expected_action not in {
            result.get("linked_action_id")
            for row in successful
            if row.tool_name == "search_runbooks"
            for result in json.loads(row.result_text).get("results", [])
        }:
            raise VerificationFailure(
                f"{expected_action} was not returned by runbook search"
            )
        error = ActionCatalog.load().validate_recommendation(
            expected_action,
            rca.get("action_parameters", {}),
            incident.target_kind or "",
            evidence,
        )
        if error:
            raise VerificationFailure(f"Action validation failed: {error}")
    elif expected_action == "none":
        escalation = next(
            (
                event.detail_json.get("reason")
                for event in reversed(audit_by_id[incident.id])
                if event.to_state == IncidentState.ESCALATED.value
            ),
            None,
        )
        if escalation != "recommended_action_not_configured:none":
            raise VerificationFailure(
                f"Expected safe escalation for action 'none'; got {escalation!r}"
            )

    return (
        f"incident={incident.id}; state={incident.state.value}; action={expected_action}; "
        f"claims={len(claims)}; citations={len(cited_ids)} existing evidence IDs; "
        f"successful tools={','.join(sorted({row.tool_name for row in successful}))}"
    )


async def ingest(settings: Settings) -> int:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live phase 4 runbook retrieval and RCA verification."
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Confirm that Docker Desktop, Kind, and local services are ready.",
    )
    args = parser.parse_args()
    gates = [
        Gate(
            "Phase 4a",
            "A deterministic exit-1 workload with one revision escalates with RCA action none and valid evidence citations.",
        ),
        Gate(
            "Phase 4b",
            "A healthy revision followed by a failing rollout recommends rollback_deployment and cites get_rollout_history.",
        ),
        Gate(
            "Phase 4 runbooks",
            "Runbooks are embedded locally and ingested into pgvector.",
        ),
    ]
    children: list[subprocess.Popen] = []
    suffix = int(time.time())
    exit1_name = f"incidentpilot-p4-exit1-{suffix}"
    rollout_name = f"incidentpilot-p4-rollout-{suffix}"
    deployment_names = [exit1_name, rollout_name]
    beat_schedule_file = (
        Path(tempfile.gettempdir())
        / f"incidentpilot-phase4-beat-{suffix}"
    )
    try:
        if not args.confirm_live:
            raise VerificationFailure(
                "Live changes are gated. Rerun with --confirm-live."
            )
        settings = load_settings()
        if settings.llm_model != "gemini-3.5-flash-lite":
            raise VerificationFailure(
                "Phase 4 verification requires LLM_MODEL=gemini-3.5-flash-lite"
            )
        ensure_infrastructure(settings)
        indexed_chunks = asyncio.run(ingest(settings))

        for service, local_port, remote_port in (
            ("monitoring-kube-prometheus-prometheus", 9090, 9090),
            ("monitoring-kube-prometheus-alertmanager", 9093, 9093),
        ):
            process = start_port_forward(service, local_port, remote_port)
            if process is not None:
                children.append(process)
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
            environment={
                **os.environ,
                "PYTHONPATH": str(ROOT / "backend"),
                "LLM_MODEL": "gemini-3.5-flash-lite",
            },
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
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "backend"),
                "LLM_MODEL": "gemini-3.5-flash-lite",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(worker)
        wait_for_celery_worker(worker)
        beat = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "--workdir",
                "backend",
                "-A",
                "app.workers.celery_app:celery_app",
                "beat",
                "--loglevel=WARNING",
                "--schedule",
                str(beat_schedule_file),
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "backend"),
                "LLM_MODEL": "gemini-3.5-flash-lite",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(beat)
        time.sleep(2)
        if beat.poll() is not None:
            raise VerificationFailure(
                f"Celery beat exited with status {beat.returncode}"
            )

        ready = request_json(API_URL + "/readyz")
        if not isinstance(ready, dict) or ready.get("status") != "ready":
            raise VerificationFailure("Backend readiness check failed")
        scenarios = [
            (
                gates[0],
                exit1_name,
                IncidentState.ESCALATED,
                "none",
                False,
                False,
            ),
            (
                gates[1],
                rollout_name,
                IncidentState.ANALYZED,
                "rollback_deployment",
                True,
                True,
            ),
        ]
        for (
            scenario_gate,
            deployment_name,
            expected_state,
            expected_action,
            require_two_revisions,
            require_rollout_claim,
        ) in scenarios:
            try:
                if deployment_name == exit1_name:
                    apply_manifest(
                        deterministic_failure_manifest(deployment_name),
                        "deterministic exit-1 workload",
                    )
                elif deployment_name == rollout_name:
                    apply_manifest(
                        rollout_manifest(deployment_name, failing=False),
                        "healthy prerequisite Deployment revision",
                    )
                    wait_for_successful_rollout(deployment_name)
                    apply_manifest(
                        rollout_manifest(deployment_name, failing=True),
                        "failing Deployment revision",
                    )
                scenario_gate.evidence = verify_live_scenario(
                    settings,
                    deployment_name=deployment_name,
                    expected_state=expected_state,
                    expected_action=expected_action,
                    require_two_revisions=require_two_revisions,
                    require_rollout_claim=require_rollout_claim,
                )
                scenario_gate.outcome = "PASS"
            except (VerificationFailure, OSError, ValueError, TypeError) as error:
                scenario_gate.evidence = str(error)
            finally:
                delete_test_workload(deployment_name)

        if indexed_chunks >= 5:
            gates[2].outcome = "PASS"
            gates[2].evidence = (
                f"{indexed_chunks} runbook chunks ingested with CPU embeddings"
            )
        else:
            gates[2].evidence = (
                f"Expected at least 5 ingested runbook chunks; found {indexed_chunks}"
            )
    except VerificationFailure as error:
        for gate in gates:
            if gate.evidence == "Not run":
                gate.evidence = str(error)
    except (OSError, ValueError, TypeError) as error:
        for gate in gates:
            if gate.evidence == "Not run":
                gate.evidence = (
                    f"Phase 4 verification error: {type(error).__name__}: {error}"
                )
    finally:
        for deployment_name in deployment_names:
            delete_test_workload(deployment_name)
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        beat_schedule_file.unlink(missing_ok=True)
    print_report(gates)
    return 0 if all(gate.outcome == "PASS" for gate in gates) else 1


if __name__ == "__main__":
    raise SystemExit(main())
