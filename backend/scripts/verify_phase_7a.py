from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.auth.security import hash_password  # noqa: E402
from app.auth.service import seed_approver_user  # noqa: E402
from app.db.models import (  # noqa: E402
    Decision,
    Execution,
    Incident,
    IncidentState,
    User,
)
from scripts.install_executor_rbac import main as install_executor_rbac  # noqa: E402
from scripts.verify_phase_5 import (  # noqa: E402
    apply_manifest,
    delete_test_resource,
    ingest_current_runbooks,
    send_test_alert,
    statefulset_manifest,
    wait_for_crashloop,
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


def api_request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict | None = None,
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(API_URL + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except HTTPError as error:
        try:
            response_body = json.loads(error.read() or b"{}")
        except json.JSONDecodeError:
            response_body = {}
        return error.code, response_body
    except (URLError, OSError, TimeoutError, json.JSONDecodeError) as error:
        raise VerificationFailure(f"API request failed: {method} {path}") from error


async def read_approval_state(settings, fingerprint: str):
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
        incident, decision, execution = asyncio.run(
            read_approval_state(settings, fingerprint)
        )
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
                    f"Incident reached unexpected state {incident.state.value}"
                )
        time.sleep(2)
    raise VerificationFailure(f"Incident did not reach {expected.value}")


def kubectl_jobs(namespace: str, incident_id: UUID) -> list[dict]:
    result = subprocess.run(
        [
            *KUBECTL,
            "get",
            "jobs",
            "--namespace",
            namespace,
            "--selector",
            f"incidentpilot.io/incident-id={incident_id}",
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
        raise VerificationFailure("Could not inspect executor Jobs")
    return json.loads(result.stdout).get("items", [])


def delete_jobs(namespace: str, incident_id: UUID) -> None:
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


async def create_temporary_viewer(settings) -> tuple[UUID, str, str]:
    password = secrets.token_urlsafe(32)
    username = f"phase7a-viewer-{secrets.token_hex(6)}"
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            viewer = User(
                username=username,
                password_hash=hash_password(password),
                role="viewer",
            )
            session.add(viewer)
            await session.commit()
            return viewer.id, username, password
    finally:
        await engine.dispose()


async def remove_temporary_viewer(settings, user_id: UUID) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(delete(User).where(User.id == user_id))
    finally:
        await engine.dispose()


async def seed_configured_approver(settings) -> UUID | None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            user = await session.scalar(
                select(User).where(User.username == settings.approver_username)
            )
            existing_id = user.id if user is not None else None
        user = await seed_approver_user(factory, settings)
        return user.id if existing_id is None and user is not None else None
    finally:
        await engine.dispose()


async def remove_temporary_approver(settings, user_id: UUID) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(delete(User).where(User.id == user_id))
    finally:
        await engine.dispose()


def login(username: str, password: str) -> str:
    status_code, response = api_request(
        "POST",
        "/api/v1/auth/login",
        payload={"username": username, "password": password},
    )
    if status_code != 200 or not isinstance(response.get("access_token"), str):
        raise VerificationFailure("JWT login did not return an access token")
    return response["access_token"]


def check_approval_case(
    settings,
    *,
    name: str,
    action: str,
    approver_token: str,
    viewer_token: str,
) -> str:
    apply_manifest(statefulset_manifest(name, "prod"), name)
    wait_for_crashloop("prod", name, minimum_restarts=2)
    fingerprint = send_test_alert(
        settings,
        "prod",
        "StatefulSet",
        name,
        summary="A transient dependency startup failure has recovered.",
    )
    incident = None
    try:
        wait_for_investigation(settings, fingerprint)
        incident, decision, execution = wait_for_state(
            settings, fingerprint, IncidentState.AWAITING_APPROVAL
        )
        if decision is None or decision.outcome != "AWAITING_APPROVAL":
            raise VerificationFailure("Prod StatefulSet did not require approval")
        if decision.recommended_action != "restart_workload":
            raise VerificationFailure("Expected restart_workload recommendation")
        if execution is not None:
            raise VerificationFailure("An execution exists before the approval decision")

        pending_status, pending = api_request(
            "GET",
            "/api/v1/approvals/pending",
            token=approver_token,
        )
        if pending_status != 200 or not any(
            item["incident_id"] == str(incident.id) for item in pending
        ):
            raise VerificationFailure("Incident is missing from pending approvals")

        if action == "approve":
            viewer_status, _ = api_request(
                "POST",
                f"/api/v1/approvals/{incident.id}/approve",
                token=viewer_token,
            )
            if viewer_status != 403:
                raise VerificationFailure("Viewer role did not receive HTTP 403")
            approved_status, approved = api_request(
                "POST",
                f"/api/v1/approvals/{incident.id}/approve",
                token=approver_token,
            )
            if approved_status != 200 or approved.get("state") != "APPROVED":
                raise VerificationFailure("Approver API did not approve incident")
            incident, decision, execution = wait_for_state(
                settings, fingerprint, IncidentState.VERIFYING
            )
            jobs = kubectl_jobs("prod", incident.id)
            if len(jobs) != 1:
                raise VerificationFailure("Approval did not create exactly one Job")
            rollout = subprocess.run(
                [
                    *KUBECTL,
                    "rollout",
                    "status",
                    f"statefulset/{name}",
                    "--namespace",
                    "prod",
                    "--timeout=120s",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=135,
                check=False,
            )
            if rollout.returncode:
                raise VerificationFailure("Approved StatefulSet did not become Ready")
            if execution is None or execution.status != "SUCCEEDED":
                raise VerificationFailure("Execution row did not record Job success")
            return "approve -> Job succeeded -> VERIFYING; viewer approve -> HTTP 403"

        rejected_status, rejected = api_request(
            "POST",
            f"/api/v1/approvals/{incident.id}/reject",
            token=approver_token,
            payload={"comment": "Live phase 7a rejection test."},
        )
        if rejected_status != 200 or rejected.get("state") != "ESCALATED":
            raise VerificationFailure("Approver API did not reject and escalate")
        incident, _, execution = wait_for_state(
            settings, fingerprint, IncidentState.ESCALATED
        )
        if execution is not None or kubectl_jobs("prod", incident.id):
            raise VerificationFailure("Rejected incident created an Execution or Job")
        return "reject -> ESCALATED; no Execution row; no Job"
    finally:
        if incident is None:
            incident, _, _ = asyncio.run(read_approval_state(settings, fingerprint))
        if incident is not None:
            delete_jobs("prod", incident.id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live phase 7a authentication and approval verification."
    )
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    suffix = int(time.time())
    approved_name = f"incidentpilot-p7a-approve-{suffix}"
    rejected_name = f"incidentpilot-p7a-reject-{suffix}"
    gates = [
        Gate(
            "Phase 7a.1",
            "Prod StatefulSet approval executes via existing Job path; viewer gets 403.",
        ),
        Gate(
            "Phase 7a.2",
            "Prod StatefulSet rejection escalates without creating a Job.",
        ),
    ]
    children: list[subprocess.Popen] = []
    viewer_id = None
    approver_id = None
    settings = None
    try:
        if not args.confirm_live:
            raise VerificationFailure("Live resources require --confirm-live")
        settings = load_settings()
        if settings.approver_username is None or settings.approver_password is None:
            raise VerificationFailure(
                "Set APPROVER_USERNAME and APPROVER_PASSWORD in the environment or .env"
            )
        if settings.llm_model != "gemini-3.5-flash-lite":
            raise VerificationFailure(
                "Phase 7a live verification requires LLM_MODEL=gemini-3.5-flash-lite"
            )
        ensure_infrastructure(settings)
        approver_id = asyncio.run(seed_configured_approver(settings))
        if asyncio.run(ingest_current_runbooks(settings)) < 5:
            raise VerificationFailure("Runbook ingestion produced too few chunks")
        install_executor_rbac()

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

        viewer_id, viewer_username, viewer_password = asyncio.run(
            create_temporary_viewer(settings)
        )
        approver_token = login(
            settings.approver_username,
            settings.approver_password.get_secret_value(),
        )
        viewer_token = login(viewer_username, viewer_password)

        try:
            gates[0].evidence = check_approval_case(
                settings,
                name=approved_name,
                action="approve",
                approver_token=approver_token,
                viewer_token=viewer_token,
            )
            gates[0].outcome = "PASS"
        except (VerificationFailure, OSError, ValueError, TypeError) as error:
            gates[0].evidence = str(error)
        try:
            gates[1].evidence = check_approval_case(
                settings,
                name=rejected_name,
                action="reject",
                approver_token=approver_token,
                viewer_token=viewer_token,
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
        delete_test_resource("prod", "StatefulSet", approved_name)
        delete_test_resource("prod", "StatefulSet", rejected_name)
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        if settings is not None and viewer_id is not None:
            asyncio.run(remove_temporary_viewer(settings, viewer_id))
        if settings is not None and approver_id is not None:
            asyncio.run(remove_temporary_approver(settings, approver_id))


if __name__ == "__main__":
    raise SystemExit(main())
