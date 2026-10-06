from pathlib import Path

import pytest
import yaml

from chaosloop.live.candidates import CandidateQueue, similarity, surprise
from chaosloop.live.catalog import load_catalog
from chaosloop.live.planner import MIN_SAMPLES, category_stats, plan_batch, plan_next
from chaosloop.schemas.eval_record import EvalRecord
from chaosloop.schemas.golden_case import GoldenCase, GoldenCaseError, load_golden_set
from chaosloop.schemas.taxonomy import Category
from tests.conftest import REPO_ROOT, write_case

CATALOG = load_catalog(REPO_ROOT / "lab" / "experiments.yaml")


def run(category: str, correct: bool, tier: str = "A", detected: bool | None = None,
        experiment_id: str | None = None) -> EvalRecord:
    return EvalRecord(
        run_id="r", case_id=experiment_id or category, experiment_id=experiment_id, tier=tier,
        repeat=1, model="m", prompt_version="p", golden_version="g",
        expected_category=category, agent_category=category if correct else "unknown",
        category_correct=correct, fix_correct=correct, is_false_positive=False,
        malformed=False, confidence=None, detected=detected,
    )


def saturated(accuracy_for: dict[str, float] | None = None) -> list[EvalRecord]:
    records = []
    for category in CATALOG.categories():
        wrong = round(MIN_SAMPLES * (1 - (accuracy_for or {}).get(category.value, 1.0)))
        records += [run(category.value, i >= wrong) for i in range(MIN_SAMPLES)]
    return records


# --- planner ------------------------------------------------------------------------------


def test_unsampled_categories_come_first():
    plan = plan_next(CATALOG, saturated()[MIN_SAMPLES:])  # drop the first category's samples
    first = CATALOG.categories()[0]
    assert CATALOG.get(plan.experiment_id).category is first
    assert "sample" in plan.reason


def test_once_sampled_the_weakest_category_is_chosen():
    plan = plan_next(CATALOG, saturated({"high_latency": 0.34}))
    assert CATALOG.get(plan.experiment_id).category is Category.HIGH_LATENCY
    assert "weakest" in plan.reason


def test_detection_misses_break_accuracy_ties():
    records = saturated()
    records.append(run("dns_resolution_failure", False, tier="B", detected=False,
                       experiment_id="frontend-dns-resolution-failure"))
    plan = plan_next(CATALOG, records)
    assert plan.experiment_id == "frontend-dns-resolution-failure"


def test_a_batch_spreads_across_the_catalog_instead_of_repeating():
    plans = plan_batch(CATALOG, [], count=len(CATALOG.plannable()))
    assert len({p.experiment_id for p in plans}) == len(CATALOG.plannable())


def test_parameters_rotate_through_allowed_values_only():
    allowed = CATALOG.get("cart-high-latency").parameters["latency_ms"].allowed
    records = saturated({"high_latency": 0.0})
    seen = []
    for _ in range(len(allowed)):
        plan = plan_next(CATALOG, records)
        assert plan.experiment_id == "cart-high-latency"
        seen.append(plan.parameters["latency_ms"])
        records.append(run("high_latency", False, tier="B", detected=True,
                           experiment_id="cart-high-latency"))
    assert sorted(seen) == sorted(allowed)


def test_stats_count_both_tiers_and_skip_aborts():
    records = [
        run("missing_env_var", True),
        run("missing_env_var", False, tier="B", detected=True),
        run("missing_env_var", False, tier="B", detected=False),
        EvalRecord(**{**run("missing_env_var", False, tier="B").model_dump(),
                      "aborted": "precheck"}),
    ]
    stats = {s.category: s for s in category_stats(records, [Category.MISSING_ENV_VAR])}
    s = stats[Category.MISSING_ENV_VAR]
    assert s.samples == 3
    assert s.accuracy == pytest.approx(0.5)  # the undetected run is not graded
    assert s.detection_miss == pytest.approx(0.5)


# --- candidate queue ----------------------------------------------------------------------


def case(tmp_path: Path, case_id: str, category: str, message: str) -> GoldenCase:
    path = write_case(tmp_path / "src", case_id, category, split="dev", evidence={
        "get_events": [{"type": "Warning", "message": message}],
    })
    return GoldenCase.model_validate(yaml.safe_load(path.read_text()))


def test_similarity_reads_distinguishing_signals(tmp_path: Path):
    a = case(tmp_path, "a", "image_tag_not_found", "pull failed: manifest unknown not found")
    b = case(tmp_path, "b", "image_tag_not_found", "pull failed: manifest unknown not found")
    c = case(tmp_path, "c", "image_tag_not_found", "toomanyrequests rate limit reached")
    assert similarity(a.evidence, b.evidence) == 1.0
    assert similarity(a.evidence, c.evidence) < 0.5


def test_surprise_reasons(tmp_path: Path):
    known = case(tmp_path, "k", "image_tag_not_found", "manifest unknown not found")
    same = case(tmp_path, "s", "image_tag_not_found", "manifest unknown not found")
    novel = case(tmp_path, "n", "image_tag_not_found", "registry returned an html login page")
    assert surprise(same, agent_correct=False, golden=[known]) == "the agent got it wrong"
    assert surprise(same, agent_correct=True, golden=[known]) is None
    assert "unlike" in (surprise(novel, agent_correct=True, golden=[known]) or "")
    assert surprise(same, agent_correct=True, golden=[]) == "first case of this category"


def test_review_approves_into_the_chosen_split(tmp_path: Path):
    golden = tmp_path / "golden"
    queue = CandidateQueue(golden)
    queue.propose(case(tmp_path, "live-01", "image_tag_not_found", "manifest unknown"), "novel")
    assert [c.case_id for c in queue.pending()] == ["live-01"]

    path = queue.approve("live-01", "test", reviewer="divya")
    assert path == golden / "test" / "live-01.yaml"
    approved = load_golden_set(golden, "test")[0]
    assert approved.split == "test"
    assert approved.metadata.model_extra["reviewed_by"] == "divya"
    assert approved.metadata.model_extra["candidate_reason"] == "novel"
    assert queue.pending() == []


def test_review_rejects_with_a_reason_and_keeps_the_record(tmp_path: Path):
    queue = CandidateQueue(tmp_path / "golden")
    queue.propose(case(tmp_path, "live-02", "image_tag_not_found", "x"), "wrong answer")
    path = queue.reject("live-02", reviewer="divya", reason="label was wrong")
    assert path.parent.name == "rejected"
    assert yaml.safe_load(path.read_text())["metadata"]["rejection_reason"] == "label was wrong"
    assert queue.pending() == []


def test_unsafe_candidate_ids_cannot_escape_the_queue(tmp_path: Path):
    queue = CandidateQueue(tmp_path / "golden")
    with pytest.raises(GoldenCaseError, match="unsafe"):
        queue.get("../test/imagepull-auth-missing-02")


def test_candidates_never_count_as_golden_cases(tmp_path: Path):
    golden = tmp_path / "golden"
    CandidateQueue(golden).propose(
        case(tmp_path, "live-03", "image_tag_not_found", "x"), "novel"
    )
    assert load_golden_set(golden) == []


def test_recording_only_experiments_are_never_planned():
    hidden = {e.id for e in CATALOG.experiments if not e.plannable}
    assert hidden  # registry_unreachable and progress_deadline_exceeded live here
    plans = plan_batch(CATALOG, [], count=3 * len(CATALOG.experiments))
    assert hidden.isdisjoint(p.experiment_id for p in plans)
    assert Category.REGISTRY_UNREACHABLE not in CATALOG.categories()
