from fastapi.testclient import TestClient

from pydantic import SecretStr

from app.config import Settings
from app.main import app


def test_healthz_is_available_without_dependency_connections(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(
            database_url="postgresql+asyncpg://user:password@localhost/incidentpilot",
            redis_url="redis://localhost:6379/0",
            jwt_secret=SecretStr("test-only-secret"),
        ),
    )
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
