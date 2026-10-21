from contextlib import asynccontextmanager
import hmac

from celery.exceptions import CeleryError
from fastapi.concurrency import run_in_threadpool
from fastapi import FastAPI, Header, HTTPException
from kombu.exceptions import OperationalError as KombuOperationalError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.approvals.api import router as approvals_router
from app.auth.api import router as auth_router
from app.auth.service import seed_approver_user
from app.config import get_settings
from app.enrichment.service import EnrichmentUnavailable, KubernetesResourceResolver
from app.ingest.schemas import AlertmanagerResponse, AlertmanagerWebhook, IngestedAlert
from app.ingest.service import process_alert


async def queue_investigation(incident_id: str) -> None:
    from app.workers.celery_app import celery_app

    try:
        await run_in_threadpool(
            celery_app.send_task,
            "incidentpilot.investigate_incident",
            args=[incident_id],
            task_id=incident_id,
        )
    except (CeleryError, KombuOperationalError, RedisError, OSError) as error:
        raise HTTPException(
            status_code=503,
            detail="Investigation queue is temporarily unavailable",
        ) from error


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.database = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"timeout": 3},
    )
    app.state.session_factory = async_sessionmaker(
        app.state.database,
        expire_on_commit=False,
    )
    app.state.redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    app.state.resolver = KubernetesResourceResolver()
    try:
        await seed_approver_user(app.state.session_factory, settings)
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.database.dispose()


app = FastAPI(title="IncidentPilot API", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(approvals_router)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
async def readyz() -> dict[str, str]:
    database: AsyncEngine = app.state.database
    redis: Redis = app.state.redis
    failures: list[str] = []

    try:
        async with database.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        failures.append("database")

    try:
        await redis.ping()
    except RedisError:
        failures.append("redis")

    if failures:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "dependencies": failures},
        )
    return {"status": "ready"}


@app.post(
    "/api/v1/webhooks/alertmanager",
    response_model=AlertmanagerResponse,
    tags=["webhooks"],
)
async def alertmanager_webhook(
    webhook: AlertmanagerWebhook,
    authorization: str | None = Header(default=None),
) -> AlertmanagerResponse:
    settings = get_settings()
    token = settings.alertmanager_webhook_token
    if token is None or not token.get_secret_value():
        raise HTTPException(
            status_code=503,
            detail="Alertmanager webhook authentication is not configured",
        )
    expected = f"Bearer {token.get_secret_value()}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook credentials")

    results: list[IngestedAlert] = []
    async with app.state.session_factory() as session:
        for alert in webhook.alerts:
            status = alert.status or webhook.status
            try:
                incident, outcome = await process_alert(
                    session,
                    app.state.resolver,
                    status=status,
                    fingerprint=alert.fingerprint,
                    labels=alert.labels,
                    payload=alert.model_dump(mode="json"),
                )
            except EnrichmentUnavailable as error:
                raise HTTPException(
                    status_code=503,
                    detail="Kubernetes enrichment is temporarily unavailable",
                ) from error
            results.append(
                IngestedAlert(
                    fingerprint=alert.fingerprint,
                    incident_id=str(incident.id) if incident else None,
                    state=incident.state.value if incident else None,
                    outcome=outcome,
                )
            )
            if (
                incident is not None
                and incident.state.value == "ENRICHED"
                and outcome in {"created", "deduplicated"}
            ):
                await queue_investigation(str(incident.id))
    return AlertmanagerResponse(results=results)
