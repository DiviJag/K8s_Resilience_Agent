"""Steady-state monitoring for the lifecycle controller (HLD §3 component 3, §9).

Detection reads Alertmanager's API rather than waiting for its webhook: the controller runs on
the host and Alertmanager inside kind, and polling needs no inbound route to the host. The alerts
are the same objects either way, and go through the same Trigger Normalizer.

Steady-state has two layers. The pod-level layer catches obvious things - a Deployment with
unavailable replicas, old replicas still terminating - and is always on. The upstream-rate layer
catches the thing pod availability cannot: a running service whose own calls to its upstream are
still erroring, which is how a Chaos Mesh DNS or network injection can leak after rollback. It
reads short-window error rates from Prometheus and is optional, because not every lab runs with
Prometheus wired in.
"""

from typing import Any, Protocol

import httpx

from chaosloop.live.gateway import Gateway
from chaosloop.live.normalizer import group_alerts


class AlertSource(Protocol):
    def firing(self) -> list[dict[str, Any]]: ...


class UpstreamProbe(Protocol):
    """Returns {service: observed_error_rate in [0,1]} for the services the caller asked about.

    An empty dict (no series matched) is treated as healthy, not as missing data: a service with
    no traffic in the window has no observed error, which is the correct steady-state answer.
    """

    def error_rate(self, namespace: str, services: list[str]) -> dict[str, float]: ...


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


DEFAULT_PROM_QUERY = (
    # Classic Spring/Micrometer/Prometheus client-library shape. If the lab's services expose a
    # different metric name, swap the template on the probe rather than this default.
    'sum by (service) ('
    '  rate(http_server_requests_seconds_count'
    '{{namespace="{namespace}",service=~"{services}",outcome="SERVER_ERROR"}}[{window}])'
    ') / clamp_min('
    'sum by (service) ('
    '  rate(http_server_requests_seconds_count'
    '{{namespace="{namespace}",service=~"{services}"}}[{window}])'
    '), 1e-9)'
)


class PrometheusProbe:
    """UpstreamProbe backed by Prometheus's instant-query API.

    The default query is 5xx rate / total over a short window, keyed by `service`. The template
    takes `{namespace}`, `{services}` (a `|`-joined regex) and `{window}` and is substituted
    before the HTTP call - callers can override the whole template to fit their metric shape
    without touching this file.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9090",
        query_template: str = DEFAULT_PROM_QUERY,
        window: str = "1m",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.query_template = query_template
        self.window = window
        self.timeout = timeout
        self.transport = transport

    def error_rate(self, namespace: str, services: list[str]) -> dict[str, float]:
        if not services:
            return {}
        regex = "|".join(sorted({_escape_regex(s) for s in services}))
        query = self.query_template.format(namespace=namespace, services=regex, window=self.window)
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            response = client.get(f"{self.base_url}/api/v1/query", params={"query": query})
        response.raise_for_status()
        payload = response.json() or {}
        if payload.get("status") != "success":
            return {}
        result = ((payload.get("data") or {}).get("result")) or []
        out: dict[str, float] = {}
        for row in result:
            svc = ((row.get("metric") or {}).get("service"))
            value = (row.get("value") or [None, None])[1]
            if not svc or value is None:
                continue
            try:
                out[svc] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
        return out


_PROM_REGEX_ESCAPE = str.maketrans({ch: "\\" + ch for ch in r".+*?()[]{}|^$\\"})


def _escape_regex(label: str) -> str:
    return label.translate(_PROM_REGEX_ESCAPE)


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


def upstream_problems(
    probe: UpstreamProbe | None,
    namespace: str,
    services: list[str] | None,
    threshold: float,
) -> list[str]:
    """Services whose observed error rate exceeds `threshold` - empty means steady.

    This is what catches the Chaos Mesh residual-state class of bug: pods are Running and alerts
    are quiet, but the service's own calls to its upstream are still erroring because the DNS or
    network injection left state behind that pod availability cannot see. Without an upstream
    probe, precheck would pass and the next experiment's label would be corrupted.
    """
    if probe is None or not services:
        return []
    try:
        rates = probe.error_rate(namespace, list(services))
    except Exception:
        return []  # a probe outage is not a lab abort - log and move on
    return [
        f"{service}: {rate:.0%} error rate > {threshold:.0%}"
        for service, rate in sorted(rates.items())
        if rate > threshold
    ]
