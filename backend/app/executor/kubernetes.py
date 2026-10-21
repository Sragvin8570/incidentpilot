import hashlib
import json
import time
from typing import Any
from uuid import UUID

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from app.executor.runtime import build_patch


class KubernetesExecutionClient:
    def __init__(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.api_client = client.ApiClient()
        self.apps = client.AppsV1Api(self.api_client)
        self.core = client.CoreV1Api(self.api_client)
        self.batch = client.BatchV1Api(self.api_client)

    def prepare(
        self,
        *,
        action_id: str,
        target_kind: str,
        parameters: dict[str, Any],
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        namespace = parameters["namespace"]
        name = parameters["name"]
        target = self._read_target(target_kind, name, namespace)
        serialized = self.api_client.sanitize_for_serialization(target)
        target_spec = serialized["spec"]
        snapshot = {
            "kind": target_kind,
            "namespace": namespace,
            "name": name,
            "spec": target_spec,
        }
        snapshot_hash = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        rollback_template = None
        if action_id == "rollback_deployment":
            rollback_template = self._previous_template(
                target, parameters.get("to_revision"), namespace
            )
        patch = build_patch(
            action_id,
            target_spec,
            parameters,
            rollback_template=rollback_template,
        )
        patch["metadata"] = {
            "resourceVersion": target.metadata.resource_version
        }
        self._dry_run_patch(action_id, target_kind, name, namespace, patch)
        return snapshot, snapshot_hash, patch

    def create_job(
        self,
        *,
        name: str,
        incident_id: str,
        namespace: str,
        service_account: str,
        image: str,
        action_id: str,
        target_kind: str,
        parameters: dict[str, Any],
        patch: dict[str, Any],
        timeout_seconds: int,
    ) -> None:
        container = client.V1Container(
            name="executor",
            image=image,
            image_pull_policy="IfNotPresent",
            env=[
                client.V1EnvVar(name="ACTION_ID", value=action_id),
                client.V1EnvVar(name="TARGET_KIND", value=target_kind),
                client.V1EnvVar(
                    name="ACTION_PARAMETERS",
                    value=json.dumps(parameters, separators=(",", ":")),
                ),
                client.V1EnvVar(
                    name="ACTION_PATCH",
                    value=json.dumps(patch, separators=(",", ":")),
                ),
                client.V1EnvVar(
                    name="ROLLOUT_TIMEOUT_SECONDS",
                    value=str(timeout_seconds),
                ),
            ],
            security_context=client.V1SecurityContext(
                allow_privilege_escalation=False,
                read_only_root_filesystem=True,
                run_as_non_root=True,
                run_as_user=10001,
                capabilities=client.V1Capabilities(drop=["ALL"]),
            ),
        )
        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=name,
                labels={
                    "app.kubernetes.io/name": "incidentpilot-executor",
                    "incidentpilot.io/incident-id": incident_id,
                },
            ),
            spec=client.V1JobSpec(
                backoff_limit=0,
                active_deadline_seconds=timeout_seconds,
                ttl_seconds_after_finished=86400,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app.kubernetes.io/name": "incidentpilot-executor",
                            "incidentpilot.io/incident-id": incident_id,
                        }
                    ),
                    spec=client.V1PodSpec(
                        service_account_name=service_account,
                        automount_service_account_token=True,
                        restart_policy="Never",
                        containers=[container],
                    ),
                ),
            ),
        )
        self.batch.create_namespaced_job(namespace, job)

    def wait_for_job(
        self, name: str, namespace: str, timeout_seconds: int
    ) -> tuple[bool, str | None]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            job = self.batch.read_namespaced_job(name, namespace)
            status = job.status
            if status.succeeded:
                return True, None
            if status.failed:
                reason = next(
                    (
                        condition.message or condition.reason
                        for condition in status.conditions or []
                        if condition.type == "Failed"
                    ),
                    "Kubernetes Job failed",
                )
                return False, reason
            time.sleep(1)
        return False, "Kubernetes Job timed out"

    def read_job_logs(self, name: str, namespace: str) -> str:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pods = self.core.list_namespaced_pod(
                namespace,
                label_selector=f"job-name={name}",
            ).items
            if pods:
                try:
                    return self.core.read_namespaced_pod_log(
                        pods[0].metadata.name,
                        namespace,
                        container="executor",
                    )
                except ApiException as error:
                    if error.status != 404:
                        raise
            time.sleep(1)
        return "Executor pod logs were not available before the log timeout"

    def _read_target(self, kind: str, name: str, namespace: str) -> Any:
        if kind == "Deployment":
            return self.apps.read_namespaced_deployment(name, namespace)
        if kind == "StatefulSet":
            return self.apps.read_namespaced_stateful_set(name, namespace)
        if kind == "PersistentVolumeClaim":
            return self.core.read_namespaced_persistent_volume_claim(name, namespace)
        raise ValueError(f"Unsupported execution target kind: {kind}")

    def _dry_run_patch(
        self,
        action_id: str,
        target_kind: str,
        name: str,
        namespace: str,
        patch: dict[str, Any],
    ) -> None:
        if action_id == "expand_pvc":
            self.core.patch_namespaced_persistent_volume_claim(
                name, namespace, patch, dry_run="All"
            )
        elif target_kind == "Deployment":
            self.apps.patch_namespaced_deployment(
                name, namespace, patch, dry_run="All"
            )
        elif target_kind == "StatefulSet":
            self.apps.patch_namespaced_stateful_set(
                name, namespace, patch, dry_run="All"
            )
        else:
            raise ValueError(f"Unsupported dry-run target kind: {target_kind}")

    def _previous_template(
        self, deployment: Any, to_revision: int | None, namespace: str
    ) -> dict[str, Any]:
        selector = deployment.spec.selector.match_labels or {}
        label_selector = ",".join(
            f"{key}={value}" for key, value in sorted(selector.items())
        )
        replicasets = self.apps.list_namespaced_replica_set(
            namespace,
            label_selector=label_selector,
        ).items
        owned = [
            item
            for item in replicasets
            if any(
                owner.uid == deployment.metadata.uid
                and owner.kind == "Deployment"
                for owner in item.metadata.owner_references or []
            )
        ]
        revisions = sorted(
            (
                int(item.metadata.annotations["deployment.kubernetes.io/revision"]),
                item,
            )
            for item in owned
            if item.metadata.annotations
            and item.metadata.annotations.get("deployment.kubernetes.io/revision")
        )
        if to_revision is None:
            if len(revisions) < 2:
                raise ValueError("No previous Deployment revision is available")
            revision, replicaset = revisions[-2]
        else:
            match = next(
                ((revision, item) for revision, item in revisions if revision == to_revision),
                None,
            )
            if match is None:
                raise ValueError("Requested rollback revision does not exist")
            revision, replicaset = match
        if (replicaset.status.ready_replicas or 0) < 1:
            raise ValueError(f"Rollback revision {revision} is not healthy")
        return self.api_client.sanitize_for_serialization(
            replicaset.spec.template
        )
