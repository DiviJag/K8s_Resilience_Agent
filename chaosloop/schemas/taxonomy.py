"""Fault taxonomy (HLD §5.1).

The symptom is what Kubernetes shows; the category is the root cause. Grading is always on
the category, so answering with a symptom ("the root cause is CrashLoopBackOff") is wrong.
"""

from enum import StrEnum


class Symptom(StrEnum):
    CRASH_LOOP_BACKOFF = "CrashLoopBackOff"
    IMAGE_PULL_BACKOFF = "ImagePullBackOff"
    OOM_KILLED = "OOMKilled"
    PENDING = "Pending"
    ROLLOUT_STALLED = "RolloutStalled"
    SERVICE_UNREACHABLE = "ServiceUnreachable"
    DNS_NETWORK = "DNSNetwork"
    PROBE_FAILURE = "ProbeFailure"
    STEADY_STATE = "SteadyState"


class Category(StrEnum):
    MISSING_ENV_VAR = "missing_env_var"
    BAD_CONFIG_VALUE = "bad_config_value"
    FAILING_LIVENESS_PROBE = "failing_liveness_probe"

    IMAGE_TAG_NOT_FOUND = "image_tag_not_found"
    REGISTRY_AUTH_MISSING = "registry_auth_missing"
    REGISTRY_UNREACHABLE = "registry_unreachable"
    REGISTRY_RATE_LIMITED = "registry_rate_limited"

    MEMORY_LIMIT_TOO_LOW = "memory_limit_too_low"
    MEMORY_LEAK = "memory_leak"

    INSUFFICIENT_RESOURCES = "insufficient_resources"
    NODE_SELECTOR_MISMATCH = "node_selector_mismatch"
    TAINT_NOT_TOLERATED = "taint_not_tolerated"
    PVC_UNBOUND = "pvc_unbound"
    QUOTA_EXCEEDED = "quota_exceeded"

    PROGRESS_DEADLINE_EXCEEDED = "progress_deadline_exceeded"

    SELECTOR_MISMATCH = "selector_mismatch"
    TARGET_PORT_MISMATCH = "target_port_mismatch"
    NETWORK_POLICY_BLOCK = "network_policy_block"

    DNS_RESOLUTION_FAILURE = "dns_resolution_failure"
    NETWORK_PARTITION = "network_partition"
    HIGH_LATENCY = "high_latency"

    READINESS_PROBE_MISCONFIGURED = "readiness_probe_misconfigured"

    NO_FAULT = "no_fault"
    UNKNOWN = "unknown"


SYMPTOM_TO_CATEGORIES: dict[Symptom, frozenset[Category]] = {
    Symptom.CRASH_LOOP_BACKOFF: frozenset(
        {
            Category.MISSING_ENV_VAR,
            Category.BAD_CONFIG_VALUE,
            Category.FAILING_LIVENESS_PROBE,
        }
    ),
    Symptom.IMAGE_PULL_BACKOFF: frozenset(
        {
            Category.IMAGE_TAG_NOT_FOUND,
            Category.REGISTRY_AUTH_MISSING,
            Category.REGISTRY_UNREACHABLE,
            Category.REGISTRY_RATE_LIMITED,
        }
    ),
    Symptom.OOM_KILLED: frozenset({Category.MEMORY_LIMIT_TOO_LOW, Category.MEMORY_LEAK}),
    Symptom.PENDING: frozenset(
        {
            Category.INSUFFICIENT_RESOURCES,
            Category.NODE_SELECTOR_MISMATCH,
            Category.TAINT_NOT_TOLERATED,
            Category.PVC_UNBOUND,
            Category.QUOTA_EXCEEDED,
        }
    ),
    Symptom.ROLLOUT_STALLED: frozenset({Category.PROGRESS_DEADLINE_EXCEEDED}),
    Symptom.SERVICE_UNREACHABLE: frozenset(
        {
            Category.SELECTOR_MISMATCH,
            Category.TARGET_PORT_MISMATCH,
            Category.NETWORK_POLICY_BLOCK,
        }
    ),
    Symptom.DNS_NETWORK: frozenset(
        {
            Category.DNS_RESOLUTION_FAILURE,
            Category.NETWORK_PARTITION,
            Category.HIGH_LATENCY,
        }
    ),
    Symptom.PROBE_FAILURE: frozenset({Category.READINESS_PROBE_MISCONFIGURED}),
    Symptom.STEADY_STATE: frozenset({Category.NO_FAULT, Category.UNKNOWN}),
}

# Cannot be injected safely or reliably against a live cluster; these exist only as recorded
# golden cases, which is one reason the offline tier is necessary (HLD §5.1).
RECORDED_ONLY: frozenset[Category] = frozenset({Category.REGISTRY_RATE_LIMITED})

# Naming either of these is not "inventing a cause", so neither counts as a false positive.
ABSTENTION: frozenset[Category] = frozenset({Category.NO_FAULT, Category.UNKNOWN})
