import inspect

import pytest

from chaosloop.tools.interface import (
    NOT_AVAILABLE,
    TOOL_NAMES,
    TOOL_SPECS,
    ToolBackend,
    UnknownToolError,
    dispatch,
    owned_by,
)
from chaosloop.tools.replay_backend import ReplayBackend


def test_abc_methods_match_tool_names():
    """The ABC, the LLM tool schema and the evidence keys must not drift apart."""
    abstract = {
        name
        for name, member in inspect.getmembers(ToolBackend, inspect.isfunction)
        if getattr(member, "__isabstractmethod__", False)
    }
    assert abstract == set(TOOL_NAMES)
    assert {spec["function"]["name"] for spec in TOOL_SPECS} == set(TOOL_NAMES)


def test_unrecorded_call_returns_not_available():
    backend = ReplayBackend({"get_pod_status": {"phase": "Pending"}})
    assert backend.get_events("prod") == NOT_AVAILABLE
    assert backend.get_pvc_status("prod", "data") == NOT_AVAILABLE
    assert backend.get_pod_status("prod", "p") == {"phase": "Pending"}


def test_call_log_records_every_call_in_order():
    backend = ReplayBackend({})
    backend.get_pod_status("prod", "p")
    backend.get_logs("prod", "p")
    backend.get_events("prod", "p")
    backend.get_logs("prod", "p")
    assert backend.call_log == ["get_pod_status", "get_logs", "get_events", "get_logs"]


def test_get_logs_normalizes_bare_string():
    backend = ReplayBackend({"get_logs": "container is waiting to start"})
    assert backend.get_logs("prod", "p") == {
        "current": "container is waiting to start",
        "previous": None,
    }


def test_get_logs_passes_through_recorded_pair():
    backend = ReplayBackend({"get_logs": {"current": "", "previous": "KeyError: 'DATABASE_URL'"}})
    assert backend.get_logs("prod", "p")["previous"] == "KeyError: 'DATABASE_URL'"


def test_replay_backend_redacts_unredacted_evidence():
    backend = ReplayBackend({"get_logs": {"current": "token=hunter2secret", "previous": None}})
    assert "hunter2secret" not in backend.get_logs("prod", "p")["current"]


MULTI_OBJECT = {
    "get_service": {
        "by_name": {
            "payment": {"spec": {"selector": {"app": "payment"}}},
            "orders-db": {"spec": {"selector": {"app": "orders-db-v2"}}},
        }
    },
    "get_events": {
        "by_name": {
            "payment": [{"message": "Back-off restarting failed container"}],
            "orders-db": [{"message": "Scaled up replica set orders-db-7f9"}],
        }
    },
    "get_logs": {"by_name": {"payment-7d9f8c6b54-2xkzq": {"current": "", "previous": "refused"}}},
}


def test_by_name_evidence_answers_per_object():
    backend = ReplayBackend(MULTI_OBJECT)
    assert backend.get_service("prod", "orders-db")["spec"]["selector"] == {"app": "orders-db-v2"}
    assert backend.get_service("prod", "payment")["spec"]["selector"] == {"app": "payment"}
    assert backend.get_service("prod", "inventory") == NOT_AVAILABLE


def test_by_name_events_follow_ownership():
    backend = ReplayBackend(MULTI_OBJECT)
    pod_events = backend.get_events("prod", "payment-7d9f8c6b54-2xkzq")
    assert pod_events == [{"message": "Back-off restarting failed container"}]
    assert len(backend.get_events("prod")) == 2
    assert backend.get_events("prod", "unrelated") == NOT_AVAILABLE


def test_by_name_logs_match_a_deployment_to_its_pod():
    backend = ReplayBackend(MULTI_OBJECT)
    assert backend.get_logs("prod", "payment-7d9f8c6b54-2xkzq")["previous"] == "refused"
    assert backend.get_logs("prod", "payment")["previous"] == "refused"


@pytest.mark.parametrize(
    "name, owner, expected",
    [
        ("cart-service", "cart-service", True),
        ("cart-service-6d4f8797f6", "cart-service", True),
        ("cart-service-6d4f8797f6-2lcfw", "cart-service", True),
        ("orders-db-0", "orders-db", True),
        ("cart-service-6d4f8797f6-2lcfw", "cart", False),
        ("cartographer-6d4f8797f6", "cart", False),
    ],
)
def test_ownership_requires_a_controller_suffix(name: str, owner: str, expected: bool):
    assert owned_by(name, owner) is expected


def test_dispatch_routes_and_rejects_unknown_tools():
    backend = ReplayBackend({"get_service": {"spec": {"selector": {"app": "x"}}}})
    assert dispatch(backend, "get_service", {"namespace": "prod", "name": "x"}) == {
        "spec": {"selector": {"app": "x"}}
    }
    with pytest.raises(UnknownToolError):
        dispatch(backend, "kubectl_exec", {})
