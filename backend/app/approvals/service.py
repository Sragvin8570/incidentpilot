from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Decision, Incident, IncidentState, User
from app.incidents.state_machine import transition_incident


class ApprovalNotFound(LookupError):
    pass


class ApprovalAlreadyDecided(ValueError):
    pass


class ApprovalExpired(ValueError):
    pass


async def expire_pending_approvals(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    expiry_minutes: int,
    now: datetime | None = None,
) -> int:
    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(minutes=expiry_minutes)
    expired_count = 0
    async with session_factory() as session:
        incidents = (
            await session.scalars(
                select(Incident)
                .join(Decision, Decision.incident_id == Incident.id)
                .where(
                    Incident.state == IncidentState.AWAITING_APPROVAL,
                    Decision.created_at <= cutoff,
                )
                .with_for_update()
            )
        ).all()
        for incident in incidents:
            await _expire_incident(session, incident, current_time)
            expired_count += 1
        await session.commit()
    return expired_count


async def decide_approval(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    incident_id: UUID,
    user: User,
    approved: bool,
    comment: str | None,
    expiry_minutes: int,
    now: datetime | None = None,
) -> IncidentState:
    current_time = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .with_for_update()
        )
        if incident is None:
            raise ApprovalNotFound(str(incident_id))
        if incident.state != IncidentState.AWAITING_APPROVAL:
            if approved and incident.state == IncidentState.APPROVED:
                return incident.state
            raise ApprovalAlreadyDecided(incident.state.value)
        decision = await session.scalar(
            select(Decision).where(Decision.incident_id == incident_id)
        )
        if decision is None:
            raise ApprovalAlreadyDecided("missing_decision")
        expires_at = _as_utc(decision.created_at) + timedelta(minutes=expiry_minutes)
        if current_time >= expires_at:
            await _expire_incident(session, incident, current_time)
            await session.commit()
            raise ApprovalExpired("Approval expired; incident was escalated")

        actor_detail = {
            "user_id": str(user.id),
            "username": user.username,
            "role": user.role,
            "comment": comment,
            "decision": "approved" if approved else "rejected",
        }
        next_state = (
            IncidentState.APPROVED if approved else IncidentState.REJECTED
        )
        await transition_incident(
            session,
            incident,
            next_state,
            actor="user",
            detail=actor_detail,
        )
        if not approved:
            await transition_incident(
                session,
                incident,
                IncidentState.ESCALATED,
                detail={"reason": "approval_rejected"},
            )
        await session.commit()
        return incident.state


async def _expire_incident(
    session: AsyncSession,
    incident: Incident,
    current_time: datetime,
) -> None:
    details = {
        "reason": "approval_expired",
        "expired_at": current_time.isoformat(),
    }
    await transition_incident(
        session,
        incident,
        IncidentState.EXPIRED,
        detail=details,
    )
    await transition_incident(
        session,
        incident,
        IncidentState.ESCALATED,
        detail=details,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
