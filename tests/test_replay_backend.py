import inspect

import pytest

from chaosloop.tools.interface import (
    NOT_AVAILABLE,
    TOOL_NAMES,
    TOOL_SPECS,
    ToolBackend,
    UnknownToolError,
    dispatch,
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


def test_dispatch_routes_and_rejects_unknown_tools():
    backend = ReplayBackend({"get_service": {"spec": {"selector": {"app": "x"}}}})
    assert dispatch(backend, "get_service", {"namespace": "prod", "name": "x"}) == {
        "spec": {"selector": {"app": "x"}}
    }
    with pytest.raises(UnknownToolError):
        dispatch(backend, "kubectl_exec", {})
