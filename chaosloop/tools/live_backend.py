"""Tier B backend: the same tools, against a real cluster (HLD §2.2, §11).

Uses the Kubernetes API via a scoped, read-only service account. Every result is projected
down to the same shape the golden cases record - so a replayed case and a live call are
interchangeable - and passed through the same redaction as the Recorder.
"""

import json
import os
import re
from typing import Any

from chaosloop.tools.interface import NOT_AVAILABLE, ToolBackend
from chaosloop.tools.redact import redact

MAX_LOG_CHARS = 8_000
LOG_TAIL_LINES = 200


class LiveBackend(ToolBackend):
    def __init__(
        self,
        kubeconfig: str | None = None,
        context: str = "kind-chaosloop",
        core_api: Any = None,
        apps_api: Any = None,
    ) -> None:
        if core_api is not None and apps_api is not None:
            self._core, self._apps = core_api, apps_api
            self._exception: type[Exception] = Exception
            return

        try:
            from kubernetes import client, config
            from kubernetes.client.exceptions import ApiException
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "LiveBackend needs the kubernetes client: uv sync --extra live"
            ) from exc

        # Resolved explicitly: an empty KUBECONFIG (easy to get from a .env) is read by the
        # client as a literal empty path rather than as "unset", and fails to find any config.
        config_file = (
            kubeconfig
            or os.environ.get("KUBECONFIG")
            or os.path.join(os.path.expanduser("~"), ".kube", "config")
        )
        config.load_kube_config(config_file=config_file, context=context)
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()
        self._exception = ApiException

    def _guard(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """A missing object is evidence, not a crash - the agent should see it as such."""
        try:
            return fn(*args, **kwargs)
        except self._exception as exc:
            if getattr(exc, "status", None) == 404:
                return NOT_AVAILABLE
            return f"error: {_api_message(exc)}"
        except Exception as exc:
            return f"error: {exc}"

    def get_pod_status(self, namespace: str, pod: str) -> Any:
        obj = self._guard(self._core.read_namespaced_pod, name=pod, namespace=namespace)
        if isinstance(obj, str):
            return obj
        return redact(
            {
                "name": obj.metadata.name,
                "namespace": obj.metadata.namespace,
                "phase": obj.status.phase,
                "startTime": _iso(obj.status.start_time),
                "nodeName": obj.spec.node_name,
                "conditions": [
                    {
                        "type": c.type,
                        "status": c.status,
                        "reason": c.reason,
                        "message": c.message,
                    }
                    for c in (obj.status.conditions or [])
                ],
                "containerStatuses": [
                    _container_status(cs) for cs in (obj.status.container_statuses or [])
                ],
            }
        )

    def get_events(self, namespace: str, object_name: str | None = None) -> Any:
        selector = f"involvedObject.name={object_name}" if object_name else None
        result = self._guard(
            self._core.list_namespaced_event, namespace=namespace, field_selector=selector
        )
        if isinstance(result, str):
            return result
        events = sorted(result.items, key=lambda e: e.last_timestamp or e.event_time or 0)
        return redact(
            [
                {
                    "type": e.type,
                    "reason": e.reason,
                    "count": e.count,
                    "message": e.message,
                    "lastTimestamp": _iso(e.last_timestamp),
                    "involvedObject": getattr(e.involved_object, "name", None),
                }
                for e in events
            ]
        )

    def get_logs(self, namespace: str, pod: str, container: str | None = None) -> Any:
        current = self._read_log(namespace, pod, container, previous=False)
        previous = self._read_log(namespace, pod, container, previous=True)
        return redact({"current": current, "previous": previous})

    def _read_log(
        self, namespace: str, pod: str, container: str | None, previous: bool
    ) -> str | None:
        result = self._guard(
            self._core.read_namespaced_pod_log,
            name=pod,
            namespace=namespace,
            container=container,
            previous=previous,
            tail_lines=LOG_TAIL_LINES,
        )
        if result is None or result == NOT_AVAILABLE:
            return None
        text = str(result)
        if len(text) > MAX_LOG_CHARS:
            text = f"...[truncated]\n{text[-MAX_LOG_CHARS:]}"
        return text

    def get_deployment_spec(self, namespace: str, name: str) -> Any:
        obj = self._guard(self._apps.read_namespaced_deployment, name=name, namespace=namespace)
        if isinstance(obj, str):
            return obj
        pod_spec = obj.spec.template.spec
        return redact(
            {
                "metadata": {
                    "name": obj.metadata.name,
                    "namespace": obj.metadata.namespace,
                    "generation": obj.metadata.generation,
                },
                "spec": {
                    "replicas": obj.spec.replicas,
                    "progressDeadlineSeconds": obj.spec.progress_deadline_seconds,
                    "strategy": {"type": getattr(obj.spec.strategy, "type", None)},
                    "selector": {"matchLabels": obj.spec.selector.match_labels},
                    "template": {
                        "metadata": {"labels": obj.spec.template.metadata.labels},
                        "spec": {
                            "nodeSelector": pod_spec.node_selector,
                            "tolerations": [
                                {"key": t.key, "operator": t.operator, "effect": t.effect}
                                for t in (pod_spec.tolerations or [])
                            ],
                            "imagePullSecrets": [
                                {"name": s.name} for s in (pod_spec.image_pull_secrets or [])
                            ],
                            "volumes": [_volume(v) for v in (pod_spec.volumes or [])],
                            "containers": [_container(c) for c in pod_spec.containers],
                        },
                    },
                },
                "status": {
                    "replicas": obj.status.replicas,
                    "readyReplicas": obj.status.ready_replicas,
                    "updatedReplicas": obj.status.updated_replicas,
                    "unavailableReplicas": obj.status.unavailable_replicas,
                    "conditions": [
                        {
                            "type": c.type,
                            "status": c.status,
                            "reason": c.reason,
                            "message": c.message,
                        }
                        for c in (obj.status.conditions or [])
                    ],
                },
            }
        )

    def get_service(self, namespace: str, name: str) -> Any:
        obj = self._guard(self._core.read_namespaced_service, name=name, namespace=namespace)
        if isinstance(obj, str):
            return obj
        return redact(
            {
                "metadata": {"name": obj.metadata.name, "namespace": obj.metadata.namespace},
                "spec": {
                    "type": obj.spec.type,
                    "selector": obj.spec.selector,
                    "ports": [
                        {
                            "port": p.port,
                            "targetPort": p.target_port,
                            "protocol": p.protocol,
                            "name": p.name,
                        }
                        for p in (obj.spec.ports or [])
                    ],
                },
            }
        )

    def get_endpoints(self, namespace: str, name: str) -> Any:
        obj = self._guard(self._core.read_namespaced_endpoints, name=name, namespace=namespace)
        if isinstance(obj, str):
            return obj
        return redact(
            {
                "metadata": {"name": obj.metadata.name, "namespace": obj.metadata.namespace},
                "subsets": [
                    {
                        "addresses": [{"ip": a.ip} for a in (s.addresses or [])],
                        "notReadyAddresses": [
                            {"ip": a.ip} for a in (s.not_ready_addresses or [])
                        ],
                        "ports": [{"port": p.port, "name": p.name} for p in (s.ports or [])],
                    }
                    for s in (obj.subsets or [])
                ],
            }
        )

    def get_pvc_status(self, namespace: str, name: str) -> Any:
        obj = self._guard(
            self._core.read_namespaced_persistent_volume_claim, name=name, namespace=namespace
        )
        if isinstance(obj, str):
            return obj
        return redact(
            {
                "metadata": {"name": obj.metadata.name, "namespace": obj.metadata.namespace},
                "spec": {
                    "storageClassName": obj.spec.storage_class_name,
                    "accessModes": obj.spec.access_modes,
                    "resources": {
                        "requests": dict(getattr(obj.spec.resources, "requests", None) or {})
                    },
                },
                "status": {"phase": obj.status.phase},
            }
        )

    def get_node_status(self, node: str | None = None) -> Any:
        result = self._guard(self._core.list_node)
        if isinstance(result, str):
            return result
        nodes = [n for n in result.items if node is None or n.metadata.name == node]
        requests = self._requests_by_node()
        return redact(
            [
                {
                    "name": n.metadata.name,
                    "labels": n.metadata.labels,
                    "conditions": [
                        {"type": c.type, "status": c.status, "reason": c.reason}
                        for c in (n.status.conditions or [])
                        if c.type == "Ready" or c.status == "True"
                    ],
                    "taints": [
                        {"key": t.key, "value": t.value, "effect": t.effect}
                        for t in (n.spec.taints or [])
                    ],
                    "allocatable": dict(n.status.allocatable or {}),
                    "allocatedRequests": requests.get(n.metadata.name, {}),
                }
                for n in nodes
            ]
        )

    def find_unready_pod(self, namespace: str, service: str) -> str:
        """Recorder helper, not part of ToolBackend - the agent never gets to enumerate pods."""
        pods = self._core.list_namespaced_pod(
            namespace=namespace, label_selector=f"app={service}"
        )
        unready = [
            p
            for p in pods.items
            if p.status.phase != "Running"
            or not all(cs.ready for cs in (p.status.container_statuses or []))
        ]
        chosen = next(iter(unready or pods.items), None)
        return chosen.metadata.name if chosen else ""

    def _requests_by_node(self) -> dict[str, dict[str, str]]:
        """Scheduling questions need requested vs allocatable, not allocatable alone."""
        result = self._guard(self._core.list_pod_for_all_namespaces)
        if isinstance(result, str):
            return {}
        totals: dict[str, dict[str, float]] = {}
        for pod in result.items:
            name = pod.spec.node_name
            if not name or pod.status.phase not in ("Running", "Pending"):
                continue
            bucket = totals.setdefault(name, {"cpu": 0.0, "memory": 0.0})
            for container in pod.spec.containers:
                requested = getattr(container.resources, "requests", None) or {}
                bucket["cpu"] += _quantity(requested.get("cpu"))
                bucket["memory"] += _quantity(requested.get("memory"))
        return {
            node: {"cpu": f"{values['cpu']:.3g}", "memory": f"{values['memory'] / 2**20:.0f}Mi"}
            for node, values in totals.items()
        }


def _api_message(exc: Any) -> str:
    """Prefer the API error body over the bare HTTP reason.

    The body is where the diagnostic text lives - a log request against a container that never
    started says "is waiting to start: trying and failing to pull image", while the reason says
    only "Bad Request", which is evidence the agent cannot use.
    """
    body = getattr(exc, "body", None)
    if body:
        try:
            message = json.loads(body).get("message")
            if message:
                return str(message)
        except (ValueError, TypeError, AttributeError):
            pass
    return str(getattr(exc, "reason", None) or exc)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _container_status(cs: Any) -> dict[str, Any]:
    return {
        "name": cs.name,
        "ready": cs.ready,
        "restartCount": cs.restart_count,
        "image": cs.image,
        "state": _container_state(cs.state),
        "lastState": _container_state(cs.last_state),
    }


def _container_state(state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    if state.waiting:
        return {"waiting": {"reason": state.waiting.reason, "message": state.waiting.message}}
    if state.terminated:
        terminated = state.terminated
        return {
            "terminated": {
                "exitCode": terminated.exit_code,
                "reason": terminated.reason,
                "startedAt": _iso(terminated.started_at),
                "finishedAt": _iso(terminated.finished_at),
            }
        }
    if state.running:
        return {"running": {"startedAt": _iso(state.running.started_at)}}
    return None


def _container(c: Any) -> dict[str, Any]:
    resources = getattr(c, "resources", None)
    return {
        "name": c.name,
        "image": c.image,
        "command": c.command,
        "args": c.args,
        "ports": [{"containerPort": p.container_port} for p in (c.ports or [])],
        "env": [_env(e) for e in (c.env or [])],
        "envFrom": [
            {"configMapRef": getattr(e.config_map_ref, "name", None)}
            for e in (c.env_from or [])
            if e.config_map_ref
        ],
        "resources": {
            "requests": dict(getattr(resources, "requests", None) or {}),
            "limits": dict(getattr(resources, "limits", None) or {}),
        },
        "livenessProbe": _probe(c.liveness_probe),
        "readinessProbe": _probe(c.readiness_probe),
        "volumeMounts": [
            {"name": m.name, "mountPath": m.mount_path} for m in (c.volume_mounts or [])
        ],
    }


def _env(e: Any) -> dict[str, Any]:
    if e.value_from is not None:
        source = e.value_from
        if source.secret_key_ref is not None:
            # The reference stays visible; the value is never fetched (HLD §11).
            return {
                "name": e.name,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": source.secret_key_ref.name,
                        "key": source.secret_key_ref.key,
                    }
                },
            }
        if source.config_map_key_ref is not None:
            return {
                "name": e.name,
                "valueFrom": {
                    "configMapKeyRef": {
                        "name": source.config_map_key_ref.name,
                        "key": source.config_map_key_ref.key,
                    }
                },
            }
    return {"name": e.name, "value": e.value}


def _probe(p: Any) -> dict[str, Any] | None:
    if p is None:
        return None
    probe: dict[str, Any] = {
        "initialDelaySeconds": p.initial_delay_seconds,
        "periodSeconds": p.period_seconds,
        "failureThreshold": p.failure_threshold,
    }
    if p.http_get is not None:
        probe["httpGet"] = {"path": p.http_get.path, "port": p.http_get.port}
    if p.tcp_socket is not None:
        probe["tcpSocket"] = {"port": p.tcp_socket.port}
    if p.exec is not None:
        probe["exec"] = {"command": p.exec.command}
    return probe


def _volume(v: Any) -> dict[str, Any]:
    volume: dict[str, Any] = {"name": v.name}
    if v.persistent_volume_claim is not None:
        volume["persistentVolumeClaim"] = {"claimName": v.persistent_volume_claim.claim_name}
    if v.config_map is not None:
        volume["configMap"] = {"name": v.config_map.name}
    if v.empty_dir is not None:
        volume["emptyDir"] = {"medium": v.empty_dir.medium}
    return volume


_QUANTITY = re.compile(r"([0-9.]+)\s*([a-zA-Z]*)")

_SUFFIX_FACTORS: dict[str, float] = {
    "": 1.0,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
}


def _quantity(value: Any) -> float:
    """Parse a Kubernetes quantity: cpu in cores, memory in bytes.

    Deliberately self-contained rather than importing kubernetes.utils - the parser has to
    work whether or not the optional client is installed, because silently reporting zero
    requested memory would tell the agent that every node is empty.
    """
    if value is None:
        return 0.0
    match = _QUANTITY.fullmatch(str(value).strip())
    if not match:
        return 0.0
    number, suffix = match.groups()
    return float(number) * _SUFFIX_FACTORS.get(suffix, 1.0)
