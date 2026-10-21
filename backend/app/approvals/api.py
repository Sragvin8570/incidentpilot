from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from kombu.exceptions import OperationalError as KombuOperationalError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.exceptions import RedisError
from sqlalchemy import select

from app.approvals.service import (
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalNotFound,
    decide_approval,
    expire_pending_approvals,
)
from app.auth.security import get_current_user
from app.config import Settings, get_settings
from app.db.models import Decision, Incident, IncidentState, User

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


class RejectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str = Field(min_length=1, max_length=2000)

    @field_validator("comment")
    @classmethod
    def comment_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Rejection comment must not be blank")
        return value.strip()


async def require_approver(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if user.role not in {"approver", "admin"}:
        raise HTTPException(status_code=403, detail="Approver role required")
    return user


@router.get("/pending")
async def list_pending_approvals(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    settings: Settings = Depends(get_settings),
) -> list[dict]:
    del user
    await expire_pending_approvals(
        request.app.state.session_factory,
        expiry_minutes=settings.approval_expiry_minutes,
    )
    async with request.app.state.session_factory() as session:
        rows = (
            await session.execute(
                select(Incident, Decision)
                .join(Decision, Decision.incident_id == Incident.id)
                .where(Incident.state == IncidentState.AWAITING_APPROVAL)
                .order_by(Decision.created_at)
            )
        ).all()
        return [
            {
                "incident_id": str(incident.id),
                "alert_name": incident.alert_name,
                "severity": incident.severity,
                "namespace": incident.namespace,
                "target_kind": incident.target_kind,
                "target_name": incident.target_name,
                "recommended_action": decision.recommended_action,
                "reason": decision.reason,
                "created_at": decision.created_at.isoformat(),
                "expires_at": _approval_expiry(
                    decision.created_at,
                    settings.approval_expiry_minutes,
                ).isoformat(),
            }
            for incident, decision in rows
        ]


@router.post("/{incident_id}/approve")
async def approve_incident(
    incident_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_approver)],
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    await _decide(
        request,
        incident_id,
        user,
        approved=True,
        comment=None,
        settings=settings,
    )
    try:
        from app.workers.celery_app import celery_app

        await run_in_threadpool(
            celery_app.send_task,
            "incidentpilot.execute_ready_incident",
            args=[str(incident_id)],
            task_id=f"execute-{incident_id}",
        )
    except (CeleryError, KombuOperationalError, RedisError, OSError) as error:
        raise HTTPException(
            status_code=503,
            detail="Execution queue is temporarily unavailable",
        ) from error
    return {"incident_id": str(incident_id), "state": "APPROVED"}


@router.post("/{incident_id}/reject")
async def reject_incident(
    incident_id: UUID,
    payload: RejectionRequest,
    request: Request,
    user: Annotated[User, Depends(require_approver)],
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    await _decide(
        request,
        incident_id,
        user,
        approved=False,
        comment=payload.comment,
        settings=settings,
    )
    return {"incident_id": str(incident_id), "state": "ESCALATED"}


async def _decide(
    request: Request,
    incident_id: UUID,
    user: User,
    *,
    approved: bool,
    comment: str | None,
    settings: Settings,
) -> None:
    try:
        await decide_approval(
            request.app.state.session_factory,
            incident_id=incident_id,
            user=user,
            approved=approved,
            comment=comment,
            expiry_minutes=settings.approval_expiry_minutes,
        )
    except ApprovalNotFound as error:
        raise HTTPException(status_code=404, detail="Incident not found") from error
    except ApprovalAlreadyDecided as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Incident is not awaiting approval",
        ) from error
    except ApprovalExpired as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


def _approval_expiry(created_at: datetime, expiry_minutes: int) -> datetime:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at + timedelta(minutes=expiry_minutes)
