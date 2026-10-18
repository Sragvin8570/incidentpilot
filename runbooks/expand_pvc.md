---
id: rb-expand-pvc
title: Expand a PersistentVolumeClaim that is almost full
linked_action_id: expand_pvc
risk_level: high
applies_to_kinds: [PersistentVolumeClaim]
---

# Expand a PersistentVolumeClaim that is almost full

## Symptoms
- Alert `PVCAlmostFull`.
- Application errors such as "no space left on device", failed writes, or a database refusing writes.
- Usually affects StatefulSets (databases, queues).

## How to confirm
- `query_prometheus`: volume usage above 85%:
  `kubelet_volume_stats_used_bytes{namespace="<ns>", persistentvolumeclaim="<pvc>"} / kubelet_volume_stats_capacity_bytes{namespace="<ns>", persistentvolumeclaim="<pvc>"}`
- `query_prometheus`: growth rate, to tell steady growth from a sudden spike (a spike can mean runaway logs or a bug).
- `get_pod_logs`: "no space left on device" or similar.
- `describe_workload` on the PVC: current size and StorageClass. The StorageClass must allow volume expansion.

## Do NOT use when
- The StorageClass does not allow volume expansion.
- Usage jumped suddenly because of runaway logs or temporary files. Expanding only delays the problem. Escalate so someone can clean up and fix the cause.
- The volume was already expanded by this action in the last 24 hours.

## Action
- Action: `expand_pvc`.
- Parameters: `namespace`, `name`, `new_size`.
- Bounds: at most 1.5x the current size per action.
- Risk: always high. Volume size can only grow, never shrink back, and storage costs rise. This action always requires human approval.

## How to tell it worked
- The PVC's reported capacity equals the new size (some storage drivers need a pod restart to finish resizing the filesystem).
- Usage ratio falls below 75% using the query above.
- Write errors stop in the logs; the original alert stops firing.

## Escalate when
- Capacity does not change after the action, or usage keeps rising quickly at the new size.
