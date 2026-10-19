from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.actions import ActionCatalog
from app.db.models import Evidence

_ROOT = Path(__file__).resolve().parents[3]


class ConfidenceWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_confidence: float = Field(ge=0, le=1)
    grounding_score: float = Field(ge=0, le=1)
    runbook_score: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "ConfidenceWeights":
        if abs(
            self.llm_confidence + self.grounding_score + self.runbook_score - 1.0
        ) > 1e-9:
            raise ValueError("Confidence weights must sum to 1")
        return self


class ConfidencePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weights: ConfidenceWeights
    threshold: float = Field(ge=0, le=1)


class RiskRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1)
    requires_approval: bool = True
    when: dict[str, str] = Field(min_length=1)


class RiskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[RiskRule] = Field(min_length=1)


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence: ConfidencePolicy
    risk: RiskPolicy


@dataclass(frozen=True)
class DecisionResult:
    llm_confidence: float
    grounding_score: float
    claim_grounding: list[dict[str, Any]]
    runbook_score: float
    confidence: float
    risk_score: float
    risk_reason: str
    outcome: str
    reason: str


class DecisionPolicy:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    @classmethod
    def load(cls, path: Path | None = None) -> "DecisionPolicy":
        policy_path = path or _ROOT / "config" / "policy.yaml"
        raw: Any = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        return cls(PolicyConfig.model_validate(raw))

    def evaluate(
        self,
        *,
        recommended_action: str,
        llm_confidence: float,
        claims: list[dict[str, Any]],
        evidence: list[Evidence],
        environment: str | None,
        target_kind: str | None,
        catalog: ActionCatalog,
        target_name: str | None = None,
        validation_error: str | None = None,
    ) -> DecisionResult:
        claim_grounding = claim_grounding_details(
            claims, evidence, target_name=target_name
        )
        grounding = (
            sum(item["grounded"] for item in claim_grounding) / len(claim_grounding)
            if claim_grounding
            else 0.0
        )
        runbook = runbook_score(recommended_action, evidence)
        weights = self.config.confidence.weights
        confidence = (
            weights.llm_confidence * llm_confidence
            + weights.grounding_score * grounding
            + weights.runbook_score * runbook
        )
        normalized_environment = _normalize_environment(environment)
        action = catalog.actions.get(recommended_action)
        attributes = {
            "recommended_action": recommended_action,
            "environment": normalized_environment,
            "target_kind": target_kind or "unknown",
            "action_risk": action.risk_level if action is not None else "unknown",
        }
        matching_rules = [
            rule
            for rule in self.config.risk.rules
            if all(attributes.get(key) == value for key, value in rule.when.items())
        ]
        if not matching_rules:
            raise ValueError(
                "Policy has no matching risk rule for "
                f"environment={normalized_environment}, target_kind={attributes['target_kind']}, "
                f"action={recommended_action}"
            )
        selected_rule = max(matching_rules, key=lambda rule: rule.score)

        if recommended_action == "none":
            outcome = "ESCALATED"
            reason = "no_safe_action"
        elif validation_error is not None:
            outcome = "ESCALATED"
            reason = validation_error
        elif selected_rule.requires_approval:
            outcome = "AWAITING_APPROVAL"
            reason = selected_rule.reason
        elif confidence < self.config.confidence.threshold:
            outcome = "AWAITING_APPROVAL"
            reason = "confidence_below_threshold"
        else:
            outcome = "READY"
            reason = selected_rule.reason

        return DecisionResult(
            llm_confidence=llm_confidence,
            grounding_score=grounding,
            claim_grounding=claim_grounding,
            runbook_score=runbook,
            confidence=confidence,
            risk_score=selected_rule.score,
            risk_reason=selected_rule.reason,
            outcome=outcome,
            reason=reason,
        )


def grounding_score(
    claims: list[dict[str, Any]],
    evidence: list[Evidence],
    *,
    target_name: str | None = None,
) -> float:
    details = claim_grounding_details(claims, evidence, target_name=target_name)
    if not details:
        return 0.0
    return sum(item["grounded"] for item in details) / len(details)


def claim_grounding_details(
    claims: list[dict[str, Any]],
    evidence: list[Evidence],
    *,
    target_name: str | None = None,
) -> list[dict[str, Any]]:
    by_id = {str(row.id): row for row in evidence}
    details: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        cited_ids = [str(value) for value in claim.get("evidence_ids", [])]
        cited_rows = [by_id.get(evidence_id) for evidence_id in cited_ids]
        successful_ids = [
            evidence_id
            for evidence_id, row in zip(cited_ids, cited_rows, strict=True)
            if row is not None and row.error is None
        ]
        target_ids = [
            evidence_id
            for evidence_id, row in zip(cited_ids, cited_rows, strict=True)
            if row is not None
            and row.error is None
            and target_name
            and _evidence_references_target(row, target_name)
        ]
        all_successful = bool(cited_ids) and len(successful_ids) == len(cited_ids)
        grounded = all_successful and bool(target_ids)
        if not cited_ids:
            reason = "no_evidence_cited"
        elif not all_successful:
            reason = "cited_evidence_missing_or_failed"
        elif not target_name:
            reason = "incident_target_name_unavailable"
        elif not target_ids:
            reason = "cited_evidence_does_not_reference_target"
        else:
            reason = "grounded"
        details.append(
            {
                "claim_index": index,
                "claim": claim.get("claim", ""),
                "evidence_ids": cited_ids,
                "successful_evidence_ids": successful_ids,
                "target_evidence_ids": target_ids,
                "grounded": grounded,
                "reason": reason,
            }
        )
    return details


def _evidence_references_target(row: Evidence, target_name: str) -> bool:
    try:
        result: Any = yaml.safe_load(row.result_text)
    except (yaml.YAMLError, TypeError):
        result = None
    return _contains_resource_name(row.args_json, target_name) or _contains_resource_name(
        result, target_name
    )


def _contains_resource_name(value: Any, target_name: str) -> bool:
    identity_keys = {
        "name",
        "pod",
        "deployment",
        "statefulset",
        "daemonset",
        "workload",
        "target_name",
        "involved_object",
        "object",
        "owner_name",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in identity_keys:
                if isinstance(child, str) and child == target_name:
                    return True
                if isinstance(child, dict) and _contains_resource_name(child, target_name):
                    return True
            elif isinstance(child, (dict, list)):
                if _contains_resource_name(child, target_name):
                    return True
    elif isinstance(value, list):
        return any(_contains_resource_name(item, target_name) for item in value)
    return False


def runbook_score(recommended_action: str, evidence: list[Evidence]) -> float:
    if recommended_action == "none":
        return 0.0
    similarities: list[float] = []
    for row in evidence:
        if row.tool_name != "search_runbooks" or row.error is not None:
            continue
        try:
            result = yaml.safe_load(row.result_text)
        except (yaml.YAMLError, TypeError):
            continue
        if not isinstance(result, dict):
            continue
        for item in result.get("results", []):
            if (
                not isinstance(item, dict)
                or item.get("linked_action_id") != recommended_action
            ):
                continue
            similarity = item.get("similarity")
            if (
                isinstance(similarity, (int, float))
                and not isinstance(similarity, bool)
                and math.isfinite(similarity)
            ):
                similarities.append(float(similarity))
    return max(0.0, min(1.0, max(similarities, default=0.0)))


def _normalize_environment(environment: str | None) -> str:
    normalized = (environment or "").strip().lower()
    if normalized in {"dev", "development"}:
        return "dev"
    if normalized in {"staging", "stage"}:
        return "staging"
    if normalized in {"prod", "production", ""}:
        return "production"
    return "unknown"
