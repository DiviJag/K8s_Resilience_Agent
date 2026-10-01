"""Promote a confirmed golden case into memory - the "confirmed -> memory" leg of the flywheel."""

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from chaosloop.memory.store import Incident, Provenance
from chaosloop.schemas.golden_case import GoldenCase
from chaosloop.schemas.taxonomy import SYMPTOM_TO_CATEGORIES, Category
from chaosloop.tools.replay_backend import BY_NAME

MAX_SIGNALS = 6
MAX_SIGNAL_CHARS = 220

_POD_SUFFIX = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}\b")
_GENERIC_EVENT = re.compile(r"^Error: \w+$")
_ERROR_LINE = re.compile(
    r"error|fatal|panic|exception|refused|denied|not set|missing|failed|cannot|unable|killed",
    re.I,
)


class PromotionError(ValueError):
    pass


def symptom_for(category: Category) -> str:
    for symptom, categories in SYMPTOM_TO_CATEGORIES.items():
        if category in categories:
            return symptom.value
    return "Unknown"


def extract_signals(evidence: dict[str, Any]) -> list[str]:
    """Pull the lines that distinguish this cause from its look-alikes.

    Ordered by how diagnostic each source usually is: warning events name the failing
    operation, error log lines carry the application's own reason, and pod state mostly
    repeats what the events already said.
    """
    candidates: list[str] = []

    for events in _per_object(evidence, "get_events"):
        if not isinstance(events, list):
            continue
        for event in events:
            if isinstance(event, dict) and event.get("type") == "Warning":
                message = str(event.get("message") or "").strip()
                if message and not _GENERIC_EVENT.match(message):
                    candidates.append(message)

    for logs in _per_object(evidence, "get_logs"):
        if not isinstance(logs, dict):
            continue
        for stream in ("previous", "current"):
            for line in str(logs.get(stream) or "").splitlines():
                if _ERROR_LINE.search(line):
                    candidates.append(line.strip())

    for pod in _per_object(evidence, "get_pod_status"):
        if not isinstance(pod, dict):
            continue
        for status in pod.get("containerStatuses") or []:
            for key in ("state", "lastState"):
                state = (status or {}).get(key) or {}
                waiting = state.get("waiting") or {}
                if waiting.get("reason") and waiting.get("message"):
                    candidates.append(f"{waiting['reason']}: {waiting['message']}")
                terminated = state.get("terminated") or {}
                if terminated.get("reason"):
                    candidates.append(
                        f"terminated {terminated['reason']} exitCode={terminated.get('exitCode')}"
                    )
        for condition in pod.get("conditions") or []:
            if condition.get("status") == "False" and condition.get("message"):
                candidates.append(f"{condition.get('reason')}: {condition['message']}")

    signals: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        # Pod-name hashes make otherwise identical signals look different and mean nothing to
        # the next incident, so they are dropped before dedup and storage.
        signal = _keep_innermost(_POD_SUFFIX.sub("", candidate))
        key = signal.lower()
        if key not in seen:
            seen.add(key)
            signals.append(signal)
        if len(signals) == MAX_SIGNALS:
            break
    return signals


def _per_object(evidence: dict[str, Any], tool: str) -> list[Any]:
    """One tool's evidence as a list of per-object outputs, unwrapping the `by_name` form."""
    value = evidence.get(tool)
    if isinstance(value, dict) and set(value) == {BY_NAME}:
        return list(value[BY_NAME].values())
    return [value]


def _keep_innermost(message: str) -> str:
    """Trim long messages from the front.

    Kubelet and containerd errors are wrapped Go errors - "failed to pull image X: rpc error:
    ... failed to copy: ... toomanyrequests: ..." - so the root cause is at the end and the
    front is boilerplate that every pull failure shares. Truncating the tail would keep the
    boilerplate and throw away the one phrase that identifies the cause.
    """
    if len(message) <= MAX_SIGNAL_CHARS:
        return message
    return "..." + message[-(MAX_SIGNAL_CHARS - 3) :].lstrip()


def default_resolution(case: GoldenCase) -> str:
    check = case.expected.fix_check
    if check.path == "*" and not check.must_exist:
        return "No change made; the condition had already cleared on its own."
    if check.must_exist:
        return f"Corrected {check.path} in the workload manifest."
    return f"Fixed outside the workload; {check.path} was left unchanged."


def incident_from_case(
    case: GoldenCase,
    confirmed_by: str,
    resolution: str | None = None,
    summary: str | None = None,
    confirmed_at: date | None = None,
) -> Incident:
    if case.split == "test":
        raise PromotionError(
            f"{case.case_id} is in the test split. A case may be in memory or in the test set, "
            "never both (HLD §6.5)."
        )
    trigger = case.trigger
    return Incident(
        incident_id=f"inc-{case.case_id}",
        category=case.expected.category,
        symptom=symptom_for(case.expected.category),
        summary=summary or f"{trigger.alertname} on {trigger.service} in {trigger.namespace}",
        signals=extract_signals(case.evidence) or [case.expected.root_cause],
        root_cause=case.expected.root_cause,
        resolution=resolution or default_resolution(case),
        provenance=Provenance(
            source="golden",
            source_case_id=case.case_id,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at or date.today(),
        ),
    )


def write_incident(incident: Incident, memory_dir: Path) -> Path:
    directory = memory_dir / "incidents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{incident.incident_id}.yaml"
    path.write_text(
        yaml.safe_dump(incident.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path
