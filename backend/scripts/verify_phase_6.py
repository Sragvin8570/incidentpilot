from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.models import Decision, Execution, IncidentState  # noqa: E402
from app.workers.tasks import execute_ready_incident_task  # noqa: E402
from scripts.verify_phase_5 import (  # noqa: E402
    apply_manifest,
    delete_test_resource,
    ingest_current_runbooks,
    rollout_manifest,
    send_test_alert,
    statefulset_manifest,
    wait_for_crashloop,
    wait_for_successful_rollout,
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


async def read_rows(settings, fingerprint: str):
    incidents, _, _ = await incident_evidence(settings, fingerprint)
    if not incidents:
        return None, None, None
    incident = incidents[0]
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            decision = await session.scalar(
                select(Decision).where(Decision.incident_id == incident.id)
            )
            execution = await session.scalar(
                select(Execution).where(Execution.incident_id == incident.id)
            )
        return incident, decision, execution
    finally:
        await engine.dispose()


def wait_for_state(settings, fingerprint: str, expected: IncidentState):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        incident, decision, execution = asyncio.run(read_rows(settings, fingerprint))
        if incident is not None:
            if incident.state == expected and (
                expected != IncidentState.VERIFYING
                or (execution is not None and execution.status == "SUCCEEDED")
            ):
                return incident, decision, execution
            if incident.state in {
                IncidentState.ESCALATED,
                IncidentState.AWAITING_APPROVAL,
            } and incident.state != expected:
                raise VerificationFailure(
                    f"{fingerprint} reached unexpected state {incident.state.value}"
                )
        time.sleep(2)
    raise VerificationFailure(
        f"{fingerprint} did not reach {expected.value} before timeout"
    )


def kubectl_json(*args: str) -> dict:
    result = subprocess.run(
        [*KUBECTL, *args, "-o", "json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise VerificationFailure(f"kubectl {' '.join(args)} failed")
    return json.loads(result.stdout)


def jobs_for_incident(incident_id: UUID) -> list[dict]:
    result = kubectl_json(
        "get",
        "jobs",
        "--namespace",
        "dev",
        "--selector",
        f"incidentpilot.io/incident-id={incident_id}",
    )
    return result.get("items", [])


def delete_jobs_for_incident(namespace: str, incident_id: UUID) -> None:
    subprocess.run(
        [
            *KUBECTL,
            "delete",
            "jobs",
            "--namespace",
            namespace,
            "--selector",
            f"incidentpilot.io/incident-id={incident_id}",
            "--ignore-not-found=true",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _check_dev_rollback(settings, name: str) -> str:
    apply_manifest(rollout_manifest(name, "dev", failing=False), name)
    wait_for_successful_rollout("dev", name)
    healthy_template = kubectl_json(
        "get", "deployment", name, "--namespace", "dev"
    )["spec"]["template"]["spec"]
    apply_manifest(rollout_manifest(name, "dev", failing=True), name)
    wait_for_crashloop("dev", name)
    fingerprint = send_test_alert(
        settings,
        "dev",
        "Deployment",
        name,
        summary="Failing rollout has a healthy previous revision; roll back.",
    )
    incident = None
    try:
        wait_for_investigation(settings, fingerprint)
        incident, decision, execution = wait_for_state(
            settings, fingerprint, IncidentState.VERIFYING
        )
        if decision is None or decision.outcome != "READY":
            raise VerificationFailure("The dev rollback was not routed READY")
        if decision.recommended_action != "rollback_deployment":
            raise VerificationFailure("The dev failing rollout did not recommend rollback")
        if execution is None or execution.snapshot_hash is None or len(
            execution.snapshot_hash
        ) != 64:
            raise VerificationFailure("Execution snapshot/hash was not persisted")
        if len(jobs_for_incident(incident.id)) != 1:
            raise VerificationFailure("Expected exactly one executor Job for dev incident")

        rollout = subprocess.run(
            [
                *KUBECTL,
                "rollout",
                "status",
                f"deployment/{name}",
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
        if rollout.returncode:
            raise VerificationFailure("Rollback pods did not become Ready")
        deployment = kubectl_json("get", "deployment", name, "--namespace", "dev")
        current_template = deployment["spec"]["template"]["spec"]
        if current_template != healthy_template:
            raise VerificationFailure("Rollback did not restore the previous pod template")
        if deployment.get("status", {}).get("readyReplicas", 0) < 1:
            raise VerificationFailure("Previous-revision pods are not Ready")

        task_result = execute_ready_incident_task.run(str(incident.id))
        if task_result:
            raise VerificationFailure("Re-running execution task unexpectedly ran again")
        if len(jobs_for_incident(incident.id)) != 1:
            raise VerificationFailure("Re-running execution created another Job")
        return (
            f"state={incident.state.value}; action={decision.recommended_action}; "
            "previous template restored; readyReplicas>=1; one Job after task retry"
        )
    finally:
        if incident is None:
            incident, _, _ = asyncio.run(read_rows(settings, fingerprint))
        if incident is not None:
            delete_jobs_for_incident("dev", incident.id)


def _check_prod_statefulset(settings, name: str) -> str:
    apply_manifest(statefulset_manifest(name, "prod"), name)
    wait_for_crashloop("prod", name, minimum_restarts=2)
    fingerprint = send_test_alert(
        settings,
        "prod",
        "StatefulSet",
        name,
        summary="Transient startup dependency failure has recovered.",
    )
    incident = None
    try:
        wait_for_investigation(settings, fingerprint)
        incident, decision, execution = wait_for_state(
            settings, fingerprint, IncidentState.AWAITING_APPROVAL
        )
        if decision is None or decision.outcome != "AWAITING_APPROVAL":
            raise VerificationFailure("Production StatefulSet did not require approval")
        if execution is not None:
            raise VerificationFailure("An Execution row exists for approval-required incident")
        result = subprocess.run(
            [
                *KUBECTL,
                "get",
                "jobs",
                "--namespace",
                "prod",
                "--selector",
                f"incidentpilot.io/incident-id={incident.id}",
                "-o",
                "json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise VerificationFailure("Could not verify production Job count")
        if json.loads(result.stdout).get("items"):
            raise VerificationFailure("A Job was created for AWAITING_APPROVAL")
        return "state=AWAITING_APPROVAL; no Execution row; no Job"
    finally:
        if incident is None:
            incident, _, _ = asyncio.run(read_rows(settings, fingerprint))
        if incident is not None:
            delete_jobs_for_incident("prod", incident.id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live phase 6 Kubernetes execution verification."
    )
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    suffix = int(time.time())
    dev_name = f"incidentpilot-p6-dev-{suffix}"
    prod_name = f"incidentpilot-p6-prod-{suffix}"
    gates = [
        Gate(
            "Phase 6a",
            "Failing dev Deployment rolls back; pods Ready; retry creates no second Job.",
        ),
        Gate(
            "Phase 6b",
            "Production StatefulSet remains AWAITING_APPROVAL with no Job.",
        ),
    ]
    children: list[subprocess.Popen] = []
    try:
        if not args.confirm_live:
            raise VerificationFailure("Live resources require --confirm-live")
        settings = load_settings()
        if settings.llm_model != "gemini-3.5-flash-lite":
            raise VerificationFailure(
                "Phase 6 live verification requires LLM_MODEL=gemini-3.5-flash-lite"
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
            environment={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
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
            env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(worker)
        wait_for_celery_worker(worker)

        try:
            gates[0].evidence = _check_dev_rollback(settings, dev_name)
            gates[0].outcome = "PASS"
        except (VerificationFailure, OSError, ValueError, TypeError) as error:
            gates[0].evidence = str(error)
        try:
            gates[1].evidence = _check_prod_statefulset(settings, prod_name)
            gates[1].outcome = "PASS"
        except (VerificationFailure, OSError, ValueError, TypeError) as error:
            gates[1].evidence = str(error)
        print_report(gates)
        return 0 if all(gate.outcome == "PASS" for gate in gates) else 1
    except VerificationFailure as error:
        for gate in gates:
            if gate.evidence == "Not run":
                gate.evidence = str(error)
        print_report(gates)
        return 1
    finally:
        delete_test_resource("dev", "Deployment", dev_name)
        delete_test_resource("prod", "StatefulSet", prod_name)
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
