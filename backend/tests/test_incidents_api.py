import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.security import create_access_token, hash_password
from app.config import Settings, get_settings
from app.db.base import Base
from app.db.models import (
    AuditEvent,
    Decision,
    Evidence,
    Execution,
    Incident,
    IncidentState,
    User,
    Verification,
)
from app.main import app


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


def test_incidents_api_crud_and_stats() -> None:
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

        user = User(
            id=uuid4(),
            username="test-viewer",
            password_hash=hash_password("test-password-viewer"),
            role="viewer",
        )
        incident_id = uuid4()
        incident = Incident(
            id=incident_id,
            fingerprint="fp-test-1",
            alert_name="KubePodCrashLooping",
            severity="critical",
            namespace="dev",
            environment="dev",
            priority="P1",
            target_kind="Deployment",
            target_name="demo-app",
            state=IncidentState.INVESTIGATING,
            rca_json={"root_cause": "OOMKilled", "confidence": 0.95},
        )
        decision = Decision(
            id=uuid4(),
            incident_id=incident_id,
            recommended_action="increase_memory_limit",
            outcome="READY",
            confidence=0.88,
            llm_confidence=0.9,
            grounding_score=0.85,
            claim_grounding=[],
            runbook_score=0.92,
            risk_score=0.3,
            risk_reason="low_risk_in_dev",
            reason="Automated policy routing",
        )
        evidence = Evidence(
            id=uuid4(),
            incident_id=incident_id,
            tool_name="get_pod_logs",
            args_json={"pod_name": "demo-app-xxx"},
            result_text="Out of memory: Kill process 1 (python)",
            error=None,
        )
        execution = Execution(
            id=uuid4(),
            incident_id=incident_id,
            action_id="increase_memory_limit",
            status="COMPLETED",
            job_name="exec-increase-memory",
            target_snapshot={"spec": {"replicas": 1}},
            snapshot_hash="hash123",
            applied_spec_hash="hash456",
            patch_json={"spec": {"template": {}}},
            logs="Job executed successfully",
        )
        verification = Verification(
            id=uuid4(),
            incident_id=incident_id,
            status="PASSED",
            checks=[{"name": "pods_ready", "passed": True}],
            effectiveness_score=1.0,
            resolution_summary="Memory limit increased, pods stable.",
        )
        audit = AuditEvent(
            incident_id=incident_id,
            from_state="ENRICHED",
            to_state="INVESTIGATING",
            actor="system",
            detail_json={"agent": "investigator"},
        )

        # Another incident in AWAITING_APPROVAL state
        incident2_id = uuid4()
        incident2 = Incident(
            id=incident2_id,
            fingerprint="fp-test-2",
            alert_name="StatefulSetCrashLoop",
            severity="warning",
            namespace="prod",
            environment="production",
            priority="P2",
            target_kind="StatefulSet",
            target_name="db-cluster",
            state=IncidentState.AWAITING_APPROVAL,
        )

        async with factory() as session:
            session.add_all([user, incident, decision, evidence, execution, verification, audit, incident2])
            await session.commit()

        token = create_access_token(user, settings)
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # 1. Unauthenticated request must fail with 401
            resp = await client.get("/api/v1/incidents")
            assert resp.status_code == 401

            # 2. Authenticated list incidents
            resp = await client.get("/api/v1/incidents", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 2
            assert len(data["items"]) == 2
            item1 = next(item for item in data["items"] if item["id"] == str(incident_id))
            assert item1["alert_name"] == "KubePodCrashLooping"
            assert item1["has_rca"] is True
            assert item1["recommended_action"] == "increase_memory_limit"
            assert item1["confidence"] == 0.88

            # 3. Filter by state
            resp = await client.get("/api/v1/incidents?state=AWAITING_APPROVAL", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["items"][0]["id"] == str(incident2_id)

            # 4. Search by alert_name
            resp = await client.get("/api/v1/incidents?search=KubePod", headers=headers)
            assert resp.status_code == 200
            assert resp.json()["total"] == 1

            # 5. Stats endpoint
            resp = await client.get("/api/v1/incidents/stats", headers=headers)
            assert resp.status_code == 200
            stats = resp.json()
            assert stats["total"] == 2
            assert stats["awaiting_approval"] == 1
            assert stats["active"] == 2

            # 6. Incident detail endpoint
            resp = await client.get(f"/api/v1/incidents/{incident_id}", headers=headers)
            assert resp.status_code == 200
            detail = resp.json()
            assert detail["id"] == str(incident_id)
            assert detail["alert_name"] == "KubePodCrashLooping"
            assert detail["rca_json"]["root_cause"] == "OOMKilled"
            assert detail["decision"]["recommended_action"] == "increase_memory_limit"
            assert detail["execution"]["job_name"] == "exec-increase-memory"
            assert detail["verification"]["status"] == "PASSED"
            assert len(detail["evidence"]) == 1
            assert detail["evidence"][0]["tool_name"] == "get_pod_logs"
            assert len(detail["audit_events"]) == 1
            assert detail["audit_events"][0]["to_state"] == "INVESTIGATING"

            # 7. Non-existent incident detail returns 404
            resp = await client.get(f"/api/v1/incidents/{uuid4()}", headers=headers)
            assert resp.status_code == 404

    run_scenario(scenario)
