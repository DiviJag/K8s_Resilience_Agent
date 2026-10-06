"""One narrow door to the cluster for everything that changes it.

The lifecycle controller, config mutator and fix verifier all go through `Gateway`, so the
whole live loop can be exercised against an in-memory cluster in tests, and the real one can be
bound to a least-privilege injector service account rather than cluster admin (HLD §11).
"""

import copy
import os
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ObjectRef:
    api_version: str
    kind: str
    name: str
    namespace: str | None = None

    @classmethod
    def of(cls, obj: dict[str, Any]) -> "ObjectRef":
        meta = obj.get("metadata") or {}
        return cls(obj["apiVersion"], obj["kind"], meta["name"], meta.get("namespace"))

    def label(self) -> str:
        return f"{self.kind}/{self.name}"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "name": self.name,
            "namespace": self.namespace,
        }


class GatewayError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Gateway(Protocol):
    def get(self, ref: ObjectRef) -> dict[str, Any] | None: ...

    def list(
        self, api_version: str, kind: str, namespace: str | None = None
    ) -> list[dict[str, Any]]: ...

    def create(self, obj: dict[str, Any]) -> dict[str, Any]: ...

    def replace(self, obj: dict[str, Any], dry_run: bool = False) -> dict[str, Any]: ...

    def delete(self, ref: ObjectRef) -> None: ...


_SERVER_OWNED = ("uid", "creationTimestamp", "generation", "managedFields", "selfLink")


def for_replace(desired: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Prepare a desired object for PUT over the current one.

    Server-owned metadata and status come off; the current resourceVersion goes on, so the PUT
    is an optimistic update rather than a blind overwrite of whatever changed meanwhile.
    """
    obj = copy.deepcopy(desired)
    obj.pop("status", None)
    meta = obj.setdefault("metadata", {})
    for key in _SERVER_OWNED:
        meta.pop(key, None)
    meta["resourceVersion"] = (current.get("metadata") or {}).get("resourceVersion")
    return obj


def kubeconfig_path(explicit: str | None = None) -> str:
    """An empty KUBECONFIG (easy to get from a .env) reads as a literal empty path, not as
    unset, so it is resolved here rather than left to the client."""
    return (
        explicit
        or os.environ.get("KUBECONFIG")
        or os.path.join(os.path.expanduser("~"), ".kube", "config")
    )


class KubeGateway:
    """Gateway over the Kubernetes dynamic client - one code path for built-in kinds and the
    Chaos Mesh custom resources alike."""

    def __init__(self, kubeconfig: str | None = None, context: str | None = "kind-chaosloop"):
        from kubernetes import config, dynamic

        api = config.new_client_from_config(
            config_file=kubeconfig_path(kubeconfig), context=context
        )
        self._client = dynamic.DynamicClient(api)

    def _resource(self, api_version: str, kind: str) -> Any:
        return self._client.resources.get(api_version=api_version, kind=kind)

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        from kubernetes.dynamic.exceptions import DynamicApiError

        try:
            return fn(*args, **kwargs)
        except DynamicApiError as exc:
            raise GatewayError(f"{exc.status} {exc.reason}: {exc.summary()}", exc.status) from exc

    def get(self, ref: ObjectRef) -> dict[str, Any] | None:
        try:
            obj = self._call(
                self._resource(ref.api_version, ref.kind).get,
                name=ref.name,
                namespace=ref.namespace,
            )
        except GatewayError as exc:
            if exc.status == 404:
                return None
            raise
        return obj.to_dict()

    def list(
        self, api_version: str, kind: str, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        result = self._call(self._resource(api_version, kind).get, namespace=namespace)
        return [item.to_dict() for item in result.items]

    def create(self, obj: dict[str, Any]) -> dict[str, Any]:
        ref = ObjectRef.of(obj)
        resource = self._resource(ref.api_version, ref.kind)
        return self._call(resource.create, body=obj, namespace=ref.namespace).to_dict()

    def replace(self, obj: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        ref = ObjectRef.of(obj)
        resource = self._resource(ref.api_version, ref.kind)
        kwargs = {"dry_run": "All"} if dry_run else {}
        return self._call(
            resource.replace, body=obj, name=ref.name, namespace=ref.namespace, **kwargs
        ).to_dict()

    def delete(self, ref: ObjectRef) -> None:
        try:
            self._call(
                self._resource(ref.api_version, ref.kind).delete,
                name=ref.name,
                namespace=ref.namespace,
            )
        except GatewayError as exc:
            if exc.status != 404:
                raise
