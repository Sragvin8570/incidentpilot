---
id: rb-increase-memory-limit
title: Raise memory limit for OOMKilled containers
linked_action_id: increase_memory_limit
risk_level: medium
applies_to_kinds: [Deployment]
---

# Raise memory limit for OOMKilled containers

## Symptoms
- Alert `ContainerOOMKilled` or `PodCrashLooping`.
- Last termination reason is `OOMKilled` (exit code 137).
- Memory usage climbs toward the container's memory limit before each restart.

## How to confirm
- `get_pod_status`: last termination reason `OOMKilled`.
- `describe_workload`: the container's current memory limit and request.
- `query_prometheus`: working-set memory reached the limit before the kill:
  `max_over_time(container_memory_working_set_bytes{namespace="<ns>", pod=~"<name>-.*", container="<container>"}[30m])`
  compared with
  `kube_pod_container_resource_limits{namespace="<ns>", pod=~"<name>-.*", container="<container>", resource="memory"}`
- Distinguish a too-small limit from a leak: a limit that is too small shows memory jumping to the limit soon after start; a leak shows steady growth over a long time. A leak will come back even with a higher limit.

## Do NOT use when
- Termination reason is not `OOMKilled`.
- The node is under memory pressure (`get_node_status` shows `MemoryPressure=True`). Raising limits makes it worse. Escalate.
- The limit has already been raised by this action for this workload in the last 24 hours. A repeat suggests a leak. Escalate.

## Action
- Action: `increase_memory_limit`.
- Parameters: `namespace`, `name`, `container`, `factor` (1.25 to 2.0; default 1.5).
- Bounds: new limit at most 2x the current limit; the request is raised by the same factor only if it would otherwise be above the new limit.
- Effect: Deployment rolls out new pods with the higher limit.

## How to tell it worked
- No `OOMKilled` terminations during the stabilization window (5 minutes for this action).
- Memory stays below 90% of the new limit:
  `max_over_time(container_memory_working_set_bytes{namespace="<ns>", pod=~"<name>-.*", container="<container>"}[5m]) < 0.9 * <new_limit_bytes>`
- Restarts stop increasing; the original alert stops firing.
- Spec check: the new limit is still in the Deployment.

## Escalate when
- OOM recurs at the new limit, or memory keeps growing steadily (likely a memory leak; needs a code fix).
