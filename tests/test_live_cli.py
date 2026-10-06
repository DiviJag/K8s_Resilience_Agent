from pathlib import Path

import yaml
from typer.testing import CliRunner

from chaosloop.live import cli
from chaosloop.live.candidates import CandidateQueue
from chaosloop.live.catalog import load_catalog
from chaosloop.schemas.golden_case import GoldenCase, load_golden_set
from tests.conftest import REPO_ROOT, write_case

runner = CliRunner()
CATALOG = REPO_ROOT / "lab" / "experiments.yaml"


def test_catalog_lists_every_experiment():
    result = runner.invoke(cli.app, ["catalog", "--catalog", str(CATALOG)])
    assert result.exit_code == 0, result.output
    for experiment in load_catalog(CATALOG).experiments:
        assert experiment.id in result.output


def test_plan_explains_each_choice(tmp_path: Path):
    result = runner.invoke(
        cli.app, ["plan", "--count", "3", "--runs-dir", str(tmp_path), "--catalog", str(CATALOG)]
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("sample(s), under the") == 3


def test_queue_resolves_explicit_parameters_and_ignores_foreign_ones(tmp_path: Path):
    catalog = load_catalog(CATALOG)
    queue = cli._queue(
        catalog, ["cart-high-latency", "billing-missing-env-var"],
        planned=0, params={"latency_ms": 2500}, runs_dir=tmp_path,
    )
    (latency, latency_params), (env, env_params) = queue
    assert latency_params == {"latency_ms": 2500}
    assert "2500" in latency.model_dump_json()
    assert env_params == {}


def test_queue_appends_planner_choices(tmp_path: Path):
    queue = cli._queue(load_catalog(CATALOG), [], planned=2, params={}, runs_dir=tmp_path)
    assert len(queue) == 2
    assert queue[0][0].id != queue[1][0].id


def test_run_without_anything_to_run_fails_before_touching_the_cluster(tmp_path: Path):
    result = runner.invoke(cli.app, ["run", "--catalog", str(CATALOG), "--out-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "nothing to run" in result.output


def test_params_are_parsed_as_yaml_scalars():
    assert cli._parse_params(["latency_ms=1800", "name=cart"]) == {
        "latency_ms": 1800, "name": "cart",
    }


def _candidate(tmp_path: Path, golden: Path, case_id: str) -> None:
    path = write_case(tmp_path / "src", case_id, "image_tag_not_found", split="dev")
    case = GoldenCase.model_validate(yaml.safe_load(path.read_text()))
    CandidateQueue(golden).propose(case, "first case of this category")


def test_review_list_approve_and_reject(tmp_path: Path):
    golden = tmp_path / "golden"
    _candidate(tmp_path, golden, "live-a")
    _candidate(tmp_path, golden, "live-b")

    listed = runner.invoke(cli.review_app, ["list", "--golden-dir", str(golden)])
    assert "live-a" in listed.output and "first case" in listed.output

    shown = runner.invoke(cli.review_app, ["show", "live-a", "--golden-dir", str(golden)])
    assert '"case_id": "live-a"' in shown.output

    bad_split = runner.invoke(
        cli.review_app,
        ["approve", "live-a", "--split", "prod", "--by", "divya", "--golden-dir", str(golden)],
    )
    assert bad_split.exit_code == 2

    approved = runner.invoke(
        cli.review_app,
        ["approve", "live-a", "--split", "test", "--by", "divya", "--golden-dir", str(golden)],
    )
    assert approved.exit_code == 0, approved.output
    assert [c.case_id for c in load_golden_set(golden, "test")] == ["live-a"]

    rejected = runner.invoke(
        cli.review_app,
        ["reject", "live-b", "--by", "divya", "--reason", "mislabelled",
         "--golden-dir", str(golden)],
    )
    assert rejected.exit_code == 0, rejected.output
    assert "no candidates waiting" in runner.invoke(
        cli.review_app, ["list", "--golden-dir", str(golden)]
    ).output


def test_model_runs_stop_before_they_can_pass_the_spend_limit(tmp_path: Path, monkeypatch):
    import chaosloop.tools.live_backend as live_backend
    from chaosloop.schemas.eval_record import EvalRecord

    class FakeController:
        def __init__(self, **kwargs):
            pass

        def run(self, experiment, run_id, repeat, parameters):
            return EvalRecord(
                run_id=run_id, case_id=experiment.id, experiment_id=experiment.id, tier="B",
                repeat=repeat, model="m", prompt_version="v3", golden_version="cat",
                expected_category=experiment.category, agent_category=experiment.category,
                category_correct=True, fix_correct=True, is_false_positive=False,
                malformed=False, confidence=0.9, detected=True, cost_usd=0.90,
            )

    class FakeMutator:
        def __init__(self, *args):
            pass

        def reconcile(self):
            return {}

    monkeypatch.setattr(cli, "_gateway", lambda: None)
    monkeypatch.setattr(cli, "_need", lambda path: str(path))
    monkeypatch.setattr(cli, "Mutator", FakeMutator)
    monkeypatch.setattr(cli, "LifecycleController", FakeController)
    monkeypatch.setattr(cli, "_investigators", lambda *args: None)
    monkeypatch.setattr(live_backend, "LiveBackend", lambda **kwargs: None)

    ids = [e.id for e in load_catalog(CATALOG).plannable()][:10]
    args = ["run", "--agent", "model", "--catalog", str(CATALOG), "--out-dir", str(tmp_path),
            "--golden-dir", str(tmp_path / "golden"), "--max-spend", "5"]
    for experiment_id in ids:
        args += ["--experiment", experiment_id]
    result = runner.invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    # 0.90 each, worst case 0.65 more: after 4 runs ($3.60) a fifth fits, after 5 ($4.50) not.
    assert result.output.count(" r1") == 5
    assert "stopping: $4.50 spent" in result.output
    assert "LLM spend this run: $4.50" in result.output
