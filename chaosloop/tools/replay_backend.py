"""Tier A backend: returns evidence recorded in a golden case, with no cluster (HLD §2.2)."""

from typing import Any

from chaosloop.schemas.golden_case import GoldenCase
from chaosloop.tools.interface import NOT_AVAILABLE, ToolBackend
from chaosloop.tools.redact import redact


class ReplayBackend(ToolBackend):
    def __init__(self, case: GoldenCase | dict[str, Any]) -> None:
        self._evidence = case.evidence if isinstance(case, GoldenCase) else case
        self.call_log: list[str] = []

    def _lookup(self, tool_name: str) -> Any:
        self.call_log.append(tool_name) #logging very call used later to grade the tool choice.
        # Recorded evidence is redacted at capture time; redacting again is cheap insurance
        # for hand-authored cases where someone pasted raw cluster output.
        return redact(self._evidence.get(tool_name, NOT_AVAILABLE))

    def get_pod_status(self, namespace: str, pod: str) -> Any:
        return self._lookup("get_pod_status")

    def get_events(self, namespace: str, object_name: str | None = None) -> Any:
        return self._lookup("get_events")

    def get_logs(self, namespace: str, pod: str, container: str | None = None) -> Any:
        logs = self._lookup("get_logs")
        if isinstance(logs, str):
            return {"current": logs, "previous": None}
        return logs

    def get_deployment_spec(self, namespace: str, name: str) -> Any:
        return self._lookup("get_deployment_spec")

    def get_service(self, namespace: str, name: str) -> Any:
        return self._lookup("get_service")

    def get_endpoints(self, namespace: str, name: str) -> Any:
        return self._lookup("get_endpoints")

    def get_pvc_status(self, namespace: str, name: str) -> Any:
        return self._lookup("get_pvc_status")

    def get_node_status(self, node: str | None = None) -> Any:
        return self._lookup("get_node_status")
