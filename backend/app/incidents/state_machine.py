from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEvent, Incident, IncidentState


class InvalidIncidentTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[IncidentState | None, set[IncidentState]] = {
    None: {IncidentState.RECEIVED},
    IncidentState.RECEIVED: {IncidentState.UNSCOPED, IncidentState.ENRICHED},
    IncidentState.ENRICHED: {
        IncidentState.INVESTIGATING,
        IncidentState.ESCALATED,
    },
    IncidentState.INVESTIGATING: {
        IncidentState.ANALYZED,
        IncidentState.ESCALATED,
    },
    IncidentState.ANALYZED: {
        IncidentState.EXECUTING,
        IncidentState.AWAITING_APPROVAL,
    },
    IncidentState.AWAITING_APPROVAL: {
        IncidentState.APPROVED,
        IncidentState.REJECTED,
        IncidentState.EXPIRED,
    },
    IncidentState.APPROVED: {IncidentState.EXECUTING},
    IncidentState.REJECTED: {IncidentState.ESCALATED},
    IncidentState.EXPIRED: {IncidentState.ESCALATED},
    IncidentState.EXECUTING: {
        IncidentState.VERIFYING,
        IncidentState.FAILED,
    },
    IncidentState.VERIFYING: {
        IncidentState.RESOLVED,
        IncidentState.ESCALATED,
    },
    IncidentState.FAILED: {IncidentState.ESCALATED},
    IncidentState.UNSCOPED: set(),
    IncidentState.RESOLVED: set(),
    IncidentState.ESCALATED: set(),
}


async def transition_incident(
    session: AsyncSession,
    incident: Incident,
    to_state: IncidentState,
    *,
    detail: dict | None = None,
    actor: str = "system",
) -> None:
    from_state = incident.state
    if to_state not in ALLOWED_TRANSITIONS[from_state]:
        raise InvalidIncidentTransition(
            f"Transition from {from_state.value} to {to_state.value} is not allowed"
        )
    incident.state = to_state
    session.add(
        AuditEvent(
            incident_id=incident.id,
            from_state=from_state.value,
            to_state=to_state.value,
            actor=actor,
            detail_json=detail or {},
        )
    )


async def record_incident_creation(
    session: AsyncSession, incident: Incident, *, detail: dict | None = None
) -> None:
    if IncidentState.RECEIVED not in ALLOWED_TRANSITIONS[None]:
        raise InvalidIncidentTransition("RECEIVED is not a valid initial state")
    session.add(
        AuditEvent(
            incident_id=incident.id,
            from_state=None,
            to_state=IncidentState.RECEIVED.value,
            actor="system",
            detail_json=detail or {},
        )
    )

