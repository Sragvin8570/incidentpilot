from __future__ import annotations

import sys
from pathlib import Path

from kubernetes import client, config
from kubernetes.client.rest import ApiException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent.actions import ActionCatalog


NAMESPACES = ("dev", "staging", "prod")
PREFIX = "incidentpilot-executor-"


def main() -> None:
    try:
        config.load_kube_config()
    except config.ConfigException:
        config.load_incluster_config()
    core = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()
    for action in ActionCatalog.load().actions.values():
        suffix = action.id.replace("_", "-")
        account_name = PREFIX + suffix
        rules = _rules(action.id)
        for namespace in NAMESPACES:
            _upsert_service_account(core, namespace, account_name)
            _upsert_role(rbac, namespace, account_name, rules)
            _upsert_binding(rbac, namespace, account_name)
    print("Installed executor service accounts and namespaced roles in dev, staging, prod")


def _rules(action_id: str) -> list[client.V1PolicyRule]:
    if action_id in {"restart_workload", "rollback_deployment"}:
        resources = (
            ["deployments", "statefulsets"]
            if action_id == "restart_workload"
            else ["deployments"]
        )
        return [
            client.V1PolicyRule(
                api_groups=["apps"],
                resources=resources,
                verbs=["get", "patch"],
            )
        ]
    if action_id == "expand_pvc":
        return [
            client.V1PolicyRule(
                api_groups=[""],
                resources=["persistentvolumeclaims"],
                verbs=["patch"],
            )
        ]
    if action_id in {"scale_deployment", "increase_memory_limit"}:
        return [
            client.V1PolicyRule(
                api_groups=["apps"],
                resources=["deployments"],
                verbs=["patch"],
            )
        ]
    raise ValueError(f"Unsupported configured action: {action_id}")


def _upsert_service_account(
    api: client.CoreV1Api, namespace: str, name: str
) -> None:
    account = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        automount_service_account_token=False,
    )
    try:
        api.read_namespaced_service_account(name, namespace)
    except ApiException as error:
        if error.status != 404:
            raise
        api.create_namespaced_service_account(namespace, account)
    else:
        api.replace_namespaced_service_account(name, namespace, account)


def _upsert_role(
    api: client.RbacAuthorizationV1Api,
    namespace: str,
    name: str,
    rules: list[client.V1PolicyRule],
) -> None:
    role = client.V1Role(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        rules=rules,
    )
    try:
        api.read_namespaced_role(name, namespace)
    except ApiException as error:
        if error.status != 404:
            raise
        api.create_namespaced_role(namespace, role)
    else:
        api.replace_namespaced_role(name, namespace, role)


def _upsert_binding(
    api: client.RbacAuthorizationV1Api, namespace: str, name: str
) -> None:
    binding = client.V1RoleBinding(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="Role",
            name=name,
        ),
        subjects=[
            client.RbacV1Subject(
                kind="ServiceAccount",
                name=name,
                namespace=namespace,
            )
        ],
    )
    try:
        api.read_namespaced_role_binding(name, namespace)
    except ApiException as error:
        if error.status != 404:
            raise
        api.create_namespaced_role_binding(namespace, binding)
    else:
        api.replace_namespaced_role_binding(name, namespace, binding)


if __name__ == "__main__":
    main()
