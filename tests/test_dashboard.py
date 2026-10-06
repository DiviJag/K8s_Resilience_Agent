from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chaosloop.dashboard.app import create_app, trace_steps
from chaosloop.dashboard.charts import reliability_series, reliability_svg
from chaosloop.eval.harness import TierAHarness, write_records
from tests.conftest import FakeLLMClient, diagnosis_json, write_case

XSS = "<script>alert('pwned')</script>"
RUN_ID = "20261001T120000Z-abc123"


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    golden = tmp_path / "golden"
    evidence = {"get_logs": {"current": XSS, "previous": None}}
    write_case(golden / "test", "case-auth", "registry_auth_missing", evidence=evidence)
    write_case(golden / "test", "case-tag", "image_tag_not_found",
               fix_check={"path": "spec.template.spec.containers.image", "must_exist": True})

    runs = tmp_path / "runs"
    client = FakeLLMClient(
        [
            [("get_logs", {"namespace": "prod", "pod": "p"})],
            diagnosis_json("registry_auth_missing"),
            diagnosis_json("registry_auth_missing"),
            diagnosis_json("image_tag_not_found", patch="image: a:v2"),
            diagnosis_json("registry_auth_missing"),  # wrong answer on case-tag
            diagnosis_json("registry_auth_missing"),
            diagnosis_json("registry_auth_missing"),
        ]
    )
    harness = TierAHarness(
        golden_dir=golden, model="fake/model", client=client, split="test", repeats=1,
        samples=2, run_id=RUN_ID, trace_dir=runs / RUN_ID / "traces",
    )
    write_records(harness.run(), runs / f"{RUN_ID}.jsonl")
    return runs


@pytest.fixture
def client(runs_dir: Path) -> TestClient:
    return TestClient(create_app(runs_dir))


def test_index_lists_runs(client: TestClient):
    page = client.get("/")
    assert page.status_code == 200
    assert RUN_ID in page.text
    assert "50%" in page.text  # one of two cases right


def test_run_page_has_scoreboard_chart_and_table_view(client: TestClient):
    page = client.get(f"/runs/{RUN_ID}")
    assert page.status_code == 200
    assert 'class="reliability"' in page.text
    assert "Table view" in page.text
    assert "registry_auth_missing" in page.text and "image_tag_not_found" in page.text
    assert "&#10007; fail" in page.text  # status carries an icon and a word, not colour alone


def test_trace_page_escapes_tool_output(client: TestClient):
    page = client.get(f"/runs/{RUN_ID}/cases/case-auth/1")
    assert page.status_code == 200
    assert XSS not in page.text
    assert "&lt;script&gt;" in page.text
    assert "Calls get_logs" in page.text


@pytest.mark.parametrize(
    "path",
    [
        "/runs/does-not-exist",
        f"/runs/{RUN_ID}/cases/case-auth/9",
        f"/runs/{RUN_ID}/cases/..%2F..%2Fsecrets/1",
        "/runs/..%2F..%2Fetc%2Fpasswd",
    ],
)
def test_unknown_or_crafted_ids_are_404(client: TestClient, path: str):
    assert client.get(path).status_code == 404


def test_compare_page_renders_deltas(client: TestClient):
    page = client.get("/compare", params={"baseline": RUN_ID, "candidate": RUN_ID})
    assert page.status_code == 200
    assert "Run comparison" in page.text
    assert "+0.000" in page.text


def test_empty_runs_dir_renders(tmp_path: Path):
    page = TestClient(create_app(tmp_path / "nothing")).get("/")
    assert page.status_code == 200
    assert "No runs yet" in page.text


def test_reliability_svg_has_accessible_hit_targets(runs_dir: Path):
    from chaosloop.eval.harness import read_records

    records = read_records(runs_dir / f"{RUN_ID}.jsonl")
    series = reliability_series(records)
    assert {s.style.source for s in series} == {"self_reported", "consistency"}
    svg = reliability_svg(series)
    assert svg.count('class="hit"') == sum(len(s.points) for s in series)
    assert 'tabindex="0"' in svg
    assert "perfect calibration" in svg


def test_run_page_reports_against_the_gate(runs_dir: Path, tmp_path: Path):
    gate = tmp_path / "thresholds.yaml"
    gate.write_text("category_accuracy_min: 0.9\nfalse_positive_rate_max: 0.2\n")
    page = TestClient(create_app(runs_dir, gate)).get(f"/runs/{RUN_ID}")
    assert "banner error" in page.text
    assert "category_accuracy 0.500" in page.text
    assert "misses gate" in page.text and "meets gate" in page.text


def test_without_a_thresholds_file_nothing_claims_a_verdict(runs_dir: Path, tmp_path: Path):
    page = TestClient(create_app(runs_dir, tmp_path / "missing.yaml")).get(f"/runs/{RUN_ID}")
    assert "meets gate" not in page.text and "misses gate" not in page.text
    assert "banner success" not in page.text


def test_tile_gates_judge_min_and_max_rules():
    from chaosloop.dashboard.app import tile_gates
    from chaosloop.eval.harness import Thresholds, summarize

    metrics = summarize([]).model_copy(
        update={"n_runs": 4, "n_scored": 4, "category_accuracy": 0.9, "false_positive_rate": 0.5}
    )
    gates = tile_gates(metrics, Thresholds(category_accuracy_min=0.85, false_positive_rate_max=0.2))
    assert gates["category"] == {"ok": True, "rule": "≥ 85%"}
    assert gates["false_positive"]["ok"] is False
    assert tile_gates(metrics, None) == {}


def test_compare_direction_respects_lower_is_better():
    from chaosloop.dashboard.app import compare_rows
    from chaosloop.eval.harness import summarize

    before = summarize([]).model_copy(update={"category_accuracy": 0.8, "false_positive_rate": 0.1})
    after = summarize([]).model_copy(update={"category_accuracy": 0.9, "false_positive_rate": 0.3})
    rows = {r["label"]: r for r in compare_rows(before, after)}
    assert rows["Category accuracy"]["direction"] == "better"
    assert rows["False-positive rate"]["direction"] == "worse"
    assert rows["Fix accuracy"]["direction"] == "same"


def test_trace_page_marks_disagreeing_samples(client: TestClient):
    page = client.get(f"/runs/{RUN_ID}/cases/case-auth/1")
    assert "banner success" in page.text
    assert "&#10007; image_tag_not_found" in page.text  # the one sample that disagreed


def test_trace_steps_flatten_calls_results_and_answer():
    steps = trace_steps(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "alert"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "get_events", "arguments": "{}"}}],
            },
            {"role": "tool", "name": "get_events", "content": "[]"},
            {"role": "assistant", "content": '{"category": "no_fault"}'},
        ]
    )
    assert [s["kind"] for s in steps] == ["system", "user", "call", "tool", "answer"]


def test_live_run_page_shows_the_lifecycle(tmp_path: Path):
    from chaosloop.schemas.eval_record import EvalRecord

    base = dict(
        run_id="20261002T090000Z-live01", tier="B", repeat=1, model="oracle",
        prompt_version="oracle", golden_version="cat:sha256:abc", fix_correct=True,
        is_false_positive=False, malformed=False, confidence=1.0,
    )
    records = [
        EvalRecord(**base, case_id="cart-high-latency", experiment_id="cart-high-latency",
                   parameters={"latency_ms": 2500}, expected_category="high_latency",
                   agent_category="high_latency", category_correct=True, detected=True,
                   trigger_alertname="UpstreamSlow", t_inject=100.0, t_detect=160.0,
                   t_diagnose=161.0, t_recover=250.0, fix_applied=True, fix_verified=True),
        EvalRecord(**base, case_id="cart-quota-exceeded", experiment_id="cart-quota-exceeded",
                   expected_category="quota_exceeded", agent_category=None,
                   category_correct=False, detected=False, error="detection_miss",
                   t_inject=300.0),
    ]
    runs = tmp_path / "runs"
    write_records(records, runs / "20261002T090000Z-live01.jsonl")
    client = TestClient(create_app(runs))

    page = client.get("/runs/20261002T090000Z-live01")
    assert page.status_code == 200, page.text
    assert "Live lifecycle" in page.text and "live chaos (Tier B)" in page.text
    assert "latency_ms=2500" in page.text and "UpstreamSlow" in page.text
    assert "60 s" in page.text and "150 s" in page.text  # detect, recover
    assert "&#10007; missed" in page.text
    assert "live" in client.get("/").text


def test_live_runs_are_not_judged_against_the_tier_a_gate(tmp_path: Path):
    from chaosloop.schemas.eval_record import EvalRecord

    record = EvalRecord(
        run_id="20261002T090000Z-live02", case_id="x", experiment_id="x", tier="B", repeat=1,
        model="oracle", prompt_version="oracle", golden_version="cat",
        expected_category="pvc_unbound",
        agent_category="pvc_unbound", category_correct=True, fix_correct=True,
        is_false_positive=False, malformed=False, confidence=1.0, detected=True,
    )
    write_records([record], tmp_path / "runs" / "20261002T090000Z-live02.jsonl")
    thresholds = tmp_path / "thresholds.yaml"
    thresholds.write_text("category_accuracy_min: 0.5\nfix_accuracy_min: 0.5\n"
                          "false_positive_rate_max: 0.2\n")
    page = TestClient(create_app(tmp_path / "runs", thresholds)).get(
        "/runs/20261002T090000Z-live02"
    )
    assert "gate" not in page.text.lower()
