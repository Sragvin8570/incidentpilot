import json
import math
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from kubernetes import client, config
from kubernetes.utils.quantity import parse_quantity


def build_patch(
    action_id: str,
    target_spec: dict[str, Any],
    parameters: dict[str, Any],
    *,
    rollback_template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if action_id in {"restart_workload"}:
        template = target_spec["template"]
        annotations = dict(template.get("metadata", {}).get("annotations", {}))
        annotations["kubectl.kubernetes.io/restartedAt"] = (
            datetime.now(timezone.utc).isoformat()
        )
        return {"spec": {"template": {"metadata": {"annotations": annotations}}}}
    if action_id == "rollback_deployment":
        if rollback_template is None:
            raise ValueError("Rollback requires the previous known-good template")
        return {"spec": {"template": rollback_template}}
    if action_id == "scale_deployment":
        return {"spec": {"replicas": parameters["replicas"]}}
    if action_id == "increase_memory_limit":
        containers = target_spec["template"]["spec"]["containers"]
        container = next(
            (item for item in containers if item["name"] == parameters["container"]),
            None,
        )
        if container is None:
            raise ValueError("Configured container was not found in target workload")
        limits = container.get("resources", {}).get("limits", {})
        current_limit = limits.get("memory")
        if not isinstance(current_limit, str):
            raise ValueError("Target container has no memory limit")
        factor = parameters.get("factor", 1.5)
        new_limit_bytes = math.ceil(
            parse_quantity(current_limit) * Decimal(str(factor))
        )
        new_limit = f"{new_limit_bytes}"
        requests = container.get("resources", {}).get("requests", {})
        patch: dict[str, Any] = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": parameters["container"],
                                "resources": {"limits": {"memory": new_limit}},
                            }
                        ]
                    }
                }
            }
        }
        request = requests.get("memory")
        if isinstance(request, str) and parse_quantity(request) > new_limit_bytes:
            patch["spec"]["template"]["spec"]["containers"][0]["resources"][
                "requests"
            ] = {"memory": new_limit}
        return patch
    if action_id == "expand_pvc":
        return {
            "spec": {"resources": {"requests": {"storage": parameters["new_size"]}}}
        }
    raise ValueError(f"Unsupported configured action: {action_id}")


def execute_job_action() -> None:
    action_id = os.environ["ACTION_ID"]
    parameters = json.loads(os.environ["ACTION_PARAMETERS"])
    patch = json.loads(os.environ["ACTION_PATCH"])
    namespace = parameters["namespace"]
    name = parameters["name"]
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    apps = client.AppsV1Api()
    core = client.CoreV1Api()
    if action_id == "expand_pvc":
        core.patch_namespaced_persistent_volume_claim(name, namespace, patch)
        return
    if action_id in {"scale_deployment", "rollback_deployment", "increase_memory_limit"}:
        apps.patch_namespaced_deployment(name, namespace, patch)
        if action_id == "rollback_deployment":
            _wait_for_deployment(apps, name, namespace)
        return
    if action_id == "restart_workload":
        kind = os.environ["TARGET_KIND"]
        if kind == "Deployment":
            apps.patch_namespaced_deployment(name, namespace, patch)
            _wait_for_deployment(apps, name, namespace)
        elif kind == "StatefulSet":
            apps.patch_namespaced_stateful_set(name, namespace, patch)
            _wait_for_stateful_set(apps, name, namespace)
        else:
            raise ValueError(f"Unsupported restart target: {kind}")
        return
    raise ValueError(f"Unsupported configured action: {action_id}")


def _wait_for_deployment(apps: client.AppsV1Api, name: str, namespace: str) -> None:
    deadline = time.monotonic() + int(os.environ.get("ROLLOUT_TIMEOUT_SECONDS", "240"))
    while time.monotonic() < deadline:
        deployment = apps.read_namespaced_deployment(name, namespace)
        desired = deployment.spec.replicas or 0
        status = deployment.status
        if (
            (status.observed_generation or 0) >= (deployment.metadata.generation or 0)
            and status.updated_replicas == desired
            and status.ready_replicas == desired
            and status.available_replicas == desired
        ):
            return
        time.sleep(2)
    raise TimeoutError(f"Deployment/{name} did not become ready before timeout")


def _wait_for_stateful_set(
    apps: client.AppsV1Api, name: str, namespace: str
) -> None:
    deadline = time.monotonic() + int(os.environ.get("ROLLOUT_TIMEOUT_SECONDS", "240"))
    while time.monotonic() < deadline:
        workload = apps.read_namespaced_stateful_set(name, namespace)
        desired = workload.spec.replicas or 0
        status = workload.status
        if (
            (status.observed_generation or 0) >= (workload.metadata.generation or 0)
            and status.ready_replicas == desired
            and status.current_revision == status.update_revision
        ):
            return
        time.sleep(2)
    raise TimeoutError(f"StatefulSet/{name} did not become ready before timeout")


if __name__ == "__main__":
    execute_job_action()
