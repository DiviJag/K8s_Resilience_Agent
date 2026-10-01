"""Phase 2 harness behaviour: self-consistency, calibration, judge, memory, traces."""

import json
from pathlib import Path

import pytest
import yaml

from chaosloop.agent.diagnostician import SAMPLE_NUDGE, SEARCH_MEMORY, Diagnostician
from chaosloop.eval.calibration import IsotonicCalibrator
from chaosloop.eval.harness import (
    TierAHarness,
    apply_calibrator,
    read_records,
    summarize,
    trace_path,
    write_records,
)
from chaosloop.eval.judge import RootCauseJudge
from chaosloop.memory.retriever import Memory
from chaosloop.memory.store import MemoryLeakError, load_memory
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.replay_backend import ReplayBackend
from tests.conftest import FakeLLMClient, diagnosis_json, write_case

TRIGGER = Trigger(alertname="PodImagePullBackOff", service="svc", namespace="prod", pod="p")
AUTH = diagnosis_json("registry_auth_missing")
TAG = diagnosis_json("image_tag_not_found", patch="image: a:v2")


def test_samples_measure_agreement_but_do_not_change_the_answer():
    client = FakeLLMClient([AUTH, AUTH, TAG, AUTH, "not json", AUTH])
    diagnosis, trace = Diagnostician(ReplayBackend({}), "m", client, samples=5).diagnose(TRIGGER)

    assert diagnosis is not None and diagnosis.category.value == "registry_auth_missing"
    assert trace.samples == [
        "registry_auth_missing", "image_tag_not_found", "registry_auth_missing", None,
        "registry_auth_missing",
    ]
    assert trace.consistency == pytest.approx(3 / 5)


def test_samples_are_drawn_with_tools_off_at_temperature():
    client = FakeLLMClient([AUTH] * 4)
    Diagnostician(ReplayBackend({}), "m", client, samples=3, sample_temperature=0.7).diagnose(
        TRIGGER
    )
    investigation, *sampling = client.seen_options
    assert investigation == {"temperature": None, "tool_choice": None}
    assert all(o == {"temperature": 0.7, "tool_choice": "none"} for o in sampling)
    assert client.seen_messages[1][-1] == {"role": "user", "content": SAMPLE_NUDGE}


def test_sampling_stops_at_the_cost_budget():
    from chaosloop.agent.diagnostician import Budget

    client = FakeLLMClient([AUTH] * 10, cost=0.2)
    _, trace = Diagnostician(
        ReplayBackend({}), "m", client, samples=5, budget=Budget(max_cost_usd=0.5)
    ).diagnose(TRIGGER)
    assert len(trace.samples) == 2  # 0.2 spent investigating, two samples reach 0.6


def test_malformed_answer_is_not_sampled():
    client = FakeLLMClient(["no json here", AUTH, AUTH])
    diagnosis, trace = Diagnostician(ReplayBackend({}), "m", client, samples=2).diagnose(TRIGGER)
    assert diagnosis is None
    assert trace.samples == [] and trace.consistency is None
    assert len(client.seen_messages) == 1


def test_provider_error_during_sampling_is_not_a_vote():
    class FlakySampler(FakeLLMClient):
        def chat(self, messages, tools, model, **options):
            if options.get("tool_choice") == "none" and len(self.seen_messages) == 2:
                self.seen_messages.append(messages)
                raise RuntimeError("outage")
            return super().chat(messages, tools, model, **options)

    client = FlakySampler([AUTH, AUTH, AUTH])
    _, trace = Diagnostician(ReplayBackend({}), "m", client, samples=3).diagnose(TRIGGER)
    assert trace.sample_errors == 1
    assert trace.consistency == 1.0


def build_golden(tmp_path: Path) -> Path:
    golden = tmp_path / "golden"
    write_case(golden / "test", "case-auth", "registry_auth_missing")
    write_case(golden / "dev", "dev-auth", "registry_auth_missing", split="dev")
    return golden


def test_harness_records_confidence_components_and_calibrates(tmp_path: Path):
    golden = build_golden(tmp_path)
    calibrator = IsotonicCalibrator.fit([0.6, 1.0], [False, True], fitted_on="dev-run")
    client = FakeLLMClient([AUTH, AUTH, TAG, AUTH, TAG, AUTH])
    records = TierAHarness(
        golden_dir=golden, model="m", client=client, split="test", repeats=1, samples=5,
        calibrator=calibrator,
    ).run()

    record = records[0]
    assert record.split == "test"
    assert record.self_reported_confidence == pytest.approx(0.9)
    assert record.consistency_confidence == pytest.approx(0.6)
    assert record.calibrated_confidence == pytest.approx(0.0)
    assert record.confidence == record.calibrated_confidence
    assert len(record.samples) == 5


def test_confidence_falls_back_to_self_report_without_sampling(tmp_path: Path):
    records = TierAHarness(
        golden_dir=build_golden(tmp_path), model="m", client=FakeLLMClient([AUTH]),
        split="test", repeats=1,
    ).run()
    assert records[0].consistency_confidence is None
    assert records[0].confidence == records[0].self_reported_confidence == pytest.approx(0.9)


def test_judge_score_and_rationale_are_recorded(tmp_path: Path):
    agent = FakeLLMClient([AUTH])
    judge = RootCauseJudge(
        FakeLLMClient(['{"score": 0.5, "rationale": "Right idea, vague."}']), "openai/j", "m"
    )
    record = TierAHarness(
        golden_dir=build_golden(tmp_path), model="m", client=agent, split="test", repeats=1,
        judge=judge,
    ).run()[0]
    assert record.root_cause_score == 0.5
    assert record.judge_rationale == "Right idea, vague."
    assert record.judge_model == "openai/j"
    assert summarize([record]).root_cause_score == 0.5


def memory_with_incident_from(tmp_path: Path, source_case_id: str) -> Memory:
    root = tmp_path / "memory"
    (root / "incidents").mkdir(parents=True)
    (root / "runbooks").mkdir()
    incident = {
        "incident_id": f"inc-{source_case_id}",
        "category": "registry_auth_missing",
        "symptom": "ImagePullBackOff",
        "summary": "s",
        "signals": ["pull access denied authentication required"],
        "root_cause": "no pull secret",
        "resolution": "added imagePullSecrets",
        "provenance": {
            "source": "golden",
            "source_case_id": source_case_id,
            "confirmed_by": "t",
            "confirmed_at": "2026-10-01",
        },
    }
    (root / "incidents" / "i.yaml").write_text(yaml.safe_dump(incident), encoding="utf-8")
    return Memory(load_memory(root))


def test_harness_refuses_memory_that_holds_a_test_case(tmp_path: Path):
    golden = build_golden(tmp_path)
    with pytest.raises(MemoryLeakError):
        TierAHarness(
            golden_dir=golden, model="m", client=FakeLLMClient([]), split="dev",
            memory=memory_with_incident_from(tmp_path, "case-auth"),
        )


def test_dev_case_cannot_retrieve_its_own_incident(tmp_path: Path):
    golden = build_golden(tmp_path)
    client = FakeLLMClient(
        [[(SEARCH_MEMORY, {"query": "pull access denied authentication required"})], AUTH]
    )
    record = TierAHarness(
        golden_dir=golden, model="m", client=client, split="dev", repeats=1,
        memory=memory_with_incident_from(tmp_path, "dev-auth"),
    ).run()[0]

    assert "inc-dev-auth" not in client.sent_text
    assert record.memory_snapshot is not None
    assert summarize([record]).memory_usage_rate == 1.0


def test_trace_files_capture_every_message(tmp_path: Path):
    trace_dir = tmp_path / "traces"
    client = FakeLLMClient([[("get_events", {"namespace": "prod"})], AUTH, AUTH])
    TierAHarness(
        golden_dir=build_golden(tmp_path), model="m", client=client, split="test", repeats=1,
        samples=1, trace_dir=trace_dir,
    ).run()

    payload = json.loads(trace_path(trace_dir, "case-auth", 1).read_text())
    roles = [m["role"] for m in payload["messages"]]
    assert roles[:2] == ["system", "user"] and "tool" in roles and roles[-1] == "assistant"
    assert payload["samples"] == ["registry_auth_missing"]
    assert payload["expected"]["category"] == "registry_auth_missing"


def test_stored_runs_can_be_recalibrated_without_rerunning(tmp_path: Path):
    client = FakeLLMClient([AUTH, AUTH, TAG])
    records = TierAHarness(
        golden_dir=build_golden(tmp_path), model="m", client=client, split="test", repeats=1,
        samples=2,
    ).run()
    path = write_records(records, tmp_path / "run.jsonl")
    reloaded = read_records(path)
    assert reloaded == records

    calibrated = apply_calibrator(reloaded, IsotonicCalibrator.fit([0.5], [True], "dev"))
    assert calibrated[0].confidence == calibrated[0].calibrated_confidence == 1.0

    summary = summarize(calibrated).calibration
    assert set(summary) == {"system", "self_reported", "consistency", "calibrated"}
    assert summary["consistency"].n == 1
