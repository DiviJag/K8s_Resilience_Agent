"""Steady-state monitoring: Prometheus upstream probe and problem-aggregation helpers."""

import httpx

from chaosloop.live.monitoring import (
    DEFAULT_PROM_QUERY,
    PrometheusProbe,
    _escape_regex,
    upstream_problems,
)


class RecordingTransport(httpx.BaseTransport):
    """Records the last request and replies with a canned Prometheus payload."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload, self.status = payload, status
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)


def _result(service: str, value: str) -> dict:
    return {"metric": {"service": service}, "value": [0, value]}


def test_prometheus_probe_substitutes_namespace_services_window_into_the_template():
    transport = RecordingTransport({"status": "success", "data": {"result": []}})
    probe = PrometheusProbe(
        base_url="http://prom:9090",
        query_template='rate(x{{namespace="{namespace}",service=~"{services}"}}[{window}])',
        window="2m",
        transport=transport,
    )
    probe.error_rate("chaosloop-lab", ["cart-service", "checkout-frontend"])
    sent_query = transport.requests[-1].url.params.get("query")
    assert sent_query == (
        'rate(x{namespace="chaosloop-lab",'
        'service=~"cart-service|checkout-frontend"}[2m])'
    )


def test_prometheus_probe_parses_series_into_a_service_to_rate_dict():
    transport = RecordingTransport({
        "status": "success",
        "data": {"result": [_result("cart-service", "0.12"), _result("checkout-frontend", "0.0")]},
    })
    probe = PrometheusProbe(base_url="http://prom:9090", transport=transport)
    rates = probe.error_rate("ns", ["cart-service", "checkout-frontend"])
    assert rates == {"cart-service": 0.12, "checkout-frontend": 0.0}


def test_prometheus_probe_tolerates_malformed_rows():
    transport = RecordingTransport({
        "status": "success",
        "data": {"result": [
            _result("cart-service", "0.1"),
            {"metric": {}, "value": [0, "0.5"]},        # missing service label - dropped
            _result("checkout-frontend", "not-a-float"),  # unparseable value   - dropped
        ]},
    })
    probe = PrometheusProbe(base_url="http://prom:9090", transport=transport)
    assert probe.error_rate("ns", ["cart-service", "checkout-frontend"]) == {"cart-service": 0.1}


def test_prometheus_probe_returns_empty_on_prometheus_error_status():
    transport = RecordingTransport({"status": "error", "error": "parse"})
    probe = PrometheusProbe(base_url="http://prom:9090", transport=transport)
    assert probe.error_rate("ns", ["cart-service"]) == {}


def test_prometheus_probe_short_circuits_on_empty_service_list():
    """No services means no steady-state check to make - we skip the HTTP call entirely."""
    transport = RecordingTransport({"status": "success", "data": {"result": []}})
    probe = PrometheusProbe(base_url="http://prom:9090", transport=transport)
    assert probe.error_rate("ns", []) == {}
    assert transport.requests == []


def test_default_prom_query_divides_by_clamped_zero_to_avoid_division_errors():
    """A series with zero total requests must not blow up the query - clamp_min guards it."""
    assert "clamp_min(" in DEFAULT_PROM_QUERY


def test_escape_regex_quotes_prom_special_chars():
    assert _escape_regex("svc.a+b") == r"svc\.a\+b"


def test_upstream_problems_only_flags_services_above_the_threshold():
    class Fixed:
        def error_rate(self, namespace, services):
            return {"cart-service": 0.08, "checkout-frontend": 0.02}

    assert upstream_problems(Fixed(), "ns", ["cart-service", "checkout-frontend"], 0.05) == [
        "cart-service: 8% error rate > 5%",
    ]


def test_upstream_problems_with_no_probe_wires_in_no_overhead():
    assert upstream_problems(None, "ns", ["cart-service"], 0.05) == []


def test_upstream_problems_swallows_probe_exceptions():
    class Broken:
        def error_rate(self, namespace, services):
            raise RuntimeError("outage")

    assert upstream_problems(Broken(), "ns", ["cart-service"], 0.05) == []
