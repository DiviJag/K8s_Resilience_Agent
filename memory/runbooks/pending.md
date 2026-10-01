---
runbook_id: pending
title: Pending pod triage
categories: [insufficient_resources, node_selector_mismatch, taint_not_tolerated, pvc_unbound, quota_exceeded]
---

## Read the FailedScheduling message

A Pending pod with no node assigned was rejected by the scheduler, and the `FailedScheduling`
event says why, node by node: "0/3 nodes are available: ...". Each clause names a reason and
how many nodes it applies to.

## Phrase to cause

- `Insufficient memory` / `Insufficient cpu` - nodes exist but none has room for the request:
  **insufficient_resources**. Compare the pod's requests with each node's allocatable capacity
  minus what is already requested.
- `didn't match Pod's node affinity/selector` - the pod demands node labels no node carries:
  **node_selector_mismatch**. Compare `nodeSelector` / affinity with the node labels.
- `had untolerated taint` - nodes are tainted and the pod lacks the toleration:
  **taint_not_tolerated**.
- `unbound immediate PersistentVolumeClaims` / `pod has unbound` - the volume claim never
  bound: **pvc_unbound**. Check the claim's storage class exists and can provision.

## Mixed messages: ignore the control plane

Control-plane nodes normally carry a NoSchedule taint, so almost every FailedScheduling message
includes "1 node(s) had untolerated taint(s)" for them. That clause is noise. The cause is the
reason given for the *worker* nodes - the ones that could otherwise have run the pod. Only call
it taint_not_tolerated when the workers themselves are rejecting on a taint.

## Quota failures leave no Pending pod

When a ResourceQuota is exhausted the pod is never created, so there is nothing Pending to
inspect. The evidence is a `FailedCreate` event on the ReplicaSet saying `exceeded quota`,
and a deployment whose ready count is below its desired replicas: **quota_exceeded**.

## Where the fix belongs

Selector, request size and missing tolerations are the workload's own configuration: patch the
manifest. A request genuinely larger than any node's capacity should be resized unless the
cluster is meant to grow.
