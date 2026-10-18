---
id: rb-restart-workload
title: Restart a crash-looping Deployment
linked_action_id: restart_workload
risk_level: low
applies_to_kinds: [Deployment]
---

# Restart a crash-looping Deployment

## Symptoms
- Alert `PodCrashLooping` firing for a pod owned by a Deployment.
- Container state `CrashLoopBackOff` with a rising restart count.
- The failure looks transient: a dependency was briefly unavailable, a stuck connection, a corrupted in-memory cache, or a one-off startup race.

## How to confirm
- `get_pod_status`: restart count increasing; last termination reason is `Error` (not `OOMKilled`).
- `get_pod_logs` with `previous=true`: errors point to a temporary condition, e.g. "connection refused" to a dependency that is now healthy, timeouts during startup, or lock/cache errors.
- `get_rollout_history`: NO new revision in the last hour. If the image or config changed recently, use `rollback_deployment` instead.
- `get_events`: no `FailedScheduling`, `ImagePullBackOff` or `FailedMount` events.

## Do NOT use when
- Last termination reason is `OOMKilled` → use `increase_memory_limit`.
- A rollout happened shortly before the crashes started → use `rollback_deployment`.
- Logs show a configuration or code error that will repeat on every start (missing env var, bad config file, stack trace in application code). A restart will not fix it; escalate.
- The dependency the logs point to is still down. Fix or escalate the dependency instead.

## Action
- Action: `restart_workload` (rolling restart of the Deployment).
- Parameters: `namespace`, `name`.
- Effect: pods are recreated one by one; the spec is unchanged except the restart annotation.

## How to tell it worked
- Within the stabilization window (3 minutes), all desired replicas are Ready.
- Restart count does not increase after the restart:
  `increase(kube_pod_container_status_restarts_total{namespace="<ns>", pod=~"<name>-.*"}[3m]) == 0`
- Alert `PodCrashLooping` for this workload stops firing.

## Escalate when
- Pods return to `CrashLoopBackOff` after the restart. The cause is not transient.
