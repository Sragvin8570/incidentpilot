from datetime import datetime
from collections.abc import Callable
from typing import Any, Literal

from fastapi.concurrency import run_in_threadpool
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from pydantic import BaseModel, Field
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.agent.tools.base import Tool, ToolExecutionError, ToolRegistry
from app.enrichment.service import EnrichmentUnavailable

_KUBERNETES_NAME = r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"
_NAMESPACE_NAME = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


class NamespacedResource(BaseModel):
    namespace: str = Field(min_length=1, max_length=63, pattern=_NAMESPACE_NAME)
    name: str = Field(min_length=1, max_length=253, pattern=_KUBERNETES_NAME)


class PodArgs(NamespacedResource):
    pass


class PodLogsArgs(PodArgs):
    container: str | None = Field(default=None, max_length=253)
    previous: bool = False
    tail_lines: int = Field(default=100, ge=1, le=200)


class EventArgs(BaseModel):
    namespace: str = Field(min_length=1, max_length=63, pattern=_NAMESPACE_NAME)
    involved_object: str | None = Field(
        default=None, min_length=1, max_length=253, pattern=_KUBERNETES_NAME
    )


class WorkloadArgs(NamespacedResource):
    kind: Literal["Deployment", "StatefulSet", "DaemonSet"]


class DeploymentArgs(NamespacedResource):
    pass


class NodeArgs(BaseModel):
    node: str = Field(min_length=1, max_length=253, pattern=_KUBERNETES_NAME)


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class KubernetesEvidenceTools:
    def __init__(self) -> None:
        self._core: client.CoreV1Api | None = None
        self._apps: client.AppsV1Api | None = None

    def registry(self) -> ToolRegistry:
        return ToolRegistry(
            [
                Tool(
                    "get_pod_status",
                    "Read pod phase, container states, restart counts and last termination reasons.",
                    PodArgs,
                    self.get_pod_status,
                ),
                Tool(
                    "get_pod_logs",
                    "Read a bounded tail of container logs.",
                    PodLogsArgs,
                    self.get_pod_logs,
                ),
                Tool(
                    "get_events",
                    "Read recent Kubernetes events in a namespace, optionally for one object.",
                    EventArgs,
                    self.get_events,
                ),
                Tool(
                    "describe_workload",
                    "Read a Deployment, StatefulSet or DaemonSet configuration summary.",
                    WorkloadArgs,
                    self.describe_workload,
                ),
                Tool(
                    "get_rollout_history",
                    "Read recent Deployment revisions and container images.",
                    DeploymentArgs,
                    self.get_rollout_history,
                ),
                Tool(
                    "get_node_status",
                    "Read node conditions, capacity and allocatable resources.",
                    NodeArgs,
                    self.get_node_status,
                ),
            ]
        )

    def _ensure_clients(self) -> None:
        if self._core is not None and self._apps is not None:
            return
        try:
            config.load_incluster_config()
        except ConfigException:
            try:
                config.load_kube_config()
            except ConfigException as error:
                raise EnrichmentUnavailable(
                    "No Kubernetes client configuration is available"
                ) from error
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()

    async def _run(
        self,
        handler: Callable[..., Any],
        *args: Any,
    ) -> Any:
        try:
            return await run_in_threadpool(handler, *args)
        except (
            ApiException,
            OSError,
            Urllib3HTTPError,
            EnrichmentUnavailable,
        ) as error:
            raise ToolExecutionError(
                f"Kubernetes read failed: {error}"
            ) from error

    def _pod_status(self, namespace: str, name: str) -> dict:
        self._ensure_clients()
        assert self._core is not None
        pod = self._core.read_namespaced_pod(name, namespace)
        statuses = pod.status.container_statuses or []
        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "containers": [
                {
                    "name": item.name,
                    "ready": item.ready,
                    "restart_count": item.restart_count,
                    "state": item.state.to_dict() if item.state else None,
                    "last_state": item.last_state.to_dict() if item.last_state else None,
                }
                for item in statuses
            ],
        }

    async def get_pod_status(self, args: PodArgs) -> dict:
        return await self._run(self._pod_status, args.namespace, args.name)

    def _pod_logs(
        self,
        namespace: str,
        name: str,
        container: str | None,
        previous: bool,
        tail_lines: int,
    ) -> dict:
        self._ensure_clients()
        assert self._core is not None
        logs = self._core.read_namespaced_pod_log(
            name=name,
            namespace=namespace,
            container=container,
            previous=previous,
            tail_lines=tail_lines,
            timestamps=True,
        )
        return {"pod": name, "namespace": namespace, "logs": logs}

    async def get_pod_logs(self, args: PodLogsArgs) -> dict:
        return await self._run(
            self._pod_logs,
            args.namespace,
            args.name,
            args.container,
            args.previous,
            args.tail_lines,
        )

    def _events(self, namespace: str, involved_object: str | None) -> dict:
        self._ensure_clients()
        assert self._core is not None
        field_selector = (
            f"involvedObject.name={involved_object}" if involved_object else None
        )
        events = self._core.list_namespaced_event(
            namespace=namespace,
            field_selector=field_selector,
            limit=100,
        )
        return {
            "events": [
                {
                    "type": item.type,
                    "reason": item.reason,
                    "message": item.message,
                    "object": item.involved_object.name,
                    "count": item.count,
                    "first_timestamp": _timestamp(item.first_timestamp),
                    "last_timestamp": _timestamp(item.last_timestamp),
                }
                for item in events.items
            ]
        }

    async def get_events(self, args: EventArgs) -> dict:
        return await self._run(
            self._events, args.namespace, args.involved_object
        )

    def _workload(self, namespace: str, kind: str, name: str) -> dict:
        self._ensure_clients()
        assert self._apps is not None
        readers = {
            "Deployment": self._apps.read_namespaced_deployment,
            "StatefulSet": self._apps.read_namespaced_stateful_set,
            "DaemonSet": self._apps.read_namespaced_daemon_set,
        }
        workload = readers[kind](name, namespace)
        spec = workload.spec
        status = workload.status
        return {
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "generation": workload.metadata.generation,
            "replicas": getattr(spec, "replicas", None),
            "ready_replicas": getattr(status, "ready_replicas", None),
            "updated_replicas": getattr(status, "updated_replicas", None),
            "selector": (
                spec.selector.match_labels
                if spec.selector and spec.selector.match_labels
                else {}
            ),
            "containers": [
                {
                    "name": container.name,
                    "image": container.image,
                    "resources": container.resources.to_dict()
                    if container.resources
                    else {},
                    "readiness_probe": container.readiness_probe.to_dict()
                    if container.readiness_probe
                    else None,
                    "liveness_probe": container.liveness_probe.to_dict()
                    if container.liveness_probe
                    else None,
                }
                for container in spec.template.spec.containers
            ],
            "conditions": [
                {
                    "type": condition.type,
                    "status": condition.status,
                    "reason": condition.reason,
                    "message": condition.message,
                }
                for condition in (status.conditions or [])
            ],
        }

    async def describe_workload(self, args: WorkloadArgs) -> dict:
        return await self._run(
            self._workload, args.namespace, args.kind, args.name
        )

    def _rollout_history(self, namespace: str, name: str) -> dict:
        self._ensure_clients()
        assert self._apps is not None
        deployment = self._apps.read_namespaced_deployment(name, namespace)
        labels = deployment.spec.selector.match_labels or {}
        selector = ",".join(f"{key}={value}" for key, value in labels.items())
        replicas = self._apps.list_namespaced_replica_set(
            namespace=namespace,
            label_selector=selector,
            limit=100,
        )
        revisions = []
        for replica_set in replicas.items:
            if not any(
                owner.controller
                and owner.kind == "Deployment"
                and owner.name == name
                for owner in replica_set.metadata.owner_references or []
            ):
                continue
            revisions.append(
                {
                    "revision": (replica_set.metadata.annotations or {}).get(
                        "deployment.kubernetes.io/revision"
                    ),
                    "created_at": _timestamp(replica_set.metadata.creation_timestamp),
                    "ready_replicas": replica_set.status.ready_replicas,
                    "images": [
                        container.image
                        for container in replica_set.spec.template.spec.containers
                    ],
                }
            )
        revisions.sort(key=lambda item: int(item["revision"] or 0), reverse=True)
        return {"deployment": name, "revisions": revisions[:20]}

    async def get_rollout_history(self, args: DeploymentArgs) -> dict:
        return await self._run(
            self._rollout_history, args.namespace, args.name
        )

    def _node_status(self, name: str) -> dict:
        self._ensure_clients()
        assert self._core is not None
        node = self._core.read_node(name)
        return {
            "name": node.metadata.name,
            "conditions": [
                {
                    "type": condition.type,
                    "status": condition.status,
                    "reason": condition.reason,
                    "message": condition.message,
                }
                for condition in (node.status.conditions or [])
            ],
            "capacity": node.status.capacity or {},
            "allocatable": node.status.allocatable or {},
        }

    async def get_node_status(self, args: NodeArgs) -> dict:
        return await self._run(self._node_status, args.node)
