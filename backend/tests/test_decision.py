import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.actions import ActionCatalog, ActionDefinition
from app.db.models import Evidence
from app.decision.policy import DecisionPolicy, grounding_score, runbook_score


ROOT = Path(__file__).resolve().parents[2]


def evidence(
    tool_name: str,
    result: str,
    *,
    error: str | None = None,
) -> Evidence:
    return Evidence(
        id=uuid4(),
        incident_id=uuid4(),
        tool_name=tool_name,
        args_json={"namespace": "dev", "name": "demo"},
        result_text=result,
        error=error,
    )


def test_confidence_formula_uses_configured_weights() -> None:
    policy = DecisionPolicy.load()
    evidence_row = evidence("get_pod_logs", "application is healthy")
    runbook_row = evidence(
        "search_runbooks",
        json.dumps(
            {
                "results": [
                    {"linked_action_id": "restart_workload", "similarity": 0.75}
                ]
            }
        ),
    )

    result = policy.evaluate(
        recommended_action="restart_workload",
        llm_confidence=0.9,
        claims=[{"evidence_ids": [str(evidence_row.id)]}],
        evidence=[evidence_row, runbook_row],
        environment="dev",
        target_kind="Deployment",
        catalog=ActionCatalog.load(),
        target_name="demo",
    )

    assert result.llm_confidence == 0.9
    assert result.grounding_score == 1.0
    assert result.claim_grounding == [
        {
            "claim_index": 0,
            "claim": "",
            "evidence_ids": [str(evidence_row.id)],
            "successful_evidence_ids": [str(evidence_row.id)],
            "target_evidence_ids": [str(evidence_row.id)],
            "grounded": True,
            "reason": "grounded",
        }
    ]
    assert result.runbook_score == 0.75
    assert result.confidence == pytest.approx(0.88)
    assert result.outcome == "READY"


def test_every_risk_rule_selects_the_highest_matching_score() -> None:
    policy = DecisionPolicy.load()
    catalog = ActionCatalog.load()
    cases = [
        (
            "none",
            "dev",
            "Deployment",
            "no_safe_action",
            1.0,
            "ESCALATED",
        ),
        (
            "expand_pvc",
            "dev",
            "PersistentVolumeClaim",
            "expand_pvc_always_requires_approval",
            1.0,
            "AWAITING_APPROVAL",
        ),
        (
            "restart_workload",
            "unknown",
            "Deployment",
            "unknown_environment_requires_approval",
            0.95,
            "AWAITING_APPROVAL",
        ),
        (
            "restart_workload",
            "dev",
            "unknown",
            "unknown_target_requires_approval",
            0.95,
            "AWAITING_APPROVAL",
        ),
        (
            "test_high_risk_action",
            "dev",
            "Deployment",
            "high_risk_action_requires_approval",
            0.9,
            "AWAITING_APPROVAL",
        ),
        (
            "restart_workload",
            "production",
            "Deployment",
            "production_low_risk_auto_execute",
            0.3,
            "READY",
        ),
        (
            "rollback_deployment",
            "production",
            "Deployment",
            "production_medium_risk_auto_execute",
            0.5,
            "READY",
        ),
        (
            "restart_workload",
            "dev",
            "StatefulSet",
            "statefulset_requires_approval",
            0.9,
            "AWAITING_APPROVAL",
        ),
        (
            "test_high_risk_action",
            "production",
            "Deployment",
            "high_risk_action_requires_approval",
            0.9,
            "AWAITING_APPROVAL",
        ),
        (
            "restart_workload",
            "dev",
            "Node",
            "node_requires_approval",
            0.9,
            "AWAITING_APPROVAL",
        ),
        (
            "restart_workload",
            "dev",
            "PersistentVolumeClaim",
            "pvc_target_requires_approval",
            0.9,
            "AWAITING_APPROVAL",
        ),
        (
            "restart_workload",
            "staging",
            "Deployment",
            "staging_requires_approval",
            0.7,
            "AWAITING_APPROVAL",
        ),
        (
            "rollback_deployment",
            "dev",
            "Deployment",
            "development_medium_risk_auto_execute",
            0.45,
            "READY",
        ),
        (
            "restart_workload",
            "dev",
            "Deployment",
            "development_low_risk_auto_execute",
            0.3,
            "READY",
        ),
        (
            "unconfigured_test_action",
            "dev",
            "Deployment",
            "unconfigured_action_requires_approval",
            0.95,
            "AWAITING_APPROVAL",
        ),
    ]

    for action, environment, kind, reason, score, outcome in cases:
        grounding_row = evidence("get_pod_logs", "cause")
        runbook_row = evidence(
            "search_runbooks",
            json.dumps(
                {"results": [{"linked_action_id": action, "similarity": 1.0}]}
            ),
        )
        test_catalog = ActionCatalog(
            {
                **catalog.actions,
                "test_high_risk_action": ActionDefinition(
                    id="test_high_risk_action",
                    risk_level="high",
                    applies_to_kinds=["Deployment"],
                    parameters={"type": "object"},
                    bounds={},
                ),
            }
        )
        if action == "unconfigured_test_action":
            test_catalog.actions.pop(action, None)
        result = policy.evaluate(
            recommended_action=action,
            llm_confidence=1.0,
            claims=[{"evidence_ids": [str(grounding_row.id)]}],
            evidence=[grounding_row, runbook_row],
            environment=environment,
            target_kind=kind,
            catalog=test_catalog,
            target_name="demo",
        )
        assert result.risk_reason == reason, (action, environment, kind)
        assert result.risk_score == score, (action, environment, kind)
        assert result.outcome == outcome, (action, environment, kind)


def test_unlabeled_environment_uses_production_auto_execute_rule() -> None:
    log = evidence("get_pod_logs", "healthy predecessor")
    runbook = evidence(
        "search_runbooks",
        json.dumps(
            {"results": [{"linked_action_id": "rollback_deployment", "similarity": 0.9}]}
        ),
    )
    result = DecisionPolicy.load().evaluate(
        recommended_action="rollback_deployment",
        llm_confidence=1.0,
        claims=[{"evidence_ids": [str(log.id)]}],
        evidence=[log, runbook],
        environment=None,
        target_kind="Deployment",
        catalog=ActionCatalog.load(),
        target_name="demo",
    )

    assert result.risk_reason == "production_medium_risk_auto_execute"
    assert result.outcome == "READY"


def test_production_low_confidence_requires_approval() -> None:
    policy = DecisionPolicy.load()
    log = evidence("get_pod_logs", "uncertain failure")
    runbook = evidence(
        "search_runbooks",
        json.dumps(
            {"results": [{"linked_action_id": "rollback_deployment", "similarity": 0.8}]}
        ),
    )
    result = policy.evaluate(
        recommended_action="rollback_deployment",
        llm_confidence=0.2,
        claims=[{"evidence_ids": [str(log.id)]}],
        evidence=[log, runbook],
        environment="prod",
        target_kind="Deployment",
        catalog=ActionCatalog.load(),
        target_name="demo",
    )

    assert result.confidence == pytest.approx(0.76)
    assert result.risk_score == 0.5
    assert result.risk_reason == "production_medium_risk_auto_execute"
    assert result.reason == "confidence_below_threshold"
    assert result.outcome == "AWAITING_APPROVAL"


def test_confidence_below_threshold_routes_to_approval() -> None:
    policy = DecisionPolicy.load()
    action_evidence = evidence("get_pod_logs", "failure")

    result = policy.evaluate(
        recommended_action="restart_workload",
        llm_confidence=0.2,
        claims=[{"evidence_ids": []}],
        evidence=[action_evidence],
        environment="dev",
        target_kind="Deployment",
        catalog=ActionCatalog.load(),
        target_name="demo",
    )

    assert result.confidence < policy.config.confidence.threshold
    assert result.outcome == "AWAITING_APPROVAL"
    assert result.reason == "confidence_below_threshold"
    assert result.risk_reason == "development_low_risk_auto_execute"


def test_grounding_and_runbook_scores_require_successful_evidence() -> None:
    success = evidence("get_pod_logs", "found cause")
    failed = evidence("get_pod_status", "forbidden", error="forbidden")
    search = evidence(
        "search_runbooks",
        json.dumps(
            {
                "results": [
                    {"linked_action_id": "restart_workload", "similarity": 0.42},
                    {"linked_action_id": "restart_workload", "similarity": 0.76},
                    {"linked_action_id": "rollback_deployment", "similarity": 0.98},
                ]
            }
        ),
    )
    failed_search = evidence(
        "search_runbooks",
        json.dumps(
            {"results": [{"linked_action_id": "restart_workload", "similarity": 1.0}]}
        ),
        error="tool failed",
    )
    second_search = evidence(
        "search_runbooks",
        json.dumps(
            {"results": [{"linked_action_id": "restart_workload", "similarity": 0.88}]}
        ),
    )
    unrelated = evidence("get_pod_logs", "other incident", error=None)
    unrelated.args_json = {"namespace": "dev", "name": "other-workload"}

    assert grounding_score(
        [
            {"evidence_ids": [str(success.id)]},
            {"evidence_ids": [str(failed.id)]},
            {"evidence_ids": [str(unrelated.id)]},
        ],
        [success, failed],
        target_name="demo",
    ) == pytest.approx(1 / 3)
    assert grounding_score(
        [{"evidence_ids": [str(success.id), str(unrelated.id)]}],
        [success, unrelated],
        target_name="demo",
    ) == 1.0
    assert runbook_score("restart_workload", [search, second_search]) == 0.88
    assert runbook_score("restart_workload", [failed_search]) == 0.0
    assert runbook_score("unknown_action", [search]) == 0.0


def test_claim_grounding_accepts_owning_workload_reference() -> None:
    pod_evidence = evidence(
        "get_pod_logs",
        json.dumps(
            {
                "pod": "demo-7d4c9f6d8b-abcde",
                "workload": {"kind": "Deployment", "name": "demo"},
                "logs": "startup failed",
            }
        ),
    )
    unrelated_evidence = evidence(
        "get_pod_logs",
        json.dumps(
            {
                "pod": "other-7d4c9f6d8b-abcde",
                "workload": {"kind": "Deployment", "name": "other"},
                "logs": "startup failed",
            }
        ),
    )
    unrelated_evidence.args_json = {"namespace": "dev", "name": "other-pod"}

    assert grounding_score(
        [
            {"claim": "Target logs show a startup failure.", "evidence_ids": [str(pod_evidence.id)]},
            {"claim": "Another workload failed.", "evidence_ids": [str(unrelated_evidence.id)]},
        ],
        [pod_evidence, unrelated_evidence],
        target_name="demo",
    ) == 0.5


def test_policy_config_has_valid_confidence_weights() -> None:
    policy = DecisionPolicy.load()

    assert policy.config.confidence.weights.llm_confidence == 0.2
    assert policy.config.confidence.weights.grounding_score == 0.4
    assert policy.config.confidence.weights.runbook_score == 0.4
