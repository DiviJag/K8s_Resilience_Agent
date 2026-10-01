---
runbook_id: crashloop
title: CrashLoopBackOff triage
categories: [missing_env_var, bad_config_value, failing_liveness_probe, memory_limit_too_low]
---

## Always read the previous container

While a crash-looping container is backing off, its current logs are usually empty. The reason
it died is in the *previous* container's logs. Pair that with `lastState.terminated` on the
container status: the exit code and reason say who ended the process.

## What the exit code tells you

- `1` or another small non-zero code - the application exited on its own. Read its last log
  lines for the reason.
- `78` (EX_CONFIG) - the application rejected its configuration.
- `137` with reason `OOMKilled` - the kernel killed it for exceeding its memory limit. That is
  an OOM problem, not a config problem; see the OOMKilled runbook.
- `137` or `143` with reason `Error` and no OOM - something sent SIGKILL/SIGTERM. Most often
  the kubelet, because a liveness probe failed.

## Missing versus bad configuration

**missing_env_var**: the log names a variable that is required and absent ("is not set",
"KeyError", "missing required configuration"). Confirm against the deployment's `env` and
`envFrom` - the variable is in neither, and no referenced ConfigMap supplies it.

**bad_config_value**: the variable is present but its value is rejected ("must be an
integer", "invalid", "cannot parse"). Find which ConfigMap or env entry supplies the value.

## Liveness probe kills

**failing_liveness_probe**: events show `Liveness probe failed` followed by the kubelet
restarting the container, while the application's own logs look healthy up to the kill.
Compare the probe's port and path with what the container actually serves; a probe pointed at
a port nothing listens on kills a perfectly good process.
