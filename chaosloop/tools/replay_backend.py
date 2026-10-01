"""Tier A backend: returns evidence recorded in a golden case, with no cluster (HLD §2.2)."""

from typing import Any

from chaosloop.schemas.golden_case import GoldenCase
from chaosloop.tools.interface import NOT_AVAILABLE, ToolBackend, owned_by
from chaosloop.tools.redact import redact

# Evidence for a tool is normally one recorded output, returned whatever object is asked for.
# A case spanning several objects - a crash in one service caused by another - records
# `{by_name: {<object>: <output>, ...}}` instead, so asking about the wrong object returns
# "not available" rather than the right answer.
BY_NAME = "by_name"


class ReplayBackend(ToolBackend):
    def __init__(self, case: GoldenCase | dict[str, Any]) -> None:
        self._evidence = case.evidence if isinstance(case, GoldenCase) else case
        self.call_log: list[str] = []

    def _lookup(self, tool_name: str, name: str | None = None, owned: bool = False) -> Any:
        self.call_log.append(tool_name) #logging very call used later to grade the tool choice.
        value = self._evidence.get(tool_name, NOT_AVAILABLE)
        if isinstance(value, dict) and set(value) == {BY_NAME}:
            value = _select(value[BY_NAME], name, owned)
        # Recorded evidence is redacted at capture time; redacting again is cheap insurance
        # for hand-authored cases where someone pasted raw cluster output.
        return redact(value)

    def get_pod_status(self, namespace: str, pod: str) -> Any:
        return self._lookup("get_pod_status", pod)

    def get_events(self, namespace: str, object_name: str | None = None) -> Any:
        return self._lookup("get_events", object_name, owned=True)

    def get_logs(self, namespace: str, pod: str, container: str | None = None) -> Any:
        logs = self._lookup("get_logs", pod, owned=True)
        if isinstance(logs, str):
            return {"current": logs, "previous": None}
        return logs

    def get_deployment_spec(self, namespace: str, name: str) -> Any:
        return self._lookup("get_deployment_spec", name)

    def get_service(self, namespace: str, name: str) -> Any:
        return self._lookup("get_service", name)

    def get_endpoints(self, namespace: str, name: str) -> Any:
        return self._lookup("get_endpoints", name)

    def get_pvc_status(self, namespace: str, name: str) -> Any:
        return self._lookup("get_pvc_status", name)

    def get_node_status(self, node: str | None = None) -> Any:
        return self._lookup("get_node_status", node)


def _select(entries: dict[str, Any], name: str | None, owned: bool) -> Any:
    if name is not None and name in entries:
        return entries[name]
    if not owned:
        return NOT_AVAILABLE
    # Events and logs follow ownership the way LiveBackend does: asking about a deployment
    # returns its pods' records, and asking about a pod returns its deployment's.
    related = [
        value
        for key, value in entries.items()
        if name is None or owned_by(name, key) or owned_by(key, name)
    ]
    if not related:
        return NOT_AVAILABLE
    if all(isinstance(value, list) for value in related):
        return [item for value in related for item in value]
    return related[0]
