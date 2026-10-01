from pathlib import Path
from typing import Any

from chaosloop.recorder.recorder import Recorder, write_golden_case
from chaosloop.schemas.golden_case import (
    CaseMetadata,
    Expected,
    FixCheck,
    load_golden_case,
    load_golden_set,
)
from chaosloop.schemas.taxonomy import Category
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.interface import NOT_AVAILABLE, TOOL_NAMES, ToolBackend
from chaosloop.tools.replay_backend import ReplayBackend

TRIGGER = Trigger(
    alertname="PodImagePullBackOff",
    service="cart-service",
    namespace="chaosloop-lab",
    pod="cart-service-abc",
)


class StubBackend(ToolBackend):
    """Records which tools were called so we can prove the Recorder calls all of them."""

    def __init__(self) -> None:
        self.called: list[str] = []

    def _record(self, tool: str, value: Any) -> Any:
        self.called.append(tool)
        return value

    def get_pod_status(self, namespace, pod):
        return self._record("get_pod_status", {"phase": "Pending"})

    def get_events(self, namespace, object_name=None):
        return self._record("get_events", [{"message": "pull access denied"}])

    def get_logs(self, namespace, pod, container=None):
        return self._record(
            "get_logs", {"current": "API_TOKEN=sk-abcdefghijklmnopqrstu", "previous": None}
        )

    def get_deployment_spec(self, namespace, name):
        return self._record("get_deployment_spec", {"spec": {"replicas": 2}})

    def get_service(self, namespace, name):
        return self._record("get_service", {"spec": {"selector": {"app": "cart-service"}}})

    def get_endpoints(self, namespace, name):
        return self._record("get_endpoints", {"subsets": []})

    def get_pvc_status(self, namespace, name):
        return self._record("get_pvc_status", {"status": {"phase": "Bound"}})

    def get_node_status(self, node=None):
        return self._record("get_node_status", [{"name": "chaosloop-worker"}])


def test_recorder_calls_every_tool_not_just_the_likely_ones():
    backend = StubBackend()
    evidence = Recorder(backend).capture(TRIGGER)

    assert set(evidence) == set(TOOL_NAMES)
    # No PVC target was given, so that one tool is recorded as unavailable rather than called.
    assert evidence["get_pvc_status"] == NOT_AVAILABLE
    assert set(backend.called) == set(TOOL_NAMES) - {"get_pvc_status"}


def test_recorder_captures_pvc_when_targeted():
    evidence = Recorder(StubBackend()).capture(TRIGGER, targets={"pvc": "cart-data"})
    assert evidence["get_pvc_status"] == {"status": {"phase": "Bound"}}


def test_recorder_redacts_before_writing():
    evidence = Recorder(StubBackend()).capture(TRIGGER)
    assert "sk-abcdefghijklmnopqrstu" not in str(evidence)


def test_recorder_survives_a_failing_tool():
    class Exploding(StubBackend):
        def get_events(self, namespace, object_name=None):
            raise RuntimeError("api server timeout")

    evidence = Recorder(Exploding()).capture(TRIGGER)
    assert "api server timeout" in evidence["get_events"]
    assert evidence["get_pod_status"] == {"phase": "Pending"}


def test_written_case_round_trips_and_replays(tmp_path: Path):
    evidence = Recorder(StubBackend()).capture(TRIGGER)
    path = write_golden_case(
        case_id="registry-auth-missing-07",
        split="dev",
        trigger=TRIGGER,
        evidence=evidence,
        expected=Expected(
            category=Category.REGISTRY_AUTH_MISSING,
            root_cause="no imagePullSecrets",
            fix_check=FixCheck(path="spec.template.spec.imagePullSecrets"),
        ),
        metadata=CaseMetadata(
            source="chaos", k8s_version="1.30", runtime="containerd", recorded="2026-09-30"
        ),
        out_dir=tmp_path,
    )

    assert path == tmp_path / "dev" / "registry-auth-missing-07.yaml"
    case = load_golden_case(path)
    assert case.expected.category is Category.REGISTRY_AUTH_MISSING
    assert load_golden_set(tmp_path, "dev") == [case]

    # The recorded evidence must be directly replayable - that is the whole point.
    replay = ReplayBackend(case)
    assert replay.get_pod_status("chaosloop-lab", "cart-service-abc") == {"phase": "Pending"}
    assert replay.get_pvc_status("chaosloop-lab", "x") == NOT_AVAILABLE
