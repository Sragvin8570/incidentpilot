from dataclasses import dataclass
from typing import Protocol

from fastapi.concurrency import run_in_threadpool
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError as Urllib3HTTPError


@dataclass(frozen=True)
class WorkloadTarget:
    kind: str
    name: str


@dataclass(frozen=True)
class EnrichmentResult:
    environment: str
    target: WorkloadTarget | None


class EnrichmentUnavailable(RuntimeError):
    pass


class ResourceResolver(Protocol):
    async def resolve(
        self, namespace: str, kind: str, name: str
    ) -> EnrichmentResult: ...


def normalize_environment(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"dev", "development"}:
        return "dev"
    if normalized in {"staging", "stage"}:
        return "staging"
    if normalized in {"prod", "production"}:
        return "production"
    return "production"


class KubernetesResourceResolver:
    def __init__(self) -> None:
        self._core: client.CoreV1Api | None = None
        self._apps: client.AppsV1Api | None = None

    async def resolve(
        self, namespace: str, kind: str, name: str
    ) -> EnrichmentResult:
        try:
            return await run_in_threadpool(
                self._resolve_sync, namespace, kind, name
            )
        except (ApiException, OSError, Urllib3HTTPError) as error:
            raise EnrichmentUnavailable(
                "Kubernetes enrichment is unavailable"
            ) from error

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

    def _resolve_sync(self, namespace: str, kind: str, name: str) -> EnrichmentResult:
        self._ensure_clients()
        assert self._core is not None
        assert self._apps is not None

        try:
            namespace_obj = self._core.read_namespace(namespace)
        except ApiException as error:
            if error.status == 404:
                return EnrichmentResult("production", None)
            raise

        environment = normalize_environment(
            (namespace_obj.metadata.labels or {}).get(
                "incidentpilot.io/environment"
            )
        )
        try:
            target = self._resolve_target(namespace, kind, name)
        except ApiException as error:
            if error.status == 404:
                return EnrichmentResult(environment, None)
            raise
        return EnrichmentResult(environment, target)

    def _resolve_target(
        self, namespace: str, kind: str, name: str
    ) -> WorkloadTarget | None:
        assert self._core is not None
        assert self._apps is not None

        canonical_kind = kind.lower()
        if canonical_kind == "pod":
            pod = self._core.read_namespaced_pod(name, namespace)
            for owner in pod.metadata.owner_references or []:
                if not owner.controller:
                    continue
                if owner.kind == "ReplicaSet":
                    replica_set = self._apps.read_namespaced_replica_set(
                        owner.name, namespace
                    )
                    for parent in replica_set.metadata.owner_references or []:
                        if parent.controller and parent.kind == "Deployment":
                            self._apps.read_namespaced_deployment(
                                parent.name, namespace
                            )
                            return WorkloadTarget("Deployment", parent.name)
                    return WorkloadTarget("ReplicaSet", owner.name)
                if owner.kind == "StatefulSet":
                    self._apps.read_namespaced_stateful_set(owner.name, namespace)
                    return WorkloadTarget("StatefulSet", owner.name)
                if owner.kind == "DaemonSet":
                    self._apps.read_namespaced_daemon_set(owner.name, namespace)
                    return WorkloadTarget("DaemonSet", owner.name)
            return WorkloadTarget("Pod", name)

        if canonical_kind == "deployment":
            self._apps.read_namespaced_deployment(name, namespace)
            return WorkloadTarget("Deployment", name)
        if canonical_kind == "statefulset":
            self._apps.read_namespaced_stateful_set(name, namespace)
            return WorkloadTarget("StatefulSet", name)
        if canonical_kind in {"pvc", "persistentvolumeclaim"}:
            self._core.read_namespaced_persistent_volume_claim(name, namespace)
            return WorkloadTarget("PersistentVolumeClaim", name)
        return None
