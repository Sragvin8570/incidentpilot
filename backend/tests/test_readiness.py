import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.main import app, readyz


class HealthyDatabase:
    @asynccontextmanager
    async def connect(self):
        yield self

    async def execute(self, query):
        return 1


class HealthyRedis:
    async def ping(self):
        return True


class UnavailableDatabase:
    @asynccontextmanager
    async def connect(self):
        raise SQLAlchemyError("database unavailable")
        yield


class UnavailableRedis:
    async def ping(self):
        raise RedisError("redis unavailable")


def test_readyz_returns_ready_when_dependencies_are_available(monkeypatch) -> None:
    monkeypatch.setattr(app.state, "database", HealthyDatabase(), raising=False)
    monkeypatch.setattr(app.state, "redis", HealthyRedis(), raising=False)

    response = asyncio.run(readyz())

    assert response == {"status": "ready"}


def test_readyz_reports_unavailable_dependencies(monkeypatch) -> None:
    monkeypatch.setattr(app.state, "database", UnavailableDatabase(), raising=False)
    monkeypatch.setattr(app.state, "redis", UnavailableRedis(), raising=False)

    with pytest.raises(HTTPException) as error:
        asyncio.run(readyz())

    assert error.value.status_code == 503
    assert error.value.detail == {
        "status": "not_ready",
        "dependencies": ["database", "redis"],
    }

