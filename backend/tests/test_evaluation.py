import asyncio
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.usage import record_llm_call
from app.db.base import Base
from app.db.models import Incident, IncidentState, LLMUsage
from eval.run import (
    EVALUATION_INVESTIGATION_TIMEOUT_SECONDS,
    _load_scenarios,
    _metric_row,
    _provider_error,
    _quota_error,
    _root_cause_score,
    _valid_run_keys,
)
from eval.summarize import render


def test_evaluation_scenarios_define_all_requested_cases() -> None:
    scenarios = _load_scenarios()

    assert set(scenarios) == {
        "exit1_no_fix",
        "bad_rollout_dev",
        "bad_rollout_prod",
        "statefulset_prod",
        "oom",
        "ambiguous_prod",
        "pipeline_revert",
        "prompt_injection_in_logs",
    }
    assert all(
        case.get("action")
        and case.get("final_state")
        and case.get("root_cause_concepts")
        for case in scenarios.values()
    )


def test_evaluation_investigation_timeout_allows_slow_model_responses() -> None:
    assert EVALUATION_INVESTIGATION_TIMEOUT_SECONDS > 300


def test_quota_error_recognizes_daily_quota_exhaustion() -> None:
    row = {
        "audits": [
            SimpleNamespace(detail_json={"reason": "daily_quota_exceeded"})
        ],
        "evidence": [],
    }

    assert _quota_error(row) == "{'reason': 'daily_quota_exceeded'}"


def test_provider_errors_cover_timeouts_and_server_errors() -> None:
    assert _provider_error(None, "TimeoutError: request timed out")
    assert _provider_error(
        {"audits": [SimpleNamespace(detail_json={"reason": "Error code: 503"})]},
    )


def test_metric_row_marks_provider_failure_invalid() -> None:
    row = {
        "incident_id": uuid4(),
        "created_at": None,
        "state": IncidentState.ESCALATED,
        "rca": None,
        "decision": None,
        "execution": None,
        "verification": None,
        "evidence": [],
        "audits": [
            SimpleNamespace(
                detail_json={"stage": "rca", "reason": "Error code: 503 UNAVAILABLE"}
            )
        ],
        "usage": [],
        "final_at": None,
    }

    metric = _metric_row(
        config="full",
        scenario_name="bad_rollout_dev",
        run_number=1,
        model="test-model",
        scenario={
            "action": "rollback_deployment",
            "final_state": "RESOLVED",
            "root_cause_concepts": [],
        },
        row=row,
    )

    assert metric["status"] == "INVALID"
    assert "503" in metric["invalid_reason"]


def test_root_cause_score_requires_each_concept_and_explains_matches() -> None:
    scenario = {
        "root_cause_concepts": [
            {"label": "rollout change", "any_of": ["rollout", "revision"]},
            {"label": "image change", "any_of": ["image", "new revision"]},
            {"label": "failure", "any_of": ["crash", "unhealthy"]},
        ]
    }

    correct, reason = _root_cause_score(
        "A rollout installed a new image which caused the pod to crash.",
        scenario,
    )
    incorrect, missing = _root_cause_score(
        "A rollout installed a new image.",
        scenario,
    )

    assert correct
    assert "matched: rollout change (rollout)" in reason
    assert not incorrect
    assert "missing: failure" in missing


def test_llm_call_usage_persists_each_call_and_tokens() -> None:
    async def scenario() -> None:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        incident_id = uuid4()
        async with factory() as session:
            session.add(
                Incident(
                    id=incident_id,
                    fingerprint="usage-test",
                    alert_name="UsageTest",
                    state=IncidentState.ENRICHED,
                )
            )
            await session.commit()
        await record_llm_call(
            factory,
            incident_id,
            SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45)
            ),
        )
        async with factory() as session:
            usage = (await session.scalars(select(LLMUsage))).all()
            assert len(usage) == 1
            assert usage[0].prompt_tokens == 123
            assert usage[0].completion_tokens == 45
        await engine.dispose()

    asyncio.run(scenario())


def test_metric_row_detects_hallucinations_and_false_resolution() -> None:
    incident = SimpleNamespace(
        id=uuid4(),
        created_at=None,
    )
    decision = SimpleNamespace(
        recommended_action="rollback_deployment",
        claim_grounding=[{"grounded": False}],
    )
    rca = {
        "root_cause": "The rollout created an unhealthy revision.",
        "claims": [{"evidence_ids": ["missing-evidence"]}],
    }
    row = {
        "incident_id": incident.id,
        "created_at": incident.created_at,
        "state": IncidentState.RESOLVED,
        "rca": rca,
        "decision": decision,
        "execution": None,
        "verification": None,
        "evidence": [],
        "audits": [],
        "usage": [],
        "final_at": None,
    }
    scenario = {
        "action": "rollback_deployment",
        "final_state": "ESCALATED",
        "root_cause_concepts": [
            {"label": "rollout", "any_of": ["rollout", "revision"]}
        ],
    }

    metric = _metric_row(
        config="no_verification",
        scenario_name="pipeline_revert",
        run_number=1,
        model="test-model",
        scenario=scenario,
        row=row,
    )

    assert metric["hallucinated_claims"] == 1
    assert not metric["rca_correct"]
    assert metric["false_resolution"]
    assert not metric["final_state_correct"]


def test_metric_row_does_not_count_supported_runbook_claim_as_hallucination() -> None:
    evidence = SimpleNamespace(id=uuid4(), tool_name="search_runbooks", error=None)
    decision = SimpleNamespace(
        recommended_action="rollback_deployment",
        claim_grounding=[
            {
                "grounded": False,
                "reason": "cited_evidence_does_not_reference_target",
            }
        ],
    )
    row = {
        "incident_id": uuid4(),
        "created_at": None,
        "state": IncidentState.READY,
        "rca": {
            "root_cause": "A bad rollout introduced the failing revision.",
            "claims": [
                {
                    "claim": "The runbook specifies rollback_deployment.",
                    "evidence_ids": [str(evidence.id)],
                }
            ],
        },
        "decision": decision,
        "execution": None,
        "verification": None,
        "evidence": [evidence],
        "audits": [],
        "usage": [],
        "final_at": None,
    }

    metric = _metric_row(
        config="full",
        scenario_name="bad_rollout_dev",
        run_number=1,
        model="test-model",
        scenario={
            "action": "rollback_deployment",
            "final_state": "RESOLVED",
            "root_cause_concepts": [
                {"label": "rollout", "any_of": ["rollout", "revision"]}
            ],
        },
        row=row,
    )

    assert metric["hallucinated_claims"] == 0
    assert metric["rca_correct"]


def test_metric_row_counts_no_decision_as_expected_none_action() -> None:
    row = {
        "incident_id": uuid4(),
        "created_at": None,
        "state": IncidentState.ESCALATED,
        "rca": None,
        "decision": None,
        "execution": None,
        "verification": None,
        "evidence": [],
        "audits": [],
        "usage": [],
        "final_at": None,
    }

    metric = _metric_row(
        config="full",
        scenario_name="exit1_no_fix",
        run_number=1,
        model="test-model",
        scenario={
            "action": "none",
            "final_state": "ESCALATED",
            "root_cause_concepts": [],
        },
        row=row,
    )

    assert metric["action"] is None
    assert metric["action_correct"]


def test_summary_has_one_markdown_table_per_configuration() -> None:
    rows = [
        {
            "config": config,
            "rca_correct": "True",
            "action_correct": "True",
            "final_state_correct": "False",
            "status": "VALID",
            "hallucinated_claims": "0",
            "time_to_final_state_seconds": "10",
            "llm_calls": "2",
            "llm_prompt_tokens": "100",
            "llm_completion_tokens": "30",
            "false_resolution": "False",
        }
        for config in ("full", "no_tools", "no_verification")
    ]

    report = render(rows)

    assert report.count("| Metric | Mean / rate |") == 3
    assert "## full (1 valid, 0 invalid)" in report
    assert "## no_tools (1 valid, 0 invalid)" in report
    assert "## no_verification (1 valid, 0 invalid)" in report
    assert "| RCA correct | 100.0% |" in report
    assert "| Action correct | 100.0% |" in report
    assert "| Final state correct | 0.0% |" in report
    assert "| False resolutions | 0.0% |" in report
    assert "LLM calls count successful model responses" in report


def test_summary_includes_configurations_without_completed_runs() -> None:
    report = render([])

    assert report.count("| Metric | Mean / rate |") == 3
    assert "## full (0 valid, 0 invalid)" in report
    assert "## no_tools (0 valid, 0 invalid)" in report
    assert "## no_verification (0 valid, 0 invalid)" in report


def test_summary_excludes_invalid_rows_and_reports_their_count() -> None:
    rows = [
        {
            "config": "full",
            "status": "VALID",
            "rca_correct": "True",
            "action_correct": "True",
            "final_state_correct": "True",
            "hallucinated_claims": "0",
            "false_resolution": "False",
        },
        {
            "config": "full",
            "status": "INVALID",
            "rca_correct": "False",
            "action_correct": "False",
            "final_state_correct": "False",
            "hallucinated_claims": "99",
            "false_resolution": "True",
        },
    ]

    report = render(rows)

    assert "## full (1 valid, 1 invalid)" in report
    assert "| Invalid runs | 1 |" in report
    assert "| RCA correct | 100.0% |" in report
    assert "| Hallucinated claims | 0.00 |" in report


def test_resume_keys_include_only_valid_csv_rows(tmp_path) -> None:
    path = tmp_path / "results.csv"
    path.write_text(
        "scenario,run,status\n"
        "bad_rollout_dev,1,VALID\n"
        "oom,2,INVALID\n"
        "oom,3,ERROR\n",
        encoding="utf-8",
    )

    assert _valid_run_keys(path) == {("bad_rollout_dev", 1)}
