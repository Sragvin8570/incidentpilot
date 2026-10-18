import asyncio

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.db.base import Base
from app.db.models import Incident
from app.enrichment.service import EnrichmentResult, WorkloadTarget
from app.main import app


class FakeResolver:
    async def resolve(self, namespace: str, kind: str, name: str) -> EnrichmentResult:
        return EnrichmentResult("dev", WorkloadTarget("Deployment", "checkout"))


def test_webhook_fails_closed_without_configured_authentication(monkeypatch) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@localhost/incidentpilot",
        redis_url="redis://localhost:6379/0",
        jwt_secret=SecretStr("test-only-secret"),
        alertmanager_webhook_token=None,
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/webhooks/alertmanager",
            json={
                "version": "4",
                "status": "firing",
                "alerts": [],
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Alertmanager webhook authentication is not configured"
    )


def test_webhook_rejects_wrong_bearer_token(monkeypatch) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@localhost/incidentpilot",
        redis_url="redis://localhost:6379/0",
        jwt_secret=SecretStr("test-only-secret"),
        alertmanager_webhook_token=SecretStr("expected-test-token"),
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/webhooks/alertmanager",
            headers={"Authorization": "Bearer wrong-token"},
            json={"version": "4", "status": "firing", "alerts": []},
        )

    assert response.status_code == 401


def test_authenticated_webhook_creates_and_deduplicates_incident(monkeypatch) -> None:
    async def scenario() -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://user:password@localhost/incidentpilot",
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr("test-only-secret"),
            alertmanager_webhook_token=SecretStr("expected-test-token"),
        )
        monkeypatch.setattr("app.main.get_settings", lambda: settings)
        queued_incidents: list[str] = []

        async def record_queue(incident_id: str) -> None:
            queued_incidents.append(incident_id)

        monkeypatch.setattr("app.main.queue_investigation", record_queue)
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        app.state.session_factory = async_sessionmaker(
            engine, expire_on_commit=False
        )
        app.state.resolver = FakeResolver()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                payload = {
                    "version": "4",
                    "status": "firing",
                    "alerts": [
                        {
                            "status": "firing",
                            "labels": {
                                "alertname": "PodCrashLooping",
                                "severity": "critical",
                                "namespace": "dev",
                                "pod": "checkout-abc123",
                            },
                            "fingerprint": "api-fingerprint",
                        }
                    ],
                }
                headers = {"Authorization": "Bearer expected-test-token"}
                first = await client.post(
                    "/api/v1/webhooks/alertmanager",
                    headers=headers,
                    json=payload,
                )
                second = await client.post(
                    "/api/v1/webhooks/alertmanager",
                    headers=headers,
                    json=payload,
                )

            assert first.status_code == 200
            assert first.json()["results"][0]["outcome"] == "created"
            assert first.json()["results"][0]["state"] == "ENRICHED"
            assert second.status_code == 200
            assert second.json()["results"][0]["outcome"] == "deduplicated"
            assert len(queued_incidents) == 2
            assert queued_incidents[0] == queued_incidents[1]
            async with app.state.session_factory() as session:
                assert await session.scalar(
                    select(func.count()).select_from(Incident)
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
