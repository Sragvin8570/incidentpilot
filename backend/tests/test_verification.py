import asyncio
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.actions import ActionCatalog
from app.config import Settings
from app.db.base import Base
from app.db.models import (
    Decision,
    Execution,
    Incident,
    IncidentState,
    Verification,
)
from app.verification.service import (
    evaluate_alert_resolution,
    evaluate_change_persistence,
    evaluate_metric_recovery,
    evaluate_workload_health,
    recovery_threshold,
    render_recovery_query,
    verify_incident,
)
from app.agent.tools.runbooks import RunbookSearchTools, SimilarIncidentsArgs


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


async def no_sleep(seconds: float) -> None:
    assert seconds >= 0


class FakeKubernetes:
    def __init__(self, *, ready: int = 1, spec_hash: str = "applied") -> None:
        self.ready = ready
        self.spec_hash = spec_hash

    def read_workload_health(self, target_kind, name, namespace):
        assert (target_kind, name, namespace) == ("Deployment", "demo", "dev")
        return {
            "desired_replicas": 1,
            "ready_replicas": self.ready,
            "updated_replicas": 1,
            "generation": 1,
            "observed_generation": 1,
            "restart_counts": {"pod-uid/app": 0},
        }

    def read_current_spec_hash(self, target_kind, name, namespace):
        assert (target_kind, name, namespace) == ("Deployment", "demo", "dev")
        return self.spec_hash


class FakePrometheus:
    def __init__(self, values: list[float] | None = None) -> None:
        self.values = values or [1.0]
        self.query_text = None

    async def query(self, query: str) -> list[float]:
        self.query_text = query
        return self.values


class FakeAlertmanager:
    def __init__(self, firing: bool = False) -> None:
        self.firing = firing

    async def is_firing(self, fingerprint: str) -> bool:
        assert fingerprint.startswith("phase8-")
        return self.firing


def test_workload_health_requires_ready_replicas_and_stable_restarts() -> None:
    healthy = evaluate_workload_health(
        [
            {
                "desired_replicas": 2,
                "ready_replicas": 2,
                "updated_replicas": 2,
                "generation": 1,
                "observed_generation": 1,
                "restart_counts": {"pod/app": 1},
            },
            {
                "desired_replicas": 2,
                "ready_replicas": 2,
                "updated_replicas": 2,
                "generation": 1,
                "observed_generation": 1,
                "restart_counts": {"pod/app": 1},
            },
        ]
    )
    unhealthy = evaluate_workload_health(
        [
            {
                "desired_replicas": 2,
                "ready_replicas": 1,
                "updated_replicas": 1,
                "generation": 2,
                "observed_generation": 2,
                "restart_counts": {"pod/app": 1},
            },
            {
                "desired_replicas": 2,
                "ready_replicas": 1,
                "updated_replicas": 1,
                "generation": 2,
                "observed_generation": 2,
                "restart_counts": {"pod/app": 2},
            },
        ]
    )

    assert healthy.passed
    assert not unhealthy.passed
    assert unhealthy.observed["restart_increases"] == [
        {"container": "pod/app", "from": 1, "to": 2}
    ]


def test_alert_check_requires_original_fingerprint_to_stop_firing() -> None:
    assert evaluate_alert_resolution(False).passed
    assert not evaluate_alert_resolution(True).passed


def test_metric_check_obeys_recovery_threshold_operator() -> None:
    assert evaluate_metric_recovery([2.0], 2.0, ">=").passed
    assert not evaluate_metric_recovery([1.0], 2.0, ">=").passed
    assert evaluate_metric_recovery([10.0], 10.0, "<=").passed
    assert not evaluate_metric_recovery([], 1.0, ">=").passed


def test_persistence_check_compares_the_current_and_applied_spec_hash() -> None:
    assert evaluate_change_persistence("same", "same").passed
    assert not evaluate_change_persistence("reverted", "same").passed
    assert not evaluate_change_persistence(None, "same").passed


def test_recovery_query_and_action_thresholds_use_target_parameters() -> None:
    catalog = ActionCatalog.load()
    rollback = catalog.actions["rollback_deployment"]
    query = render_recovery_query(
        rollback,
        namespace="dev",
        name="checkout-api",
        parameters={},
    )
    memory_action = catalog.actions["increase_memory_limit"]
    memory_execution = Execution(
        patch_json={
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "api",
                                "resources": {"limits": {"memory": "805306368"}},
                            }
                        ]
                    }
                }
            }
        }
    )
    pvc_action = catalog.actions["expand_pvc"]
    pvc_execution = Execution(
        patch_json={"spec": {"resources": {"requests": {"storage": "2Gi"}}}}
    )

    assert 'namespace="dev",deployment="checkout-api"' in query
    assert recovery_threshold(memory_action, memory_execution, {"container": "api"}) == (
        805306368
    )
    assert recovery_threshold(pvc_action, pvc_execution, {}) == 2 * 1024**3
    assert all(
        action.stabilization_seconds >= 0
        and action.alert_grace_seconds >= 0
        and action.recovery_promql
        for action in catalog.actions.values()
    )


def test_find_similar_incidents_returns_completed_effectiveness_score() -> None:
    async def scenario(factory) -> None:
        class FakeEmbedder:
            def encode(self, texts: list[str]) -> list[list[float]]:
                return [[1.0, 0.0] for _ in texts]

        incident_id = uuid4()
        async with factory() as session:
            session.add(
                Incident(
                    id=incident_id,
                    fingerprint="verified-similar",
                    alert_name="VerifiedAlert",
                    state=IncidentState.RESOLVED,
                    rca_json={"root_cause": "dependency outage"},
                )
            )
            session.add(
                Verification(
                    incident_id=incident_id,
                    status="COMPLETED",
                    checks=[],
                    effectiveness_score=0.75,
                    resolution_summary="Checks passed.",
                )
            )
            await session.commit()

        toolset = RunbookSearchTools(factory, FakeEmbedder())
        result = await toolset.find_similar_incidents(
            SimilarIncidentsArgs(query="dependency outage", limit=1)
        )

        assert result["results"][0]["effectiveness_score"] == 0.75

    run_scenario(scenario)


def test_verification_resolves_after_all_checks_pass(monkeypatch) -> None:
    async def scenario(factory) -> None:
        incident_id = uuid4()
        async with factory() as session:
            incident = Incident(
                id=incident_id,
                fingerprint="phase8-pass",
                alert_name="TestAlert",
                namespace="dev",
                target_kind="Deployment",
                target_name="demo",
                state=IncidentState.VERIFYING,
                rca_json={"action_parameters": {"namespace": "dev", "name": "demo"}},
            )
            session.add(incident)
            session.add(
                Decision(
                    incident_id=incident_id,
                    recommended_action="rollback_deployment",
                    outcome="READY",
                    confidence=0.9,
                    llm_confidence=0.9,
                    grounding_score=0.9,
                    runbook_score=0.9,
                    risk_score=0.2,
                    risk_reason="test",
                    reason="test",
                )
            )
            session.add(
                Execution(
                    incident_id=incident_id,
                    action_id="rollback_deployment",
                    status="SUCCEEDED",
                    target_snapshot={"spec": {"replicas": 1}},
                    applied_spec_hash="applied",
                    patch_json={"spec": {"template": {}}},
                )
            )
            await session.commit()

        catalog = ActionCatalog.load()
        catalog.actions["rollback_deployment"] = catalog.actions[
            "rollback_deployment"
        ].model_copy(update={"stabilization_seconds": 0, "alert_grace_seconds": 0})
        monkeypatch.setattr(
            "app.verification.service.ActionCatalog.load", lambda: catalog
        )
        settings = Settings(
            database_url="sqlite+aiosqlite://",
            redis_url="redis://localhost:6379/0",
            jwt_secret="test-secret",
        )
        completed = await verify_incident(
            incident_id,
            factory,
            settings,
            kubernetes_client=FakeKubernetes(),
            prometheus_client=FakePrometheus(),
            alertmanager_client=FakeAlertmanager(),
            sleep=no_sleep,
        )

        async with factory() as session:
            current = await session.get(Incident, incident_id)
            verification = await session.scalar(
                select(Verification).where(Verification.incident_id == incident_id)
            )
            assert completed, (current.state, verification.status, verification.checks)
            assert current.state == IncidentState.RESOLVED
            assert current.resolved_at is not None
            assert verification.status == "COMPLETED"
            assert verification.effectiveness_score == 1.0
            assert [check["name"] for check in verification.checks] == [
                "workload_health",
                "alert_resolved",
                "metric_recovery",
                "change_persistence",
            ]

    run_scenario(scenario)


def test_verification_escalates_and_records_failed_health_and_persistence(
    monkeypatch,
) -> None:
    async def scenario(factory) -> None:
        incident_id = uuid4()
        async with factory() as session:
            session.add(
                Incident(
                    id=incident_id,
                    fingerprint="phase8-fail",
                    alert_name="TestAlert",
                    namespace="dev",
                    target_kind="Deployment",
                    target_name="demo",
                    state=IncidentState.VERIFYING,
                    rca_json={
                        "action_parameters": {"namespace": "dev", "name": "demo"}
                    },
                )
            )
            session.add(
                Decision(
                    incident_id=incident_id,
                    recommended_action="rollback_deployment",
                    outcome="READY",
                    confidence=0.9,
                    llm_confidence=0.9,
                    grounding_score=0.9,
                    runbook_score=0.9,
                    risk_score=0.2,
                    risk_reason="test",
                    reason="test",
                )
            )
            session.add(
                Execution(
                    incident_id=incident_id,
                    action_id="rollback_deployment",
                    status="SUCCEEDED",
                    target_snapshot={"spec": {"replicas": 1}},
                    applied_spec_hash="applied",
                    patch_json={"spec": {"template": {}}},
                )
            )
            await session.commit()

        catalog = ActionCatalog.load()
        catalog.actions["rollback_deployment"] = catalog.actions[
            "rollback_deployment"
        ].model_copy(update={"stabilization_seconds": 0, "alert_grace_seconds": 0})
        monkeypatch.setattr(
            "app.verification.service.ActionCatalog.load", lambda: catalog
        )
        settings = Settings(
            database_url="sqlite+aiosqlite://",
            redis_url="redis://localhost:6379/0",
            jwt_secret="test-secret",
        )
        completed = await verify_incident(
            incident_id,
            factory,
            settings,
            kubernetes_client=FakeKubernetes(ready=0, spec_hash="reverted"),
            prometheus_client=FakePrometheus(),
            alertmanager_client=FakeAlertmanager(),
            sleep=no_sleep,
        )

        async with factory() as session:
            current = await session.get(Incident, incident_id)
            verification = await session.scalar(
                select(Verification).where(Verification.incident_id == incident_id)
            )
            assert not completed, (current.state, verification.status)
            assert current.state == IncidentState.ESCALATED
            assert verification.effectiveness_score == 0.5
            assert [check["name"] for check in verification.checks if not check["passed"]] == [
                "workload_health",
                "change_persistence",
            ]
            assert "workload_health" in verification.resolution_summary

    run_scenario(scenario)
