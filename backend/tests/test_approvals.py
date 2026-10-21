import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.security import hash_password, verify_password
from app.auth.service import seed_approver_user
from app.config import Settings, get_settings
from app.db.base import Base
from app.db.models import AuditEvent, Decision, Incident, IncidentState, User
from app.main import app
from app.approvals.service import expire_pending_approvals


def run_scenario(scenario) -> None:
    async def run() -> None:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        app.state.session_factory = factory
        try:
            await scenario(factory)
        finally:
            app.dependency_overrides.pop(get_settings, None)
            await engine.dispose()

    asyncio.run(run())


def test_password_hashes_are_salted_and_verified() -> None:
    first = hash_password("long-test-password-one")
    second = hash_password("long-test-password-one")

    assert first != second
    assert verify_password("long-test-password-one", first)
    assert not verify_password("wrong-password", first)
    assert not verify_password("long-test-password-one", "not-a-hash")


def test_approver_seed_is_idempotent_and_requires_env_credentials() -> None:
    async def scenario(factory) -> None:
        settings = Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://localhost/incidentpilot",
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr("test-only-secret-with-more-than-32-bytes"),
            approver_username="primary-approver",
            approver_password=SecretStr("long-test-password-one"),
        )

        first = await seed_approver_user(factory, settings)
        second = await seed_approver_user(factory, settings)
        assert first is not None
        assert second is not None
        assert first.id == second.id
        assert second.role == "approver"
        assert verify_password("long-test-password-one", second.password_hash)

        incomplete = settings.model_copy(update={"approver_password": None})
        try:
            await seed_approver_user(factory, incomplete)
        except ValueError as error:
            assert "configured together" in str(error)
        else:
            raise AssertionError("Incomplete approver env configuration was accepted")

    run_scenario(scenario)


async def create_pending_incident(
    factory,
    *,
    name: str,
    created_at: datetime | None = None,
) -> Incident:
    incident_id = uuid4()
    async with factory() as session:
        incident = Incident(
            id=incident_id,
            fingerprint=f"fingerprint-{incident_id}",
            alert_name="StatefulSetCrashLoop",
            namespace="prod",
            environment="production",
            target_kind="StatefulSet",
            target_name=name,
            state=IncidentState.AWAITING_APPROVAL,
        )
        decision = Decision(
            incident_id=incident_id,
            recommended_action="restart_workload",
            outcome="AWAITING_APPROVAL",
            confidence=0.9,
            llm_confidence=0.9,
            grounding_score=0.9,
            runbook_score=0.9,
            risk_score=0.9,
            risk_reason="statefulset_requires_approval",
            reason="statefulset_requires_approval",
        )
        if created_at is not None:
            decision.created_at = created_at
        session.add_all([incident, decision])
        await session.commit()
        return incident


def test_login_pending_approvals_role_enforcement_and_decision_audit(monkeypatch) -> None:
    async def scenario(factory) -> None:
        settings = Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://localhost/incidentpilot",
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr("test-only-secret-with-more-than-32-bytes"),
            approver_username="primary-approver",
            approver_password=SecretStr("long-test-password-one"),
        )
        app.dependency_overrides[get_settings] = lambda: settings
        approver = await seed_approver_user(factory, settings)
        assert approver is not None
        viewer = User(
            username="read-only-viewer",
            password_hash=hash_password("long-viewer-password-one"),
            role="viewer",
        )
        admin = User(
            username="incident-admin",
            password_hash=hash_password("long-admin-password-one"),
            role="admin",
        )
        async with factory() as session:
            session.add_all([viewer, admin])
            await session.commit()
        approved_incident = await create_pending_incident(
            factory, name="stateful-approved"
        )
        rejected_incident = await create_pending_incident(
            factory, name="stateful-rejected"
        )
        admin_incident = await create_pending_incident(
            factory, name="stateful-admin"
        )
        dispatched: list[tuple[str, list[str]]] = []

        async def fake_run_in_threadpool(function, *args, **kwargs):
            dispatched.append((args[0], kwargs["args"]))
            return None

        monkeypatch.setattr(
            "app.approvals.api.run_in_threadpool", fake_run_in_threadpool
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            approver_login = await client.post(
                "/api/v1/auth/login",
                json={
                    "username": "primary-approver",
                    "password": "long-test-password-one",
                },
            )
            viewer_login = await client.post(
                "/api/v1/auth/login",
                json={
                    "username": "read-only-viewer",
                    "password": "long-viewer-password-one",
                },
            )
            admin_login = await client.post(
                "/api/v1/auth/login",
                json={
                    "username": "incident-admin",
                    "password": "long-admin-password-one",
                },
            )
            invalid_login = await client.post(
                "/api/v1/auth/login",
                json={"username": "primary-approver", "password": "incorrect"},
            )
            assert approver_login.status_code == 200
            assert approver_login.json()["role"] == "approver"
            assert viewer_login.status_code == 200
            assert viewer_login.json()["role"] == "viewer"
            assert admin_login.status_code == 200
            assert admin_login.json()["role"] == "admin"
            assert invalid_login.status_code == 401

            approver_token = approver_login.json()["access_token"]
            viewer_token = viewer_login.json()["access_token"]
            admin_token = admin_login.json()["access_token"]
            pending = await client.get(
                "/api/v1/approvals/pending",
                headers={"Authorization": f"Bearer {approver_token}"},
            )
            assert pending.status_code == 200
            assert {item["incident_id"] for item in pending.json()} == {
                str(approved_incident.id),
                str(rejected_incident.id),
                str(admin_incident.id),
            }

            forbidden = await client.post(
                f"/api/v1/approvals/{approved_incident.id}/approve",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
            assert forbidden.status_code == 403

            approved = await client.post(
                f"/api/v1/approvals/{approved_incident.id}/approve",
                headers={"Authorization": f"Bearer {approver_token}"},
            )
            approved_retry = await client.post(
                f"/api/v1/approvals/{approved_incident.id}/approve",
                headers={"Authorization": f"Bearer {approver_token}"},
            )
            rejected = await client.post(
                f"/api/v1/approvals/{rejected_incident.id}/reject",
                headers={"Authorization": f"Bearer {approver_token}"},
                json={"comment": "Risk remains unacceptable."},
            )
            invalid_admin_approval = await client.post(
                f"/api/v1/approvals/{admin_incident.id}/approve",
                headers={"Authorization": f"******"},
            )
            assert approved.status_code == 200
            assert approved.json()["state"] == "APPROVED"
            assert approved_retry.status_code == 200
            assert rejected.status_code == 200
            assert rejected.json()["state"] == "ESCALATED"
            admin_approved = await client.post(
                f"/api/v1/approvals/{admin_incident.id}/approve",
                headers={"Authorization": "Bearer " + admin_token},
            )
            assert invalid_admin_approval.status_code == 401
            assert admin_approved.status_code == 200, admin_approved.text
            assert dispatched == [
                (
                    "incidentpilot.execute_ready_incident",
                    [str(approved_incident.id)],
                ),
                (
                    "incidentpilot.execute_ready_incident",
                    [str(approved_incident.id)],
                ),
                (
                    "incidentpilot.execute_ready_incident",
                    [str(admin_incident.id)],
                ),
            ]

        async with factory() as session:
            approved_row = await session.get(Incident, approved_incident.id)
            rejected_row = await session.get(Incident, rejected_incident.id)
            admin_event = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.incident_id == admin_incident.id,
                    AuditEvent.to_state == "APPROVED",
                )
            )
            approval_event = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.incident_id == approved_incident.id,
                    AuditEvent.to_state == "APPROVED",
                )
            )
            rejection_event = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.incident_id == rejected_incident.id,
                    AuditEvent.to_state == "REJECTED",
                )
            )
            assert approved_row.state == IncidentState.APPROVED
            assert rejected_row.state == IncidentState.ESCALATED
            assert approval_event.actor == "user"
            assert approval_event.detail_json["username"] == "primary-approver"
            approval_events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.incident_id == approved_incident.id,
                        AuditEvent.to_state == "APPROVED",
                    )
                )
            ).all()
            assert len(approval_events) == 1
            assert admin_event.detail_json["username"] == "incident-admin"
            assert admin_event.detail_json["role"] == "admin"
            assert rejection_event.actor == "user"
            assert rejection_event.detail_json["username"] == "primary-approver"
            assert (
                rejection_event.detail_json["comment"]
                == "Risk remains unacceptable."
            )

    run_scenario(scenario)


def test_expired_pending_approval_is_escalated_with_audit_events() -> None:
    async def scenario(factory) -> None:
        incident = await create_pending_incident(
            factory,
            name="expired-stateful",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=31),
        )

        expired = await expire_pending_approvals(factory, expiry_minutes=30)

        async with factory() as session:
            current = await session.get(Incident, incident.id)
            events = (
                await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.incident_id == incident.id)
                    .order_by(AuditEvent.id)
                )
            ).all()
            assert expired == 1
            assert current.state == IncidentState.ESCALATED
            assert [event.to_state for event in events] == ["EXPIRED", "ESCALATED"]
            assert all(
                event.detail_json["reason"] == "approval_expired"
                for event in events
            )

    run_scenario(scenario)
