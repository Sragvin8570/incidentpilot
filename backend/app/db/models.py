import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from sqlalchemy.types import Uuid

from app.db.base import Base


class IncidentState(str, enum.Enum):
    RECEIVED = "RECEIVED"
    UNSCOPED = "UNSCOPED"
    ENRICHED = "ENRICHED"
    INVESTIGATING = "INVESTIGATING"
    ANALYZED = "ANALYZED"
    READY = "READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        Index("ix_incidents_fingerprint", "fingerprint"),
        Index(
            "uq_incidents_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("state <> 'RESOLVED'"),
            sqlite_where=text("state <> 'RESOLVED'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(255))
    alert_name: Mapped[str] = mapped_column(String(255))
    severity: Mapped[str | None] = mapped_column(String(32))
    namespace: Mapped[str | None] = mapped_column(String(253))
    environment: Mapped[str | None] = mapped_column(String(32))
    priority: Mapped[str | None] = mapped_column(String(8))
    target_kind: Mapped[str | None] = mapped_column(String(64))
    target_name: Mapped[str | None] = mapped_column(String(253))
    state: Mapped[IncidentState] = mapped_column(
        Enum(IncidentState, name="incident_state"),
        default=IncidentState.RECEIVED,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rca_json: Mapped[dict | None] = mapped_column(JSON)

    occurrences: Mapped[list["AlertOccurrence"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    decision: Mapped["Decision | None"] = relationship(
        back_populates="incident", cascade="all, delete-orphan", uselist=False
    )
    execution: Mapped["Execution | None"] = relationship(
        back_populates="incident", cascade="all, delete-orphan", uselist=False
    )


class AlertOccurrence(Base):
    __tablename__ = "alert_occurrences"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    incident: Mapped[Incident | None] = relationship(back_populates="occurrences")


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(128))
    args_json: Mapped[dict] = mapped_column(JSON)
    result_text: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), unique=True
    )
    recommended_action: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column()
    llm_confidence: Mapped[float] = mapped_column()
    grounding_score: Mapped[float] = mapped_column()
    claim_grounding: Mapped[list[dict]] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )
    runbook_score: Mapped[float] = mapped_column()
    risk_score: Mapped[float] = mapped_column()
    risk_reason: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    incident: Mapped[Incident] = relationship(back_populates="decision")


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), unique=True, index=True
    )
    action_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="PREPARING")
    job_name: Mapped[str | None] = mapped_column(String(253))
    target_snapshot: Mapped[dict | None] = mapped_column(JSON)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    patch_json: Mapped[dict | None] = mapped_column(JSON)
    logs: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[Incident] = relationship(back_populates="execution")


class RunbookChunk(Base):
    __tablename__ = "runbook_chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    runbook_id: Mapped[str] = mapped_column(String(128), index=True)
    action_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(255))
    heading: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    embedding: Mapped[list[float]] = mapped_column(Vector(384))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(16))
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    incident: Mapped[Incident] = relationship(back_populates="audit_events")
