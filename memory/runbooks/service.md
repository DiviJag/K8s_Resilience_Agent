---
runbook_id: service
title: Unreachable service triage
categories: [selector_mismatch, target_port_mismatch, network_policy_block, readiness_probe_misconfigured]
---

## Start from the Endpoints

A Service routes only to the addresses listed in its Endpoints. Read them before anything else:

- **No addresses at all**, while matching-looking pods are Running - the selector does not pick
  those pods up, or none of them are Ready.
- **Addresses only under `notReadyAddresses`** - the pods are selected but failing readiness.
- **Ready addresses present**, yet clients still fail - traffic reaches the pods and is refused
  or dropped, so look at ports and network policy.

## Selector mismatch

**selector_mismatch**: compare the Service `spec.selector` with the pod template labels. One
differing label or value - a version suffix, a renamed app label - leaves Endpoints empty even
though every pod is healthy. The fix is in the Service selector (or the labels).

## Port mismatch

**target_port_mismatch**: Endpoints are populated, but the Service `targetPort` is not a port
the container listens on (compare with `containerPort` and what the process binds). Clients
get connection refused. Pods look perfectly healthy.

## Readiness probe misconfigured

**readiness_probe_misconfigured**: pods are Running but `0/1` Ready, events show `Readiness
probe failed`, and the endpoints sit in `notReadyAddresses`. Compare the probe's port and path
with what the container serves. Unlike a liveness failure, nothing restarts - the pod just never
receives traffic.

## Network policy

**network_policy_block**: endpoints are ready and ports line up, but connections time out. Look
for a NetworkPolicy selecting the pods whose ingress rules exclude the caller.
