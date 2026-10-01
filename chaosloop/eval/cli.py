"""`chaosloop-eval` - run the Tier A golden-set eval, calibrate, report and compare runs."""

import os
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from chaosloop.agent.diagnostician import Budget
from chaosloop.agent.llm_client import OpenRouterClient
from chaosloop.eval.calibration import IsotonicCalibrator
from chaosloop.eval.harness import (
    Metrics,
    TierAHarness,
    apply_calibrator,
    check_thresholds,
    is_provider_error,
    load_thresholds,
    read_records,
    summarize,
    write_records,
)
from chaosloop.eval.judge import RootCauseJudge
from chaosloop.memory.retriever import Memory
from chaosloop.memory.store import load_memory
from chaosloop.schemas.eval_record import EvalRecord
from chaosloop.schemas.golden_case import GoldenCaseError, load_golden_set

DEFAULT_JUDGE_MODEL = "openai/gpt-4.1-mini"

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

GoldenDir = Annotated[Path, typer.Option("--golden-dir", help="Root of the golden set")]


@app.command()
def validate(golden_dir: GoldenDir = Path("golden")) -> None:
    """Load and validate every golden case. Needs no API key - safe to run in CI."""
    try:
        cases = load_golden_set(golden_dir)
    except GoldenCaseError as exc:
        typer.secho(f"invalid golden case: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    for split in ("dev", "test"):
        subset = [c for c in cases if c.split == split]
        categories = sorted({c.expected.category.value for c in subset})
        typer.echo(f"{split}: {len(subset)} cases, {len(categories)} categories")
        for category in categories:
            count = sum(1 for c in subset if c.expected.category.value == category)
            typer.echo(f"  {category:<32} {count}")
    typer.secho(f"OK - {len(cases)} cases valid", fg=typer.colors.GREEN)


@app.command()
def run(
    split: Annotated[str, typer.Option(help="dev (tuning) or test (reported numbers)")] = "test",
    repeats: Annotated[int, typer.Option(help="Runs per case; LLM output is not stable")] = 3,
    model: Annotated[str, typer.Option(help="OpenRouter model id")] = "",
    golden_dir: GoldenDir = Path("golden"),
    out_dir: Annotated[Path, typer.Option(help="Where run JSONL lands")] = Path("eval/runs"),
    max_steps: Annotated[int, typer.Option(help="Tool-call budget per investigation")] = 8,
    case: Annotated[str, typer.Option(help="Run one case_id only")] = "",
    samples: Annotated[int, typer.Option(help="Self-consistency resamples; 0 disables")] = 5,
    memory: Annotated[
        bool, typer.Option("--memory/--no-memory", help="Offer search_memory")
    ] = False,
    memory_dir: Annotated[Path, typer.Option()] = Path("memory"),
    judge_model: Annotated[str, typer.Option(help="Root-cause judge; must differ")] = "",
    no_judge: Annotated[bool, typer.Option("--no-judge", help="Skip root-cause grading")] = False,
    calibrator: Annotated[Path | None, typer.Option(help="Isotonic calibrator JSON")] = None,
    no_gate: Annotated[bool, typer.Option("--no-gate", help="Report but never fail")] = False,
) -> None:
    """Replay the golden set through the agent and grade every run."""
    load_dotenv()
    if split not in ("dev", "test"):
        typer.secho("--split must be dev or test", fg=typer.colors.RED)
        raise typer.Exit(2)

    model = model or os.environ.get("CHAOSLOOP_MODEL", "")
    if not model:
        typer.secho("no model: pass --model or set CHAOSLOOP_MODEL", fg=typer.colors.RED)
        raise typer.Exit(2)

    client = OpenRouterClient()
    judge = None
    if not no_judge:
        judge_id = judge_model or os.environ.get("CHAOSLOOP_JUDGE_MODEL") or DEFAULT_JUDGE_MODEL
        judge = RootCauseJudge(client, model=judge_id, agent_model=model)

    harness = TierAHarness(
        golden_dir=golden_dir,
        model=model,
        client=client,
        split=split,  # type: ignore[arg-type]
        repeats=repeats,
        budget=Budget(max_steps=max_steps),
        case_filter=case or None,
        samples=samples,
        memory=Memory(load_memory(memory_dir)) if memory else None,
        judge=judge,
        calibrator=IsotonicCalibrator.load(calibrator) if calibrator else None,
    )
    harness.trace_dir = out_dir / harness.run_id / "traces"

    if case and not harness.cases():
        typer.secho(f"no case {case!r} in the {split} split", fg=typer.colors.RED)
        raise typer.Exit(2)

    typer.echo(
        f"run_id={harness.run_id}  model={model}  split={split}  repeats={repeats}  "
        f"samples={samples}  memory={'on' if memory else 'off'}  "
        f"judge={judge.model if judge else 'off'}"
    )
    typer.echo(f"golden_version={harness.golden_version}\n")

    records = harness.run(on_record=_echo_record)
    path = write_records(records, out_dir / f"{harness.run_id}.jsonl")
    metrics = summarize(records)
    _print_metrics(metrics)
    typer.echo(f"\nwrote {path}")

    if case:
        return
    violations = check_thresholds(metrics, load_thresholds(golden_dir / "thresholds.yaml"))
    if violations:
        for violation in violations:
            typer.secho(f"THRESHOLD FAIL: {violation}", fg=typer.colors.RED)
        if not no_gate:
            raise typer.Exit(1)
    else:
        typer.secho("thresholds OK", fg=typer.colors.GREEN)


@app.command()
def calibrate(
    run_file: Annotated[Path, typer.Argument(help="A dev-split run JSONL")],
    out: Annotated[Path, typer.Option(help="Where the calibrator lands")] = Path(
        "calibration/isotonic.json"
    ),
    min_incorrect: Annotated[
        int, typer.Option(help="Wrong answers the fit needs before it can mean anything")
    ] = 3,
) -> None:
    """Fit an isotonic calibrator on a dev run's self-consistency confidence (HLD 7.4)."""
    records = [r for r in read_records(run_file) if not is_provider_error(r)]
    if any(r.split == "test" for r in records):
        typer.secho(
            "refusing to fit on test-split results: the calibrator would then be graded on the "
            "data it was fit to",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    pairs = [
        (r.consistency_confidence, r.category_correct)
        for r in records
        if r.consistency_confidence is not None
    ]
    if not pairs:
        typer.secho("no self-consistency confidence in this run; rerun with --samples", fg="red")
        raise typer.Exit(2)
    incorrect = sum(1 for _, correct in pairs if not correct)
    if incorrect < min_incorrect:
        # Fit on all-correct outcomes, isotonic regression learns "always 100%", which then
        # inflates every confidence on the test split - worse than no calibrator at all.
        typer.secho(
            f"only {incorrect} incorrect outcome(s) in {len(pairs)}; a calibrator needs at least "
            f"{min_incorrect} to learn where confidence should drop. Add harder dev cases, or "
            "keep using raw self-consistency.",
            fg="red",
        )
        raise typer.Exit(2)

    run_ids = sorted({r.run_id for r in records})
    fitted = IsotonicCalibrator.fit(
        [c for c, _ in pairs], [o for _, o in pairs], fitted_on=",".join(run_ids)
    )
    fitted.save(out)
    typer.echo(f"fitted on {fitted.n} outcomes from {', '.join(run_ids)}")
    for x, y in zip(fitted.x, fitted.y, strict=True):
        typer.echo(f"  consistency {x:.2f} -> {y:.2f}")
    typer.secho(f"wrote {out}", fg=typer.colors.GREEN)


@app.command()
def report(
    run_file: Annotated[Path, typer.Argument(help="Run JSONL to summarise")],
    calibrator: Annotated[Path | None, typer.Option(help="Apply a calibrator post hoc")] = None,
) -> None:
    """Recompute metrics from a stored run - no model calls, no cost."""
    records = read_records(run_file)
    if calibrator is not None:
        records = apply_calibrator(records, IsotonicCalibrator.load(calibrator))
    _print_metrics(summarize(records))


@app.command()
def compare(
    baseline: Annotated[Path, typer.Argument(help="Run JSONL to compare against")],
    candidate: Annotated[Path, typer.Argument(help="Run JSONL being evaluated")],
) -> None:
    """Per-category deltas between two runs - e.g. memory lift (HLD 7.3)."""
    base_records, cand_records = read_records(baseline), read_records(candidate)
    base, cand = summarize(base_records), summarize(cand_records)

    base_cases = {r.case_id for r in base_records}
    cand_cases = {r.case_id for r in cand_records}
    if base_cases != cand_cases:
        typer.secho("warning: the runs cover different cases; deltas are not like for like",
                    fg=typer.colors.YELLOW)
    is_lift = all(r.memory_snapshot is None for r in base_records) and all(
        r.memory_snapshot is not None for r in cand_records
    )
    typer.echo("memory lift (candidate has memory, baseline does not)\n" if is_lift else "")

    def row(name: str, a: float | None, b: float | None) -> None:
        if a is None or b is None:
            typer.echo(f"{name:<22}{_fmt(a):>10}{_fmt(b):>10}{'':>10}")
        else:
            typer.echo(f"{name:<22}{a:>10.3f}{b:>10.3f}{b - a:>+10.3f}")

    typer.echo(f"{'metric':<22}{'baseline':>10}{'candidate':>10}{'delta':>10}")
    row("category_accuracy", base.category_accuracy, cand.category_accuracy)
    row("fix_accuracy", base.fix_accuracy, cand.fix_accuracy)
    row("false_positive_rate", base.false_positive_rate, cand.false_positive_rate)
    row("root_cause_score", base.root_cause_score, cand.root_cause_score)
    row("ece", _ece(base), _ece(cand))
    row("mean_cost_usd", base.mean_cost_usd, cand.mean_cost_usd)

    typer.echo(f"\n{'category':<32}{'baseline':>10}{'candidate':>10}{'delta':>10}")
    for category in sorted(set(base.by_category) | set(cand.by_category)):
        a = (base.by_category.get(category) or {}).get("category_accuracy")
        b = (cand.by_category.get(category) or {}).get("category_accuracy")
        delta = f"{b - a:>+10.2f}" if a is not None and b is not None else f"{'':>10}"
        typer.echo(f"{category:<32}{_fmt(a, 2):>10}{_fmt(b, 2):>10}{delta}")


def _ece(metrics: Metrics) -> float | None:
    system = metrics.calibration.get("system")
    return system.ece if system else None


def _fmt(value: float | None, places: int = 3) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _print_metrics(metrics: Metrics) -> None:
    typer.echo("")
    typer.echo(f"cases={metrics.n_cases} runs={metrics.n_runs} scored={metrics.n_scored}")
    if metrics.n_errors:
        typer.secho(
            f"{metrics.n_errors} run(s) never reached the model and are excluded from accuracy",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"category_accuracy   {metrics.category_accuracy:.3f}")
    typer.echo(f"fix_accuracy        {metrics.fix_accuracy:.3f}")
    typer.echo(f"root_cause_score    {_fmt(metrics.root_cause_score)}")
    typer.echo(f"false_positive_rate {metrics.false_positive_rate:.3f}")
    typer.echo(f"malformed_rate      {metrics.malformed_rate:.3f}")
    typer.echo(f"error_rate          {metrics.error_rate:.3f}")
    if metrics.memory_usage_rate is not None:
        typer.echo(f"memory_usage_rate   {metrics.memory_usage_rate:.3f}")
    typer.echo(f"mean_cost_usd       {metrics.mean_cost_usd:.4f}")
    typer.echo(f"mean_latency_s      {metrics.mean_latency_s:.2f}")

    if metrics.calibration:
        typer.echo(f"\n{'confidence source':<20}{'n':>5}{'ECE':>8}{'Brier':>8}{'gap':>8}")
        for name, summary in metrics.calibration.items():
            typer.echo(
                f"{name:<20}{summary.n:>5}{summary.ece:>8.3f}{summary.brier:>8.3f}"
                f"{summary.gap:>+8.3f}"
            )

    typer.echo(
        f"\n{'category':<32}{'runs':>6}{'acc':>7}{'fix':>7}{'conf':>7}{'gap':>8}{'rc':>6}"
    )
    for category, stats in metrics.by_category.items():
        gap = stats["calib_gap"]
        typer.echo(
            f"{category:<32}{int(stats['runs'] or 0):>6}"
            f"{_fmt(stats['category_accuracy'], 2):>7}{_fmt(stats['fix_accuracy'], 2):>7}"
            f"{_fmt(stats['mean_confidence'], 2):>7}"
            f"{(f'{gap:+.2f}' if gap is not None else '-'):>8}"
            f"{_fmt(stats['root_cause_score'], 2):>6}"
        )


def _echo_record(record: EvalRecord) -> None:
    if is_provider_error(record):
        typer.secho(
            f"[ERR ] {record.case_id} r{record.repeat} {record.error}", fg=typer.colors.YELLOW
        )
        return
    mark = "PASS" if record.category_correct else "FAIL"
    actual = record.agent_category.value if record.agent_category else "MALFORMED"
    tools = ",".join(call.tool.removeprefix("get_") for call in record.tool_calls) or "-"
    confidence = f" conf={record.confidence:.2f}" if record.confidence is not None else ""
    rc = f" rc={record.root_cause_score:g}" if record.root_cause_score is not None else ""
    typer.echo(
        f"[{mark}] {record.case_id} r{record.repeat} "
        f"expected={record.expected_category.value} got={actual} "
        f"fix={'Y' if record.fix_correct else 'N'}{confidence}{rc} tools={tools}"
    )


if __name__ == "__main__":
    app()
