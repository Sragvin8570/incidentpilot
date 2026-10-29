from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class IncidentListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    fingerprint: str
    alert_name: str
    severity: str | None = None
    namespace: str | None = None
    environment: str | None = None
    priority: str | None = None
    target_kind: str | None = None
    target_name: str | None = None
    state: str
    created_at: datetime
    resolved_at: datetime | None = None
    has_rca: bool = False
    recommended_action: str | None = None
    confidence: float | None = None
    risk_score: float | None = None


class IncidentListResponse(BaseModel):
    items: list[IncidentListItem]
    total: int
    limit: int
    offset: int


class IncidentStatsResponse(BaseModel):
    total: int
    active: int
    awaiting_approval: int
    executing: int
    verifying: int
    resolved: int
    escalated: int
    failed: int


class EvidenceItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tool_name: str
    args_json: dict[str, Any]
    result_text: str
    error: str | None = None
    created_at: datetime


class DecisionDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    recommended_action: str
    outcome: str
    confidence: float
    llm_confidence: float
    grounding_score: float
    claim_grounding: list[dict[str, Any]]
    runbook_score: float
    risk_score: float
    risk_reason: str
    reason: str
    created_at: datetime


class ExecutionDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    action_id: str
    status: str
    job_name: str | None = None
    target_snapshot: dict[str, Any] | None = None
    snapshot_hash: str | None = None
    applied_spec_hash: str | None = None
    patch_json: dict[str, Any] | None = None
    logs: str = ""
    error: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


class VerificationDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    checks: list[dict[str, Any]]
    effectiveness_score: float
    resolution_summary: str = ""
    started_at: datetime
    completed_at: datetime | None = None


class AuditEventDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    from_state: str | None = None
    to_state: str
    actor: str
    detail_json: dict[str, Any]
    created_at: datetime


class AlertOccurrenceDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    payload_json: dict[str, Any]
    received_at: datetime


class IncidentDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    fingerprint: str
    alert_name: str
    severity: str | None = None
    namespace: str | None = None
    environment: str | None = None
    priority: str | None = None
    target_kind: str | None = None
    target_name: str | None = None
    state: str
    created_at: datetime
    resolved_at: datetime | None = None
    rca_json: dict[str, Any] | None = None
    decision: DecisionDetail | None = None
    execution: ExecutionDetail | None = None
    verification: VerificationDetail | None = None
    evidence: list[EvidenceItem] = []
    audit_events: list[AuditEventDetail] = []
    occurrences: list[AlertOccurrenceDetail] = []
