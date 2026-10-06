"""An in-memory Kubernetes for exercising the live loop without a cluster."""

import copy
from typing import Any

from chaosloop.live.gateway import GatewayError, ObjectRef


class FakeCluster:
    """Implements the Gateway protocol with optimistic concurrency, like the API server.

    `validators` reject writes the real API would refuse (immutable fields, invalid specs);
    `on_write` lets a test model controllers reacting to a change.
    """

    def __init__(self) -> None:
        self.objects: dict[ObjectRef, dict[str, Any]] = {}
        self.writes: list[tuple[str, ObjectRef, bool]] = []
        self.validators: list[Any] = []
        self.on_write: list[Any] = []
        self._version = 0

    def _bump(self, obj: dict[str, Any]) -> dict[str, Any]:
        self._version += 1
        obj.setdefault("metadata", {})["resourceVersion"] = str(self._version)
        return obj

    def add(self, obj: dict[str, Any]) -> dict[str, Any]:
        stored = self._bump(copy.deepcopy(obj))
        self.objects[ObjectRef.of(stored)] = stored
        return copy.deepcopy(stored)

    def _validate(self, obj: dict[str, Any], old: dict[str, Any] | None) -> None:
        for validator in self.validators:
            message = validator(obj, old)
            if message:
                raise GatewayError(message, 422)

    def get(self, ref: ObjectRef) -> dict[str, Any] | None:
        obj = self.objects.get(ref)
        return copy.deepcopy(obj) if obj is not None else None

    def list(
        self, api_version: str, kind: str, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(o)
            for r, o in self.objects.items()
            if r.api_version == api_version and r.kind == kind
            and (namespace is None or r.namespace == namespace)
        ]

    def create(self, obj: dict[str, Any]) -> dict[str, Any]:
        ref = ObjectRef.of(obj)
        if ref in self.objects:
            raise GatewayError(f"{ref.label()} already exists", 409)
        self._validate(obj, None)
        self.writes.append(("create", ref, False))
        created = self.add(obj)
        for hook in self.on_write:
            hook(self, "create", ref)
        return created

    def replace(self, obj: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        ref = ObjectRef.of(obj)
        current = self.objects.get(ref)
        if current is None:
            raise GatewayError(f"{ref.label()} not found", 404)
        sent = (obj.get("metadata") or {}).get("resourceVersion")
        if sent != current["metadata"]["resourceVersion"]:
            raise GatewayError(f"{ref.label()} was modified meanwhile", 409)
        self._validate(obj, current)
        self.writes.append(("replace", ref, dry_run))
        if dry_run:
            return copy.deepcopy(obj)
        stored = self._bump(copy.deepcopy(obj))
        # Like the API server: a PUT to the main resource leaves status alone.
        if "status" in current and "status" not in stored:
            stored["status"] = copy.deepcopy(current["status"])
        self.objects[ref] = stored
        for hook in self.on_write:
            hook(self, "replace", ref)
        return copy.deepcopy(stored)

    def delete(self, ref: ObjectRef) -> None:
        if self.objects.pop(ref, None) is not None:
            self.writes.append(("delete", ref, False))


NS = "chaosloop-lab"


def deployment(name: str, image: str = "localhost:5001/busybox:1.36", **pod: Any) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NS},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {
                            "name": name.split("-")[0],
                            "image": image,
                            "env": [{"name": "DATABASE_URL", "value": "postgres://db"}],
                            "ports": [{"containerPort": 8080}],
                        }
                    ],
                    **pod,
                },
            },
        },
        "status": {"replicas": 2, "availableReplicas": 2, "updatedReplicas": 2},
    }


def service(name: str, selector: str | None = None, target_port: int = 8080) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": NS},
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": selector or name},
            "ports": [{"port": 80, "targetPort": target_port}],
        },
    }


def node(name: str, control_plane: bool = False) -> dict:
    labels = {"kubernetes.io/hostname": name}
    taints = []
    if control_plane:
        labels["node-role.kubernetes.io/control-plane"] = ""
        taints = [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {"name": name, "labels": labels},
        "spec": {"taints": taints},
    }


def pod(name: str, phase: str = "Running") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NS},
        "status": {"phase": phase},
    }


def lab_cluster() -> FakeCluster:
    cluster = FakeCluster()
    for name in ("billing-service", "cart-service", "analytics-worker"):
        cluster.add(deployment(name))
        cluster.add(service(name))
    cluster.add({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "billing-config", "namespace": NS},
        "data": {"MAX_WORKERS": "4"},
    })
    for name, cp in (("chaosloop-control-plane", True), ("chaosloop-worker", False),
                     ("chaosloop-worker2", False)):
        cluster.add(node(name, cp))
    for index in range(5):
        cluster.add(pod(f"p{index}"))
    cluster.add(pod("done", phase="Succeeded"))
    return cluster
