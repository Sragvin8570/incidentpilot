from __future__ import annotations

import argparse
import asyncio
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

from app.db.models import IncidentState, Verification  # noqa: E402
from scripts.verify_phase_5 import (  # noqa: E402
    apply_manifest,
    delete_test_resource,
    rollout_manifest,
    wait_for_crashloop,
    wait_for_successful_rollout,
)
from scripts.verify_phase_6 import (  # noqa: E402
    delete_jobs_for_incident,
    jobs_for_incident,
    kubectl_json,
    wait_for_state,
)
from scripts.verify_phases_1_3 import (  # noqa: E402
    API_URL,
    Gate,
    VerificationFailure,
    ensure_infrastructure,
    incident_evidence,
    load_settings,
    print_report,
    start_local_process,
    start_port_forward,
    wait_for_celery_worker,
    wait_for_crashloop_alert,
    wait_for_investigation,
)


async def read_verification(settings, incident_id: UUID):
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            return await session.scalar(
                select(Verification).where(
                    Verification.incident_id == incident_id
                )
            )
    finally:
        await engine.dispose()


def wait_for_verification(settings, fingerprint: str, expected: IncidentState):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        incidents, _, _ = asyncio.run(incident_evidence(settings, fingerprint))
        if incidents:
            incident = incidents[0]
            verification = asyncio.run(read_verification(settings, incident.id))
            if incident.state == expected and verification is not None:
                return incident, verification
            if incident.state in {
                IncidentState.RESOLVED,
                IncidentState.ESCALATED,
            } and incident.state != expected:
                raise VerificationFailure(
                    f"Incident reached unexpected state {incident.state.value}"
                )
        time.sleep(2)
    raise VerificationFailure(
        f"Incident did not complete verification as {expected.value}"
    )


def check_rollback_and_verify(settings, name: str) -> str:
    apply_manifest(rollout_manifest(name, "dev", failing=False), name)
    wait_for_successful_rollout("dev", name)
    healthy_template_spec = kubectl_json(
        "get", "deployment", name, "--namespace", "dev"
    )["spec"]["template"]["spec"]
    apply_manifest(rollout_manifest(name, "dev", failing=True), name)
    wait_for_crashloop("dev", name)
    alert = wait_for_crashloop_alert(name)
    fingerprint = alert["fingerprint"]
    incident = None
    try:
        wait_for_investigation(settings, fingerprint)
        incident, decision, _ = wait_for_state(
            settings, fingerprint, IncidentState.VERIFYING
        )
        if decision is None or decision.recommended_action != "rollback_deployment":
            raise VerificationFailure("Failing rollout did not recommend rollback")
        if len(jobs_for_incident(incident.id)) != 1:
            raise VerificationFailure("Expected exactly one rollback Job")
        incident, verification = wait_for_verification(
            settings, fingerprint, IncidentState.RESOLVED
        )
        if verification.effectiveness_score != 1.0 or any(
            not check["passed"] for check in verification.checks
        ):
            raise VerificationFailure(
                "Incident resolved without all four verification checks passing"
            )
        current_template_spec = kubectl_json(
            "get", "deployment", name, "--namespace", "dev"
        )["spec"]["template"]["spec"]
        if current_template_spec != healthy_template_spec:
            raise VerificationFailure("Resolved Deployment is not on its prior revision")
        return "one rollback Job; previous template restored; all checks passed; RESOLVED"
    finally:
        if incident is None:
            incidents, _, _ = asyncio.run(incident_evidence(settings, fingerprint))
            incident = incidents[0] if incidents else None
        if incident is not None:
            delete_jobs_for_incident("dev", incident.id)


def check_pipeline_revert_escalates(settings, name: str) -> str:
    apply_manifest(rollout_manifest(name, "dev", failing=False), name)
    wait_for_successful_rollout("dev", name)
    apply_manifest(rollout_manifest(name, "dev", failing=True), name)
    wait_for_crashloop("dev", name)
    alert = wait_for_crashloop_alert(name)
    fingerprint = alert["fingerprint"]
    incident = None
    try:
        wait_for_investigation(settings, fingerprint)
        incident, decision, execution = wait_for_state(
            settings, fingerprint, IncidentState.VERIFYING
        )
        if decision is None or decision.recommended_action != "rollback_deployment":
            raise VerificationFailure("Failing rollout did not recommend rollback")
        if execution is None or execution.status != "SUCCEEDED":
            raise VerificationFailure("Rollback Job did not succeed")
        apply_manifest(rollout_manifest(name, "dev", failing=True), name)
        incident, verification = wait_for_verification(
            settings, fingerprint, IncidentState.ESCALATED
        )
        failed_checks = {
            check["name"] for check in verification.checks if not check["passed"]
        }
        if not {"workload_health", "change_persistence"} <= failed_checks:
            raise VerificationFailure(
                "Pipeline revert did not fail workload health and persistence"
            )
        if verification.effectiveness_score >= 1.0:
            raise VerificationFailure("Pipeline revert was incorrectly marked effective")
        return (
            "pipeline revert after successful Job; workload_health and "
            "change_persistence failed; ESCALATED"
        )
    finally:
        if incident is None:
            incidents, _, _ = asyncio.run(incident_evidence(settings, fingerprint))
            incident = incidents[0] if incidents else None
        if incident is not None:
            delete_jobs_for_incident("dev", incident.id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live phase 8 closed-loop verification."
    )
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    suffix = int(time.time())
    resolved_name = f"incidentpilot-p8-resolve-{suffix}"
    reverted_name = f"incidentpilot-p8-revert-{suffix}"
    gates = [
        Gate(
            "Phase 8a",
            "Healthy previous revision passes all checks and resolves.",
        ),
        Gate(
            "Phase 8b",
            "Pipeline reversion fails health and persistence and escalates.",
        ),
    ]
    children: list[subprocess.Popen] = []
    try:
        if not args.confirm_live:
            raise VerificationFailure("Live resources require --confirm-live")
        settings = load_settings()
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
        api = start_local_process(
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
            gates[0].evidence = check_rollback_and_verify(settings, resolved_name)
            gates[0].outcome = "PASS"
        except (VerificationFailure, OSError, ValueError, TypeError) as error:
            gates[0].evidence = str(error)
        try:
            gates[1].evidence = check_pipeline_revert_escalates(
                settings, reverted_name
            )
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
        delete_test_resource("dev", "Deployment", resolved_name)
        delete_test_resource("dev", "Deployment", reverted_name)
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
