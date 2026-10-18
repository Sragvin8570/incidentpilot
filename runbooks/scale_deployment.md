---
id: rb-scale-deployment
title: Scale out an overloaded Deployment
linked_action_id: scale_deployment
risk_level: medium
applies_to_kinds: [Deployment]
---

# Scale out an overloaded Deployment

## Symptoms
- Alerts `HighHTTPErrorRate` or high latency, with pods healthy (not crashing).
- CPU usage near the CPU limit on all replicas, or request rate well above normal.
- Pods may be failing readiness probes because they are too busy to answer.

## How to confirm
- `get_pod_status`: pods are Running, few or no restarts.
- `describe_workload`: current replica count; whether an HorizontalPodAutoscaler manages it.
- `query_prometheus`: CPU saturation across replicas:
  `sum(rate(container_cpu_usage_seconds_total{namespace="<ns>", pod=~"<name>-.*"}[5m])) / sum(kube_pod_container_resource_limits{namespace="<ns>", pod=~"<name>-.*", resource="cpu"})`
  A value above about 0.85 means the Deployment is saturated.
- `query_prometheus`: request rate is higher than its recent baseline (if the app exports request metrics).

## Do NOT use when
- Pods are crashing → use another runbook.
- Errors come from a dependency (database, downstream service) rather than this service's capacity. More replicas add more load to the dependency. Escalate.
- An HPA manages the Deployment. The HPA will undo the change; adjust the HPA instead (escalate).
- The cluster has no spare capacity: new pods would be `Pending` (`FailedScheduling` events).

## Action
- Action: `scale_deployment`.
- Parameters: `namespace`, `name`, `replicas`.
- Bounds: increase by at most 3 replicas over the current count; never scale down with this action.

## How to tell it worked
- All new replicas are Ready within the stabilization window.
- CPU saturation falls below 0.7 using the query above.
- Error rate returns below 1% (if the app exports request metrics):
  `sum(rate(http_requests_total{namespace="<ns>", status=~"5.."}[5m])) / sum(rate(http_requests_total{namespace="<ns>"}[5m])) < 0.01`
- The original alert stops firing.

## Escalate when
- New pods stay `Pending`, or errors persist after scaling (the bottleneck is elsewhere).
