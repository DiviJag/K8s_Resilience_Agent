from pathlib import Path

from typer.testing import CliRunner

from chaosloop.eval.calibration import IsotonicCalibrator
from chaosloop.eval.cli import app
from chaosloop.eval.harness import TierAHarness, write_records
from tests.conftest import FakeLLMClient, diagnosis_json, write_case

runner = CliRunner()
AUTH = diagnosis_json("registry_auth_missing")
TAG = diagnosis_json("image_tag_not_found", patch="image: a:v2")


def make_run(tmp_path: Path, split: str, script: list[str], name: str) -> Path:
    golden = tmp_path / f"golden-{name}"
    write_case(golden / split, "case-a", "registry_auth_missing", split=split)
    records = TierAHarness(
        golden_dir=golden, model="m", client=FakeLLMClient(script), split=split, repeats=2,
        samples=2,
    ).run()
    return write_records(records, tmp_path / f"{name}.jsonl")


def test_calibrate_refuses_test_split_results(tmp_path: Path):
    run = make_run(tmp_path, "test", [AUTH, AUTH, TAG] * 2, "test-run")
    result = runner.invoke(app, ["calibrate", str(run), "--out", str(tmp_path / "cal.json")])
    assert result.exit_code == 2
    assert "refusing" in result.output
    assert not (tmp_path / "cal.json").exists()


def test_calibrate_refuses_a_fit_with_no_wrong_answers(tmp_path: Path):
    run = make_run(tmp_path, "dev", [AUTH, AUTH, TAG] * 2, "dev-run")
    out = tmp_path / "cal.json"
    result = runner.invoke(app, ["calibrate", str(run), "--out", str(out)])
    assert result.exit_code == 2
    assert "incorrect" in result.output
    assert not out.exists()


def test_calibrate_fits_when_dev_has_errors(tmp_path: Path):
    # Repeat 1 right with split samples, repeat 2 wrong with unanimous samples.
    run = make_run(tmp_path, "dev", [AUTH, AUTH, TAG, TAG, TAG, TAG], "dev-run")
    out = tmp_path / "cal.json"
    result = runner.invoke(
        app, ["calibrate", str(run), "--out", str(out), "--min-incorrect", "1"]
    )
    assert result.exit_code == 0, result.output
    calibrator = IsotonicCalibrator.load(out)
    assert calibrator.n == 2
    assert calibrator.predict(1.0) < 1.0


def test_report_applies_a_calibrator_without_model_calls(tmp_path: Path):
    run = make_run(tmp_path, "test", [AUTH, AUTH, TAG] * 2, "test-run")
    cal = IsotonicCalibrator.fit([0.5], [False], fitted_on="dev").save(tmp_path / "cal.json")
    result = runner.invoke(app, ["report", str(run), "--calibrator", str(cal)])
    assert result.exit_code == 0, result.output
    assert "calibrated" in result.output


def test_compare_labels_memory_lift(tmp_path: Path):
    base = make_run(tmp_path, "test", [AUTH, AUTH, TAG] * 2, "base")
    result = runner.invoke(app, ["compare", str(base), str(base)])
    assert result.exit_code == 0, result.output
    assert "category_accuracy" in result.output
    assert "memory lift" not in result.output  # neither run used memory
