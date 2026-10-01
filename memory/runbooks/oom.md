---
runbook_id: oom
title: OOMKilled triage
categories: [memory_limit_too_low, memory_leak, no_fault]
---

## Confirm it is really OOM

Look for `lastState.terminated.reason: OOMKilled` (exit code 137) on the container status. An
exit code of 137 alone is not proof - a liveness-probe kill also ends in SIGKILL - so require
the OOMKilled reason.

## Limit too low versus a leak

The two look identical in a single snapshot. Time tells them apart:

- **memory_limit_too_low**: the container is killed at or shortly after startup, every time,
  at about the same point. Its normal working set simply does not fit under
  `resources.limits.memory`. Restarts are frequent and evenly spaced.
- **memory_leak**: the container runs normally for a long time - hours or days - then dies,
  restarts, and runs a long time again. Logs or metrics show usage climbing between restarts.
  Raising the limit only stretches the interval.

## Fixes

For a limit that is too low, raise `resources.limits.memory` (and the request to match) in the
workload. For a leak, raising the limit is a stopgap; the fix is in the application, and the
diagnosis should say so.

## Old OOMs are not current incidents

A container that was OOMKilled once, days ago, and has been Running and Ready since is not
failing now. If the restart is old and the pod is healthy, the alert is stale: **no_fault**.
