from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["firing", "resolved"] | None = None
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    generatorURL: str | None = None
    fingerprint: str = Field(min_length=1, max_length=255)

    @field_validator("labels")
    @classmethod
    def require_alert_name(cls, labels: dict[str, str]) -> dict[str, str]:
        if not labels.get("alertname", "").strip():
            raise ValueError("Alert labels must include a non-empty alertname")
        return labels


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str | None = None
    status: Literal["firing", "resolved"]
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)


class IngestedAlert(BaseModel):
    fingerprint: str
    incident_id: str | None
    state: str | None
    outcome: Literal["created", "deduplicated", "unscoped", "resolved_recorded"]


class AlertmanagerResponse(BaseModel):
    results: list[IngestedAlert]

