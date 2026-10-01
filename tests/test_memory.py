from datetime import date
from pathlib import Path

import pytest
import yaml

from chaosloop.agent.diagnostician import SEARCH_MEMORY, Diagnostician
from chaosloop.memory.promote import (
    PromotionError,
    extract_signals,
    incident_from_case,
    write_incident,
)
from chaosloop.memory.retriever import Memory, tokenize
from chaosloop.memory.store import (
    MemoryLeakError,
    assert_disjoint_from,
    load_memory,
    memory_snapshot,
    parse_runbook,
)
from chaosloop.schemas.golden_case import load_golden_case, load_golden_set
from chaosloop.schemas.taxonomy import Category
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.replay_backend import ReplayBackend
from tests.conftest import REPO_ROOT, FakeLLMClient, diagnosis_json

REPO_MEMORY = REPO_ROOT / "memory"


def incident_yaml(incident_id: str, category: str, signals: list[str], source_case_id: str) -> dict:
    return {
        "incident_id": incident_id,
        "category": category,
        "symptom": "ImagePullBackOff",
        "summary": f"summary for {incident_id}",
        "signals": signals,
        "root_cause": f"root cause for {incident_id}",
        "resolution": "fixed",
        "provenance": {
            "source": "golden",
            "source_case_id": source_case_id,
            "confirmed_by": "tester",
            "confirmed_at": "2026-10-01",
        },
    }


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    (root / "incidents").mkdir(parents=True)
    (root / "runbooks").mkdir()
    for incident in [
        incident_yaml("inc-auth", "registry_auth_missing",
                      ["pull access denied, authentication required"], "case-auth"),
        incident_yaml("inc-tag", "image_tag_not_found",
                      ["failed to resolve reference: not found: manifest unknown"], "case-tag"),
        incident_yaml("inc-ratelimit", "registry_rate_limited",
                      ["toomanyrequests: You have reached your pull rate limit"], "case-rate"),
    ]:
        (root / "incidents" / f"{incident['incident_id']}.yaml").write_text(
            yaml.safe_dump(incident), encoding="utf-8"
        )
    (root / "runbooks" / "imagepull.md").write_text(
        "---\nrunbook_id: imagepull\ntitle: ImagePullBackOff\n"
        "categories: [registry_auth_missing]\n---\n\n"
        "## Auth\nA denied pull means credentials are missing.\n\n"
        "## Rate limits\ntoomanyrequests means anonymous pulls are throttled.\n",
        encoding="utf-8",
    )
    return root


def test_tokenize_keeps_negations_and_drops_instance_noise():
    tokens = tokenize(
        "pod checkout-6c9b4d7f88-v4n2p: no such host 10.96.0.10:53 not found sha256:9f86d081884c"
    )
    assert "no" in tokens and "not" in tokens and "host" in tokens
    assert not any(t.startswith("v4n2p") or t.startswith("9f86") for t in tokens)


def test_retrieval_ranks_the_matching_cause_first(memory_dir: Path):
    memory = Memory(load_memory(memory_dir))
    incidents, section = memory.search("Failed to pull image: toomanyrequests rate limit")
    assert incidents[0][0].incident_id == "inc-ratelimit"
    assert section is not None and section.heading == "Rate limits"


def test_irrelevant_query_returns_nothing(memory_dir: Path):
    text = Memory(load_memory(memory_dir)).search_text("zzz qqq xyzzy")
    assert text.startswith("No confirmed incidents")


def test_weak_matches_are_dropped_not_padded(memory_dir: Path):
    incidents, _ = Memory(load_memory(memory_dir)).search("manifest unknown")
    assert [i.incident_id for i, _ in incidents] == ["inc-tag"]


def test_leave_one_out_hides_the_case_being_graded(memory_dir: Path):
    memory = Memory(load_memory(memory_dir)).excluding({"case-auth"})
    incidents, _ = memory.search("pull access denied authentication required")
    assert "inc-auth" not in [i.incident_id for i, _ in incidents]
    assert memory.snapshot == load_memory(memory_dir).snapshot


def test_memory_rejects_incidents_from_the_test_split(memory_dir: Path):
    store = load_memory(memory_dir)
    with pytest.raises(MemoryLeakError, match="inc-tag"):
        assert_disjoint_from(store, {"case-tag", "some-other-test-case"})
    assert_disjoint_from(store, {"unrelated"})


def test_snapshot_changes_with_contents(memory_dir: Path):
    before = memory_snapshot(memory_dir)
    (memory_dir / "runbooks" / "extra.md").write_text("---\ntitle: x\n---\nbody", encoding="utf-8")
    assert memory_snapshot(memory_dir) != before


def test_duplicate_incident_ids_rejected(memory_dir: Path):
    original = memory_dir / "incidents" / "inc-auth.yaml"
    (memory_dir / "incidents" / "copy.yaml").write_text(original.read_text(), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_memory(memory_dir)


def test_runbook_requires_front_matter(tmp_path: Path):
    path = tmp_path / "bad.md"
    path.write_text("## heading\nbody", encoding="utf-8")
    with pytest.raises(ValueError, match="front matter"):
        parse_runbook(path)


def test_promotion_refuses_test_split(golden_root: Path):
    test_case = load_golden_set(golden_root, "test")[0]
    with pytest.raises(PromotionError, match="test split"):
        incident_from_case(test_case, confirmed_by="tester")


def test_promotion_extracts_distinguishing_signals(golden_root: Path, tmp_path: Path):
    case = load_golden_case(golden_root / "dev" / "imagepull-rate-limited-01.yaml")
    incident = incident_from_case(case, confirmed_by="tester", confirmed_at=date(2026, 10, 1))

    assert incident.category is Category.REGISTRY_RATE_LIMITED
    assert incident.symptom == "ImagePullBackOff"
    assert any("toomanyrequests" in s for s in incident.signals)
    assert not any(s == "Error: ErrImagePull" for s in incident.signals)
    assert incident.provenance.source_case_id == case.case_id

    path = write_incident(incident, tmp_path / "memory")
    assert load_memory(tmp_path / "memory").incidents == [incident]
    assert path.name == "inc-imagepull-rate-limited-01.yaml"


def test_signals_come_from_previous_logs_for_crashloops(golden_root: Path):
    case = load_golden_case(golden_root / "dev" / "crashloop-missing-env-var-01.yaml")
    signals = extract_signals(case.evidence)
    assert any("DATABASE_URL" in s for s in signals)


def test_signals_read_multi_object_evidence(golden_root: Path):
    case = load_golden_case(golden_root / "dev" / "cascading-upstream-targetport-01.yaml")
    signals = extract_signals(case.evidence)
    assert any("connection refused" in s for s in signals)
    assert case.expected.root_cause not in signals


def test_signals_drop_pod_hashes():
    evidence = {
        "get_events": [
            {"type": "Warning", "message": "Failed for prod/cart-service-6c9b4d7f88-v4n2p: denied"}
        ]
    }
    assert extract_signals(evidence) == ["Failed for prod/cart-service: denied"]


def test_repo_memory_is_valid_and_disjoint(golden_root: Path):
    store = load_memory(REPO_MEMORY)
    assert store.sections, "expected runbooks in memory/runbooks"
    test_ids = {c.case_id for c in load_golden_set(golden_root, "test")}
    assert_disjoint_from(store, test_ids)


def test_agent_gets_search_memory_only_when_memory_is_on(memory_dir: Path):
    trigger = Trigger(alertname="PodImagePullBackOff", service="s", namespace="prod", pod="p")

    without = FakeLLMClient([diagnosis_json("unknown", patch="")])
    Diagnostician(ReplayBackend({}), "m", without).diagnose(trigger)
    assert SEARCH_MEMORY not in without.seen_tools[0]
    assert "search_memory" not in without.seen_messages[0][0]["content"]

    memory = Memory(load_memory(memory_dir))
    with_memory = FakeLLMClient(
        [
            [(SEARCH_MEMORY, {"query": "toomanyrequests pull rate limit"})],
            diagnosis_json("registry_rate_limited"),
        ]
    )
    _, trace = Diagnostician(ReplayBackend({}), "m", with_memory, memory=memory).diagnose(trigger)

    assert SEARCH_MEMORY in with_memory.seen_tools[0]
    assert trace.tool_calls[0].tool == SEARCH_MEMORY
    assert "inc-ratelimit" in with_memory.sent_text
    assert 'trust="untrusted"' in with_memory.sent_text
