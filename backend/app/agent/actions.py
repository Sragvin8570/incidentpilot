from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from app.db.models import Evidence


class ActionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    risk_level: Literal["low", "medium", "high"]
    applies_to_kinds: list[str]
    parameters: dict[str, Any]
    bounds: dict[str, Any]
    stabilization_seconds: int = Field(ge=0)
    alert_grace_seconds: int = Field(ge=0)
    recovery_promql: str = Field(min_length=1)
    recovery_threshold: Literal[
        "desired_replicas", "memory_limit_bytes", "requested_storage_bytes"
    ]
    recovery_threshold_factor: float = Field(default=1.0, gt=0, le=1)
    recovery_operator: Literal[">=", "<="]


class ActionCatalog:
    def __init__(self, actions: dict[str, ActionDefinition]) -> None:
        self.actions = actions

    @classmethod
    def load(cls, directory: Path | None = None) -> "ActionCatalog":
        action_dir = directory or Path(__file__).resolve().parents[3] / "config" / "actions"
        actions: dict[str, ActionDefinition] = {}
        for path in sorted(action_dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            action = ActionDefinition.model_validate(data)
            if path.stem != action.id or action.id in actions:
                raise ValueError(f"Action id does not match its unique filename: {path.name}")
            Draft202012Validator.check_schema(action.parameters)
            actions[action.id] = action
        return cls(actions)

    def summaries(self) -> list[dict[str, Any]]:
        return [
            {
                "id": action.id,
                "risk_level": action.risk_level,
                "applies_to_kinds": action.applies_to_kinds,
                "parameters": action.parameters,
                "bounds": action.bounds,
            }
            for action in self.actions.values()
        ]

    def validate_recommendation(
        self,
        action_id: str,
        parameters: dict[str, Any],
        target_kind: str,
        evidence: list[Evidence],
    ) -> str | None:
        action = self.actions.get(action_id)
        if action is None:
            return f"recommended_action_not_configured:{action_id}"
        if target_kind not in action.applies_to_kinds:
            return f"action_target_kind_not_allowed:{action_id}"
        errors = sorted(
            Draft202012Validator(action.parameters).iter_errors(parameters),
            key=lambda error: list(error.path),
        )
        if errors:
            return f"invalid_action_parameters:{errors[0].message}"

        if action_id == "scale_deployment":
            current = _find_current_replicas(evidence, parameters)
            if current is None:
                return "scale_action_missing_current_replica_evidence"
            if not current < parameters["replicas"] <= current + int(
                action.bounds["maximum_increase"]
            ):
                return "scale_action_exceeds_runbook_bounds"
        elif action_id == "rollback_deployment":
            previous = _find_previous_revision(evidence, parameters)
            if previous is None:
                return "rollback_action_missing_previous_revision_evidence"
            if (
                "to_revision" in parameters
                and parameters["to_revision"] != previous
            ):
                return "rollback_action_target_is_not_previous_revision"
        elif action_id == "expand_pvc":
            current_size = _find_current_pvc_size(evidence, parameters)
            if current_size is None:
                return "expand_pvc_missing_current_capacity_evidence"
            if not _pvc_allows_expansion(evidence, parameters):
                return "expand_pvc_storage_class_disallows_expansion"
            requested = _quantity_bytes(parameters["new_size"])
            if not current_size < requested <= current_size * float(
                action.bounds["maximum_current_size_multiplier"]
            ):
                return "expand_pvc_exceeds_runbook_bounds"
        return None


def _find_previous_revision(
    evidence: list[Evidence], parameters: dict[str, Any]
) -> int | None:
    for tool_name, payload in _evidence_payloads(evidence):
        if (
            tool_name == "get_rollout_history"
            and payload.get("deployment") == parameters["name"]
            and payload.get("namespace") == parameters["namespace"]
        ):
            revisions = payload.get("revisions")
            if isinstance(revisions, list) and len(revisions) >= 2:
                try:
                    previous = revisions[1]
                    if int(previous["ready_replicas"]) < 1:
                        return None
                    return int(previous["revision"])
                except (KeyError, TypeError, ValueError):
                    return None
    return None


def _evidence_payloads(evidence: list[Evidence]):
    for row in evidence:
        if row.error is not None:
            continue
        try:
            payload = json.loads(row.result_text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            yield row.tool_name, payload


def _find_current_replicas(
    evidence: list[Evidence], parameters: dict[str, Any]
) -> int | None:
    for tool_name, payload in _evidence_payloads(evidence):
        if (
            tool_name == "describe_workload"
            and payload.get("kind") == "Deployment"
            and payload.get("namespace") == parameters["namespace"]
            and payload.get("name") == parameters["name"]
            and isinstance(payload.get("replicas"), int)
        ):
            return payload["replicas"]
    return None


def _find_current_pvc_size(
    evidence: list[Evidence], parameters: dict[str, Any]
) -> int | None:
    for tool_name, payload in _evidence_payloads(evidence):
        if (
            tool_name == "get_pvc_status"
            and payload.get("namespace") == parameters["namespace"]
            and payload.get("name") == parameters["name"]
        ):
            capacity = payload.get("capacity")
            if isinstance(capacity, dict) and isinstance(
                capacity.get("storage"), str
            ):
                return _quantity_bytes(capacity["storage"])
    return None


def _pvc_allows_expansion(
    evidence: list[Evidence], parameters: dict[str, Any]
) -> bool:
    return any(
        tool_name == "get_pvc_status"
        and payload.get("namespace") == parameters["namespace"]
        and payload.get("name") == parameters["name"]
        and payload.get("allow_volume_expansion") is True
        for tool_name, payload in _evidence_payloads(evidence)
    )


def _quantity_bytes(quantity: str) -> int:
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
    for suffix, multiplier in units.items():
        if quantity.endswith(suffix) and quantity[: -len(suffix)].isdigit():
            return int(quantity[: -len(suffix)]) * multiplier
    raise ValueError("Unsupported Kubernetes storage quantity")
