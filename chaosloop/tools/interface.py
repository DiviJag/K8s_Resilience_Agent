"""The one-interface-two-backends seam (HLD §2.2).

Agent code is identical in Tier A and Tier B; only the backend behind this ABC changes.
`_TOOL_DEFS` is the single source of truth for the tool names used by the LLM tool schema,
the Recorder's evidence keys, and ReplayBackend's lookups - keeping all three in lockstep.
"""

from abc import ABC, abstractmethod
from typing import Any

NOT_AVAILABLE = "not available"

_NS = {"type": "string", "description": "Kubernetes namespace"}

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "get_pod_status",
        "description": (
            "Pod phase, container statuses, restart counts, and waiting/terminated reasons. "
            "Start here to turn an alert into a concrete symptom."
        ),
        "properties": {"namespace": _NS, "pod": {"type": "string", "description": "Pod name"}},
        "required": ["namespace", "pod"],
    },
    {
        "name": "get_events",
        "description": (
            "Kubernetes events for an object. Usually the only place the scheduler's or "
            "kubelet's reason for a failure (pull errors, scheduling failures) is spelled out."
        ),
        "properties": {
            "namespace": _NS,
            "object_name": {
                "type": "string",
                "description": "Object to filter on (pod, deployment). Omit for all events.",
            },
        },
        "required": ["namespace"],
    },
    {
        "name": "get_logs",
        "description": (
            "Container logs. Always returns both the current and the previous container's logs, "
            "so a crash-looping container whose current logs are empty is still diagnosable."
        ),
        "properties": {
            "namespace": _NS,
            "pod": {"type": "string", "description": "Pod name"},
            "container": {"type": "string", "description": "Container name, if the pod has many"},
        },
        "required": ["namespace", "pod"],
    },
    {
        "name": "get_deployment_spec",
        "description": (
            "Deployment spec: image, env, resources, probes, imagePullSecrets, selector, "
            "strategy and rollout status. This is where most config root causes are visible."
        ),
        "properties": {"namespace": _NS, "name": {"type": "string", "description": "Deployment"}},
        "required": ["namespace", "name"],
    },
    {
        "name": "get_service",
        "description": "Service spec: selector, ports and targetPort.",
        "properties": {"namespace": _NS, "name": {"type": "string", "description": "Service"}},
        "required": ["namespace", "name"],
    },
    {
        "name": "get_endpoints",
        "description": (
            "Endpoints backing a Service. Empty endpoints alongside healthy pods points at a "
            "selector or targetPort mismatch rather than a pod-level fault."
        ),
        "properties": {"namespace": _NS, "name": {"type": "string", "description": "Service"}},
        "required": ["namespace", "name"],
    },
    {
        "name": "get_pvc_status",
        "description": "PersistentVolumeClaim phase, storage class and bind status.",
        "properties": {"namespace": _NS, "name": {"type": "string", "description": "PVC name"}},
        "required": ["namespace", "name"],
    },
    {
        "name": "get_node_status",
        "description": (
            "Node conditions, taints, labels and allocatable vs requested resources. Use for "
            "Pending pods to tell insufficient capacity from a selector or taint problem."
        ),
        "properties": {
            "node": {"type": "string", "description": "Node name. Omit for all nodes."}
        },
        "required": [],
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(d["name"] for d in _TOOL_DEFS)

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": d["name"],
            "description": d["description"],
            "parameters": {
                "type": "object",
                "properties": d["properties"],
                "required": d["required"],
                "additionalProperties": False,
            },
        },
    }
    for d in _TOOL_DEFS
]


class UnknownToolError(ValueError):
    pass


class ToolBackend(ABC):
    """Investigation tools. Implementations must redact secrets before returning."""

    @abstractmethod
    def get_pod_status(self, namespace: str, pod: str) -> Any: ...

    @abstractmethod
    def get_events(self, namespace: str, object_name: str | None = None) -> Any: ...

    @abstractmethod
    def get_logs(self, namespace: str, pod: str, container: str | None = None) -> Any:
        """Returns {"current": str, "previous": str | None}.

        `--previous` is part of this one call rather than a separate tool so the agent cannot
        forget to ask for it on a crash-looping container (HLD §3, §12).
        """

    @abstractmethod
    def get_deployment_spec(self, namespace: str, name: str) -> Any: ...

    @abstractmethod
    def get_service(self, namespace: str, name: str) -> Any: ...

    @abstractmethod
    def get_endpoints(self, namespace: str, name: str) -> Any: ...

    @abstractmethod
    def get_pvc_status(self, namespace: str, name: str) -> Any: ...

    @abstractmethod
    def get_node_status(self, node: str | None = None) -> Any: ...


def dispatch(backend: ToolBackend, tool_name: str, arguments: dict[str, Any]) -> Any:
    if tool_name not in TOOL_NAMES:
        raise UnknownToolError(f"unknown tool {tool_name!r}; available: {', '.join(TOOL_NAMES)}")
    return getattr(backend, tool_name)(**arguments)
