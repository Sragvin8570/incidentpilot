import asyncio
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Decision, Execution, Incident, IncidentState
from app.executor.runtime import build_patch
from app.executor.service import execute_ready_incident
from kubernetes import client
from kubernetes.client.rest import ApiException
from scripts.install_executor_rbac import _rules, _upsert_binding


class FakeKubernetesClient:
    def __init__(self, *, prepare_error=None, job_success=True) -> None:
        self.prepare_error = prepare_error
        self.job_success = job_success
        self.prepared = 0
        self.created_jobs = 0

    def prepare(self, **kwargs):
        self.prepared += 1
        if self.prepare_error:
            raise self.prepare_error
        return (
            {"kind": "Deployment", "name": "demo", "namespace": "dev", "spec": {}},
            "a" * 64,
            {"spec": {"template": {"metadata": {"annotations": {}}}}},
        )

    def create_job(self, **kwargs) -> None:
        self.created_jobs += 1

    def wait_for_job(self, *args):
        return self.job_success, None if self.job_success else "job failed"

    def read_job_logs(self, *args) -> str:
        return "executor output"


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


async def create_incident(
    factory,
    *,
    state=IncidentState.READY,
    namespace="dev",
    environment="dev",
    target_kind="Deployment",
    decision_outcome="READY",
) -> Incident:
    incident_id = uuid4()
    async with factory() as session:
        incident = Incident(
            id=incident_id,
            fingerprint=f"fingerprint-{incident_id}",
            alert_name="CrashLoop",
            namespace=namespace,
            environment=environment,
            target_kind=target_kind,
            target_name="demo",
            state=state,
            rca_json={
                "action_parameters": {"namespace": namespace, "name": "demo"}
            },
        )
        session.add(incident)
        session.add(
            Decision(
                incident_id=incident_id,
                recommended_action="restart_workload",
                outcome=decision_outcome,
                confidence=0.9,
                llm_confidence=0.9,
                grounding_score=0.9,
                runbook_score=0.9,
                risk_score=0.2,
                risk_reason="development_low_risk_auto_execute",
                reason="policy_passed",
            )
        )
        await session.commit()
        return incident


def test_ready_incident_runs_one_job_and_rerun_is_idempotent() -> None:
    async def scenario(factory) -> None:
        incident = await create_incident(factory)
        kubernetes = FakeKubernetesClient()

        assert await execute_ready_incident(
            incident.id,
            factory,
            executor_image="incidentpilot-executor:test",
            timeout_seconds=30,
            kubernetes_client=kubernetes,
        )
        assert not await execute_ready_incident(
            incident.id,
            factory,
            executor_image="incidentpilot-executor:test",
            timeout_seconds=30,
            kubernetes_client=kubernetes,
        )

        async with factory() as session:
            current = await session.get(Incident, incident.id)
            execution = await session.scalar(
                select(Execution).where(Execution.incident_id == incident.id)
            )
            assert current.state == IncidentState.VERIFYING
            assert execution.status == "SUCCEEDED"
            assert execution.snapshot_hash == "a" * 64
            assert execution.logs == "executor output"
            assert execution.job_name
        assert kubernetes.prepared == 1
        assert kubernetes.created_jobs == 1

    run_scenario(scenario)


def test_approved_statefulset_runs_through_existing_executor_path() -> None:
    async def scenario(factory) -> None:
        incident = await create_incident(
            factory,
            state=IncidentState.APPROVED,
            namespace="prod",
            environment="production",
            target_kind="StatefulSet",
            decision_outcome="AWAITING_APPROVAL",
        )
        kubernetes = FakeKubernetesClient()

        assert await execute_ready_incident(
            incident.id,
            factory,
            executor_image="incidentpilot-executor:test",
            timeout_seconds=30,
            kubernetes_client=kubernetes,
        )

        async with factory() as session:
            current = await session.get(Incident, incident.id)
            execution = await session.scalar(
                select(Execution).where(Execution.incident_id == incident.id)
            )
            assert current.state == IncidentState.VERIFYING
            assert execution.status == "SUCCEEDED"
        assert kubernetes.prepared == 1
        assert kubernetes.created_jobs == 1

    run_scenario(scenario)


def test_prod_statefulset_waiting_for_approval_is_never_executed() -> None:
    async def scenario(factory) -> None:
        incident = await create_incident(
            factory,
            state=IncidentState.AWAITING_APPROVAL,
            namespace="prod",
            environment="production",
            target_kind="StatefulSet",
            decision_outcome="AWAITING_APPROVAL",
        )
        kubernetes = FakeKubernetesClient()

        assert not await execute_ready_incident(
            incident.id,
            factory,
            executor_image="incidentpilot-executor:test",
            timeout_seconds=30,
            kubernetes_client=kubernetes,
        )

        async with factory() as session:
            executions = (
                await session.scalars(
                    select(Execution).where(Execution.incident_id == incident.id)
                )
            ).all()
            current = await session.get(Incident, incident.id)
            assert executions == []
            assert current.state == IncidentState.AWAITING_APPROVAL
        assert kubernetes.prepared == 0
        assert kubernetes.created_jobs == 0

    run_scenario(scenario)


def test_existing_execution_reservation_prevents_a_second_job() -> None:
    async def scenario(factory) -> None:
        incident = await create_incident(factory)
        async with factory() as session:
            session.add(
                Execution(
                    incident_id=incident.id,
                    action_id="restart_workload",
                    status="PREPARING",
                )
            )
            await session.commit()
        kubernetes = FakeKubernetesClient()

        assert not await execute_ready_incident(
            incident.id,
            factory,
            executor_image="incidentpilot-executor:test",
            timeout_seconds=30,
            kubernetes_client=kubernetes,
        )
        assert kubernetes.prepared == 0
        assert kubernetes.created_jobs == 0

    run_scenario(scenario)


def test_preflight_failure_escalates_without_creating_a_job() -> None:
    async def scenario(factory) -> None:
        incident = await create_incident(factory)
        kubernetes = FakeKubernetesClient(prepare_error=ValueError("dry run rejected"))

        assert not await execute_ready_incident(
            incident.id,
            factory,
            executor_image="incidentpilot-executor:test",
            timeout_seconds=30,
            kubernetes_client=kubernetes,
        )

        async with factory() as session:
            current = await session.get(Incident, incident.id)
            execution = await session.scalar(
                select(Execution).where(Execution.incident_id == incident.id)
            )
            assert current.state == IncidentState.ESCALATED
            assert execution.status == "FAILED"
            assert "dry run rejected" in execution.logs
            assert execution.job_name is None
        assert kubernetes.created_jobs == 0

    run_scenario(scenario)


def test_failed_job_transitions_through_failed_to_escalated_and_keeps_logs() -> None:
    async def scenario(factory) -> None:
        incident = await create_incident(factory)
        kubernetes = FakeKubernetesClient(job_success=False)

        assert not await execute_ready_incident(
            incident.id,
            factory,
            executor_image="incidentpilot-executor:test",
            timeout_seconds=30,
            kubernetes_client=kubernetes,
        )

        async with factory() as session:
            current = await session.get(Incident, incident.id)
            execution = await session.scalar(
                select(Execution).where(Execution.incident_id == incident.id)
            )
            assert current.state == IncidentState.ESCALATED
            assert execution.status == "FAILED"
            assert "executor output" in execution.logs
            assert "job failed" in execution.logs
            assert execution.error is not None

    run_scenario(scenario)


def test_configured_action_patch_construction() -> None:
    restart = build_patch(
        "restart_workload",
        {"template": {"metadata": {"annotations": {"existing": "yes"}}}},
        {},
    )
    assert "kubectl.kubernetes.io/restartedAt" in restart["spec"]["template"][
        "metadata"
    ]["annotations"]
    assert build_patch("scale_deployment", {}, {"replicas": 4}) == {
        "spec": {"replicas": 4}
    }
    assert build_patch(
        "rollback_deployment",
        {},
        {},
        rollback_template={"metadata": {"labels": {"app": "demo"}}},
    ) == {
        "spec": {
            "template": {"metadata": {"labels": {"app": "demo"}}}
        }
    }
    memory_patch = build_patch(
        "increase_memory_limit",
        {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "resources": {
                                "limits": {"memory": "512Mi"},
                                "requests": {"memory": "800Mi"},
                            },
                        }
                    ]
                }
            }
        },
        {"container": "app", "factor": 1.5},
    )
    resources = memory_patch["spec"]["template"]["spec"]["containers"][0][
        "resources"
    ]
    assert resources == {
        "limits": {"memory": "805306368"},
        "requests": {"memory": "805306368"},
    }
    assert build_patch(
        "expand_pvc", {}, {"new_size": "2Gi"}
    ) == {"spec": {"resources": {"requests": {"storage": "2Gi"}}}}


def test_action_service_accounts_get_only_required_namespaced_verbs() -> None:
    expected = {
        "restart_workload": (
            {"deployments", "statefulsets"},
            {"get", "patch"},
        ),
        "rollback_deployment": ({"deployments"}, {"get", "patch"}),
        "scale_deployment": ({"deployments"}, {"patch"}),
        "increase_memory_limit": ({"deployments"}, {"patch"}),
        "expand_pvc": ({"persistentvolumeclaims"}, {"patch"}),
    }
    for action_id, (resources, verbs) in expected.items():
        rules = _rules(action_id)
        assert len(rules) == 1
        assert set(rules[0].resources) == resources
        assert set(rules[0].verbs) == verbs


def test_rbac_binding_uses_installed_kubernetes_subject_model() -> None:
    class FakeRbacApi:
        def __init__(self) -> None:
            self.binding = None

        def read_namespaced_role_binding(self, name, namespace):
            raise ApiException(status=404)

        def create_namespaced_role_binding(self, namespace, binding) -> None:
            self.binding = binding

    api = FakeRbacApi()
    _upsert_binding(api, "dev", "incidentpilot-executor-restart-workload")

    assert isinstance(api.binding, client.V1RoleBinding)
    subject = api.binding.subjects[0]
    assert isinstance(subject, client.RbacV1Subject)
    assert subject.kind == "ServiceAccount"
    assert subject.name == "incidentpilot-executor-restart-workload"
    assert subject.namespace == "dev"
    assert subject.to_dict() == {
        "api_group": None,
        "kind": "ServiceAccount",
        "name": "incidentpilot-executor-restart-workload",
        "namespace": "dev",
    }
