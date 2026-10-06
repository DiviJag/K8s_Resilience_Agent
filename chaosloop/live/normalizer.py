"""Trigger Normalizer (HLD §3 component 3, §2.2).

A live Alertmanager alert and a recorded golden-case trigger must reach the agent as the same
`Trigger`, so the agent cannot tell - and its behaviour cannot depend on - which tier it is in.
Alertmanager speaks two shapes: the webhook payload it pushes, and the alert list its API
returns. Both reduce to the same per-alert dict, and both group into one trigger per incident.
"""

import re
from collections import defaultdict
from typing import Any

from chaosloop.schemas.trigger import Trigger

_POD_HASH = re.compile(r"^(?P<owner>.+?)-[a-z0-9]{6,10}-[a-z0-9]{5}$")


def is_firing(alert: dict[str, Any]) -> bool:
    status = alert.get("status")
    if isinstance(status, dict):
        return status.get("state") == "active"
    return status in (None, "firing")


def service_of(labels: dict[str, str]) -> str:
    """The workload an alert is about.

    Rules set a `service` label where they can; kube-state-metrics series only carry the pod,
    so the deployment name is recovered by stripping the ReplicaSet and pod hashes.
    """
    for key in ("service", "deployment", "app"):
        if labels.get(key):
            return labels[key]
    pod = labels.get("pod", "")
    match = _POD_HASH.match(pod)
    return match.group("owner") if match else pod or "unknown"


def group_alerts(alerts: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group firing alerts per (namespace, service) - one incident, one investigation."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for alert in alerts:
        if not is_firing(alert):
            continue
        labels = alert.get("labels") or {}
        groups[(labels.get("namespace", "default"), service_of(labels))].append(alert)
    return dict(groups)


def trigger_for_group(alerts: list[dict[str, Any]]) -> Trigger:
    """The earliest alert in a group names the incident; the rest ride along as context."""
    primary = min(alerts, key=lambda a: a.get("startsAt") or "")
    labels = primary.get("labels") or {}
    annotations = primary.get("annotations") or {}
    extra: dict[str, Any] = {}
    if labels.get("severity"):
        extra["severity"] = labels["severity"]
    if annotations.get("summary"):
        extra["summary"] = annotations["summary"]
    if primary.get("startsAt"):
        extra["started_at"] = primary["startsAt"]
    others = sorted({(a.get("labels") or {}).get("alertname", "") for a in alerts} - {
        labels.get("alertname", "")
    })
    if others:
        extra["also_firing"] = ", ".join(o for o in others if o)
    return Trigger(
        alertname=labels.get("alertname", "UnknownAlert"),
        service=service_of(labels),
        namespace=labels.get("namespace", "default"),
        pod=labels.get("pod") or None,
        extra=extra,
    )


def triggers_from_alerts(alerts: list[dict[str, Any]]) -> list[Trigger]:
    """From Alertmanager's API (`GET /api/v2/alerts`)."""
    groups = group_alerts(alerts)
    return [trigger_for_group(group) for _, group in sorted(groups.items())]


def triggers_from_webhook(payload: dict[str, Any]) -> list[Trigger]:
    """From an Alertmanager webhook push. A push can carry several of our incidents when the
    receiver groups more coarsely than we do, so it is regrouped rather than trusted as one."""
    return triggers_from_alerts(list(payload.get("alerts") or []))
