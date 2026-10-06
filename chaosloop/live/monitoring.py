"""Steady-state monitoring for the lifecycle controller (HLD §3 component 3, §9).

Detection reads Alertmanager's API rather than waiting for its webhook: the controller runs on
the host and Alertmanager inside kind, and polling needs no inbound route to the host. The alerts
are the same objects either way, and go through the same Trigger Normalizer.
"""

from typing import Any, Protocol

import httpx

from chaosloop.live.gateway import Gateway
from chaosloop.live.normalizer import group_alerts


class AlertSource(Protocol):
    def firing(self) -> list[dict[str, Any]]: ...


class AlertmanagerSource:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9093",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    def firing(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            response = client.get(
                f"{self.base_url}/api/v2/alerts",
                params={"active": "true", "silenced": "false", "inhibited": "false"},
            )
        response.raise_for_status()
        return list(response.json())


def deployment_problems(
    gateway: Gateway, namespace: str, services: list[str] | None = None
) -> list[str]:
    """Deployments that are not fully rolled out and available - empty means steady."""
    problems = []
    for deployment in gateway.list("apps/v1", "Deployment", namespace):
        meta, spec = deployment.get("metadata") or {}, deployment.get("spec") or {}
        name = meta.get("name", "?")
        if services is not None and name not in services:
            continue
        status = deployment.get("status") or {}
        want = spec.get("replicas", 1)
        if (status.get("observedGeneration") or 0) < (meta.get("generation") or 0):
            problems.append(f"{name}: rollout not yet observed")
        elif (status.get("updatedReplicas") or 0) < want:
            problems.append(f"{name}: {status.get('updatedReplicas') or 0}/{want} updated")
        elif (status.get("availableReplicas") or 0) < want:
            problems.append(f"{name}: {status.get('availableReplicas') or 0}/{want} available")
        elif (status.get("replicas") or 0) > want:
            problems.append(f"{name}: old replicas still terminating")
    return problems


def alert_problems(
    alerts: list[dict[str, Any]], namespace: str, services: list[str] | None = None
) -> list[str]:
    groups = group_alerts(
        [a for a in alerts if (a.get("labels") or {}).get("namespace") == namespace]
    )
    return [
        f"{service}: {', '.join(sorted({a['labels'].get('alertname', '?') for a in group}))}"
        for (_, service), group in sorted(groups.items())
        if services is None or service in services
    ]
