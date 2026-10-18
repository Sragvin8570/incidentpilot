import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.actions import ActionCatalog
from app.agent.rca import RCAValidationError, generate_rca
from app.agent.service import escalate_stuck_investigations
from app.agent.tools.runbooks import RunbookSearchTools, SimilarIncidentsArgs
from app.db.base import Base
from app.db.models import AuditEvent, Evidence, Incident, IncidentState, RunbookChunk
from app.runbooks.ingestion import ingest_runbooks, load_runbook_sections

ROOT = Path(__file__).resolve().parents[2]


class FakeCompletion:
    def __init__(self, content: str, refusal: str | None = None) -> None:
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content, refusal=refusal)
            )
        ]


class FakeCompletions:
    def __init__(self, responses: list[FakeCompletion]) -> None:
        self.responses = responses
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeRCAClient:
    def __init__(self, responses: list[FakeCompletion]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def rca_payload(evidence_id: str, **changes) -> dict:
    payload = {
        "root_cause": "A transient dependency outage caused startup failure.",
        "reasoning_steps": ["Previous container logs show a temporary connection failure."],
        "claims": [
            {
                "claim": "The dependency was temporarily unavailable.",
                "evidence_ids": [evidence_id],
            }
        ],
        "llm_confidence": 0.91,
        "recommended_action": "restart_workload",
        "action_parameters": {"namespace": "dev", "name": "demo"},
        "target": {"namespace": "dev", "kind": "Deployment", "name": "demo"},
        "alternatives_considered": ["rollback_deployment"],
    }
    payload.update(changes)
    return payload


def run_scenario(scenario) -> None:
    async def run() -> None:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await scenario(factory)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_action_catalog_matches_runbook_ids_risks_and_enforces_schema() -> None:
    catalog = ActionCatalog.load()
    sections = load_runbook_sections(ROOT / "runbooks", catalog)

    assert set(catalog.actions) == {
        "expand_pvc",
        "increase_memory_limit",
        "restart_workload",
        "rollback_deployment",
        "scale_deployment",
    }
    assert len(sections) > len(catalog.actions)
    assert catalog.actions["restart_workload"].risk_level == "low"
    assert catalog.actions["expand_pvc"].risk_level == "high"
    assert (
        catalog.validate_recommendation(
            "restart_workload",
            {"namespace": "dev", "name": "demo", "extra": True},
            "Deployment",
            [],
        )
        is not None
    )
    assert catalog.validate_recommendation("unknown_action", {}, "Deployment", []) == (
        "recommended_action_not_configured:unknown_action"
    )


def test_scale_action_enforces_relative_replica_bound() -> None:
    catalog = ActionCatalog.load()
    evidence = Evidence(
        id=uuid4(),
        incident_id=uuid4(),
        tool_name="describe_workload",
        args_json={},
        result_text=json.dumps(
            {"kind": "Deployment", "namespace": "dev", "name": "api", "replicas": 2}
        ),
    )

    assert (
        catalog.validate_recommendation(
            "scale_deployment",
            {"namespace": "dev", "name": "api", "replicas": 6},
            "Deployment",
            [evidence],
        )
        == "scale_action_exceeds_runbook_bounds"
    )
    assert (
        catalog.validate_recommendation(
            "scale_deployment",
            {"namespace": "dev", "name": "api", "replicas": 5},
            "Deployment",
            [evidence],
        )
        is None
    )


def test_rollback_requires_previous_revision_to_be_healthy() -> None:
    catalog = ActionCatalog.load()

    def evidence(ready_replicas: int) -> list[Evidence]:
        return [
            Evidence(
                id=uuid4(),
                incident_id=uuid4(),
                tool_name="get_rollout_history",
                args_json={"namespace": "dev", "name": "demo"},
                result_text=json.dumps(
                    {
                        "deployment": "demo",
                        "namespace": "dev",
                        "revisions": [
                            {"revision": "2", "ready_replicas": 0},
                            {"revision": "1", "ready_replicas": ready_replicas},
                        ],
                    }
                ),
            )
        ]

    parameters = {"namespace": "dev", "name": "demo", "to_revision": 1}
    assert (
        catalog.validate_recommendation(
            "rollback_deployment", parameters, "Deployment", evidence(0)
        )
        == "rollback_action_missing_previous_revision_evidence"
    )
    assert (
        catalog.validate_recommendation(
            "rollback_deployment", parameters, "Deployment", evidence(1)
        )
        is None
    )


def test_ingestion_replaces_chunks_and_stores_cpu_embedding_values() -> None:
    async def scenario(factory) -> None:
        class FakeEmbedder:
            def encode(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 383 + [1.0] for _ in texts]

        count = await ingest_runbooks(
            factory,
            FakeEmbedder(),
            runbook_dir=ROOT / "runbooks",
            catalog=ActionCatalog.load(),
        )
        async with factory() as session:
            chunks = (await session.scalars(select(RunbookChunk))).all()
        assert count == len(chunks)
        assert count > 5
        assert {chunk.action_id for chunk in chunks} == set(
            ActionCatalog.load().actions
        )
        assert len(chunks[0].embedding) == 384

    run_scenario(scenario)


def test_rollback_rca_must_cite_rollout_history() -> None:
    async def scenario(factory) -> None:
        incident_id = uuid4()
        log_evidence = Evidence(
            id=uuid4(),
            incident_id=incident_id,
            tool_name="get_pod_logs",
            args_json={},
            result_text="application exits with status 1",
        )
        history_evidence = Evidence(
            id=uuid4(),
            incident_id=incident_id,
            tool_name="get_rollout_history",
            args_json={"namespace": "dev", "name": "demo"},
            result_text=json.dumps(
                {
                    "deployment": "demo",
                    "namespace": "dev",
                    "revisions": [
                        {"revision": "2", "ready_replicas": 0},
                        {"revision": "1", "ready_replicas": 1},
                    ],
                }
            ),
        )
        runbook_evidence = Evidence(
            id=uuid4(),
            incident_id=incident_id,
            tool_name="search_runbooks",
            args_json={},
            result_text=json.dumps(
                {"results": [{"linked_action_id": "rollback_deployment"}]}
            ),
        )
        invalid = rca_payload(
            str(log_evidence.id),
            root_cause="A bad rollout introduced a deterministic application failure.",
            reasoning_steps=["The current revision fails while the prior revision was healthy."],
            recommended_action="rollback_deployment",
            action_parameters={
                "namespace": "dev",
                "name": "demo",
                "to_revision": 1,
            },
            alternatives_considered=["none"],
        )
        valid = {
            **invalid,
            "claims": [
                {
                    "claim": "Rollout history shows revision 1 is the healthy predecessor.",
                    "evidence_ids": [str(history_evidence.id)],
                }
            ],
        }
        client = FakeRCAClient(
            [
                FakeCompletion(json.dumps(invalid)),
                FakeCompletion(json.dumps(valid)),
            ]
        )
        rca = await generate_rca(
            client,
            model="gemini-test",
            incident_context={
                "namespace": "dev",
                "target": {"kind": "Deployment", "name": "demo"},
            },
            evidence=[log_evidence, history_evidence, runbook_evidence],
            catalog=ActionCatalog.load(),
        )
        assert rca.recommended_action == "rollback_deployment"
        assert rca.claims[0].evidence_ids == [history_evidence.id]
        assert len(client.chat.completions.requests) == 2
        assert "must cite get_rollout_history" in client.chat.completions.requests[1][
            "messages"
        ][-1]["content"]

    run_scenario(scenario)


def test_rca_structured_output_retries_invalid_json_once() -> None:
    async def scenario(factory) -> None:
        evidence = Evidence(
            id=uuid4(),
            incident_id=uuid4(),
            tool_name="get_pod_logs",
            args_json={},
            result_text="dependency temporarily unavailable",
        )
        runbook_evidence = Evidence(
            id=uuid4(),
            incident_id=evidence.incident_id,
            tool_name="search_runbooks",
            args_json={"query": "transient dependency"},
            result_text=json.dumps(
                {"results": [{"linked_action_id": "restart_workload"}]}
            ),
        )
        client = FakeRCAClient(
            [
                FakeCompletion("not json"),
                FakeCompletion(json.dumps(rca_payload(str(evidence.id)))),
            ]
        )
        rca = await generate_rca(
            client,
            model="gemini-test",
            incident_context={
                "namespace": "dev",
                "target": {"kind": "Deployment", "name": "demo"},
            },
            evidence=[evidence, runbook_evidence],
            catalog=ActionCatalog.load(),
        )
        requests = client.chat.completions.requests

        assert rca.recommended_action == "restart_workload"
        assert len(requests) == 2
        assert requests[0]["response_format"]["json_schema"]["strict"] is True
        assert "previous response was invalid" in requests[1]["messages"][-1]["content"]

    run_scenario(scenario)


def test_rca_retries_then_rejects_claims_citing_missing_evidence() -> None:
    async def scenario(factory) -> None:
        evidence = Evidence(
            id=uuid4(),
            incident_id=uuid4(),
            tool_name="get_pod_status",
            args_json={},
            result_text="CrashLoopBackOff",
        )
        runbook_evidence = Evidence(
            id=uuid4(),
            incident_id=evidence.incident_id,
            tool_name="search_runbooks",
            args_json={"query": "crash loop"},
            result_text=json.dumps(
                {"results": [{"linked_action_id": "restart_workload"}]}
            ),
        )
        client = FakeRCAClient(
            [
                FakeCompletion(
                    json.dumps(rca_payload(str(uuid4())))
                ),
                FakeCompletion(
                    json.dumps(rca_payload(str(uuid4())))
                ),
            ]
        )
        with pytest.raises(RCAValidationError, match="unknown evidence ids"):
            await generate_rca(
                client,
                model="gemini-test",
                incident_context={
                    "namespace": "dev",
                    "target": {"kind": "Deployment", "name": "demo"},
                },
                evidence=[evidence, runbook_evidence],
                catalog=ActionCatalog.load(),
            )
        assert len(client.chat.completions.requests) == 2

    run_scenario(scenario)


def test_watchdog_escalates_only_stale_investigations() -> None:
    async def scenario(factory) -> None:
        async with factory() as session:
            stale = Incident(
                fingerprint="stale-investigation",
                alert_name="TestAlert",
                state=IncidentState.INVESTIGATING,
            )
            fresh = Incident(
                fingerprint="fresh-investigation",
                alert_name="TestAlert",
                state=IncidentState.INVESTIGATING,
            )
            session.add_all([stale, fresh])
            await session.flush()
            now = datetime.now(timezone.utc)
            session.add_all(
                [
                    AuditEvent(
                        incident_id=stale.id,
                        from_state="ENRICHED",
                        to_state="INVESTIGATING",
                        actor="system",
                        detail_json={},
                        created_at=now - timedelta(minutes=10),
                    ),
                    AuditEvent(
                        incident_id=fresh.id,
                        from_state="ENRICHED",
                        to_state="INVESTIGATING",
                        actor="system",
                        detail_json={},
                        created_at=now,
                    ),
                ]
            )
            await session.commit()
            stale_id, fresh_id = stale.id, fresh.id

        count = await escalate_stuck_investigations(factory, timeout_seconds=300)
        async with factory() as session:
            stale = await session.get(Incident, stale_id)
            fresh = await session.get(Incident, fresh_id)
        assert count == 1
        assert stale is not None and stale.state == IncidentState.ESCALATED
        assert fresh is not None and fresh.state == IncidentState.INVESTIGATING

    run_scenario(scenario)


def test_find_similar_incidents_ranks_analyzed_rcas() -> None:
    async def scenario(factory) -> None:
        class FakeEmbedder:
            def encode(self, texts: list[str]) -> list[list[float]]:
                return [
                    [1.0, 0.0]
                    if "dependency" in text.lower()
                    else [0.0, 1.0]
                    for text in texts
                ]

        async with factory() as session:
            similar = Incident(
                fingerprint="similar",
                alert_name="SimilarAlert",
                state=IncidentState.ANALYZED,
                rca_json={"root_cause": "transient dependency outage"},
            )
            unrelated = Incident(
                fingerprint="unrelated",
                alert_name="OtherAlert",
                state=IncidentState.ANALYZED,
                rca_json={"root_cause": "database storage exhausted"},
            )
            session.add_all([similar, unrelated])
            await session.commit()

        toolset = RunbookSearchTools(factory, FakeEmbedder())
        result = await toolset.find_similar_incidents(
            SimilarIncidentsArgs(query="dependency outage", limit=2)
        )

        assert result["results"][0]["alert_name"] == "SimilarAlert"
        assert result["results"][0]["similarity"] == 1.0
        assert len(result["results"]) == 2

    run_scenario(scenario)
