---
id: rb-rollback-deployment
title: Roll back a bad Deployment rollout
linked_action_id: rollback_deployment
risk_level: medium
applies_to_kinds: [Deployment]
---

# Roll back a bad Deployment rollout

## Symptoms
- Pods started failing shortly after a new revision was rolled out.
- Typical alerts: `PodCrashLooping`, `DeploymentReplicasUnavailable`, `HighHTTPErrorRate`.
- Possible states: `CrashLoopBackOff`, `ImagePullBackOff` / `ErrImagePull`, or failing readiness probes on new pods while old pods were healthy.

## How to confirm
- `get_rollout_history`: a new revision (image tag or pod template change) appeared shortly before the first failure.
- `get_pod_status`: only pods from the newest ReplicaSet are failing.
- `get_events`: `Failed to pull image`, `Back-off pulling image`, or `Readiness probe failed` on new pods.
- `get_pod_logs` with `previous=true`: errors that did not exist in the previous version, such as a missing config key or a startup exception.
- `query_prometheus`: error rate or restarts rose at the time of the rollout.

## Do NOT use when
- There is no previous revision to roll back to.
- The previous revision was also failing.
- The failure started long after the rollout and matches another runbook better (e.g. OOM after hours of running → `increase_memory_limit`).
- The rollout included a database migration or other change that cannot be safely reversed. Escalate.

## Action
- Action: `rollback_deployment` (roll back to the previous revision).
- Parameters: `namespace`, `name`, `to_revision` (optional; default is the previous revision).
- Effect: the pod template returns to the last known-good revision.

## How to tell it worked
- Desired replicas are Ready on the previous revision within the stabilization window.
- `kube_deployment_status_replicas_available{namespace="<ns>", deployment="<name>"} == kube_deployment_spec_replicas{namespace="<ns>", deployment="<name>"}`
- Restarts stop increasing and the original alert stops firing.
- Spec check: the Deployment's image matches the previous revision (not reverted by a CI/CD pipeline).

## Escalate when
- The previous revision also fails, or something (e.g. a GitOps tool) re-applies the bad revision.
