"""Turns a live failure into a golden case (HLD §3).

Runs *every* tool, not just the ones an agent happened to call, so replay never has gaps.
Whatever the agent asks for later is already on disk.
"""

from pathlib import Path
from typing import Any

import yaml

from chaosloop.schemas.golden_case import CaseMetadata, Expected, GoldenCase, Split
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.interface import NOT_AVAILABLE, TOOL_NAMES, ToolBackend, dispatch
from chaosloop.tools.redact import redact


class Recorder:
    def __init__(self, backend: ToolBackend) -> None:
        self.backend = backend

    def capture(
        self, trigger: Trigger, targets: dict[str, str | None] | None = None
    ) -> dict[str, Any]:
        targets = targets or {}
        namespace = trigger.namespace
        pod = targets.get("pod") or trigger.pod
        deployment = targets.get("deployment") or trigger.service
        service = targets.get("service") or trigger.service
        pvc = targets.get("pvc")
        node = targets.get("node")

        arguments: dict[str, dict[str, Any] | None] = {
            "get_pod_status": {"namespace": namespace, "pod": pod} if pod else None,
            # Scoped to the involved pod on purpose: namespace-wide events drag in warnings
            # from unrelated workloads, and a case labelled with one cause must not carry
            # another fault's evidence.
            "get_events": {"namespace": namespace, "object_name": pod} if pod else {
                "namespace": namespace
            },
            "get_logs": {"namespace": namespace, "pod": pod} if pod else None,
            "get_deployment_spec": {"namespace": namespace, "name": deployment},
            "get_service": {"namespace": namespace, "name": service},
            "get_endpoints": {"namespace": namespace, "name": service},
            "get_pvc_status": {"namespace": namespace, "name": pvc} if pvc else None,
            "get_node_status": {"node": node},
        }

        evidence: dict[str, Any] = {}
        for tool in TOOL_NAMES:
            kwargs = arguments.get(tool)
            if kwargs is None:
                evidence[tool] = NOT_AVAILABLE
                continue
            try:
                evidence[tool] = redact(dispatch(self.backend, tool, kwargs))
            except Exception as exc:
                evidence[tool] = f"error: {exc}"
        return evidence


def write_golden_case(
    case_id: str,
    split: Split,
    trigger: Trigger,
    evidence: dict[str, Any],
    expected: Expected,
    metadata: CaseMetadata,
    out_dir: Path,
) -> Path:
    """Validate before writing, so a bad recording fails at capture time, not in CI."""
    case = GoldenCase(
        case_id=case_id,
        split=split,
        trigger=trigger,
        evidence=evidence,
        expected=expected,
        metadata=metadata,
    )
    directory = out_dir / split
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case_id}.yaml"
    path.write_text(
        yaml.safe_dump(case.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path
