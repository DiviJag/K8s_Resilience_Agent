"""`chaosloop-eval` - run the Tier A golden-set eval and gate on thresholds."""

import os
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from chaosloop.agent.diagnostician import Budget
from chaosloop.agent.llm_client import OpenRouterClient
from chaosloop.eval.harness import (
    TierAHarness,
    check_thresholds,
    is_provider_error,
    load_thresholds,
    summarize,
    write_records,
)
from chaosloop.schemas.eval_record import EvalRecord
from chaosloop.schemas.golden_case import GoldenCaseError, load_golden_set

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
    case: Annotated[str, typer.Option(help="Run one case_id only; skips the gate")] = "",
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

    harness = TierAHarness(
        golden_dir=golden_dir,
        model=model,
        client=OpenRouterClient(),
        split=split,  # type: ignore[arg-type]
        repeats=repeats,
        budget=Budget(max_steps=max_steps),
    )

    if case:
        harness.case_filter = case
        if not harness.cases():
            typer.secho(f"no case {case!r} in the {split} split", fg=typer.colors.RED)
            raise typer.Exit(2)

    typer.echo(f"run_id={harness.run_id}  model={model}  split={split}  repeats={repeats}")
    typer.echo(f"golden_version={harness.golden_version}\n")

    records = harness.run(on_record=_echo_record)
    path = write_records(records, out_dir / f"{harness.run_id}.jsonl")
    metrics = summarize(records)

    typer.echo("")
    typer.echo(f"cases={metrics.n_cases} runs={metrics.n_runs} scored={metrics.n_scored}")
    if metrics.n_errors:
        typer.secho(
            f"{metrics.n_errors} run(s) never reached the model and are excluded from accuracy",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"category_accuracy   {metrics.category_accuracy:.3f}")
    typer.echo(f"fix_accuracy        {metrics.fix_accuracy:.3f}")
    typer.echo(f"false_positive_rate {metrics.false_positive_rate:.3f}")
    typer.echo(f"malformed_rate      {metrics.malformed_rate:.3f}")
    typer.echo(f"error_rate          {metrics.error_rate:.3f}")
    typer.echo(f"mean_cost_usd       {metrics.mean_cost_usd:.4f}")
    typer.echo(f"mean_latency_s      {metrics.mean_latency_s:.2f}")
    typer.echo("")
    typer.echo(f"{'category':<32}{'runs':>6}{'acc':>8}{'fix':>8}{'conf':>8}")
    for category, stats in metrics.by_category.items():
        typer.echo(
            f"{category:<32}{int(stats['runs']):>6}"
            f"{stats['category_accuracy']:>8.2f}{stats['fix_accuracy']:>8.2f}"
            f"{stats['mean_confidence']:>8.2f}"
        )
    typer.echo(f"\nwrote {path}")

    violations = check_thresholds(metrics, load_thresholds(golden_dir / "thresholds.yaml"))
    if violations:
        for violation in violations:
            typer.secho(f"THRESHOLD FAIL: {violation}", fg=typer.colors.RED)
        if not no_gate:
            raise typer.Exit(1)
    else:
        typer.secho("thresholds OK", fg=typer.colors.GREEN)


def _echo_record(record: EvalRecord) -> None:
    if is_provider_error(record):
        typer.secho(
            f"[ERR ] {record.case_id} r{record.repeat} {record.error}", fg=typer.colors.YELLOW
        )
        return
    mark = "PASS" if record.category_correct else "FAIL"
    actual = record.agent_category.value if record.agent_category else "MALFORMED"
    tools = ",".join(call.tool.removeprefix("get_") for call in record.tool_calls) or "-"
    typer.echo(
        f"[{mark}] {record.case_id} r{record.repeat} "
        f"expected={record.expected_category.value} got={actual} "
        f"fix={'Y' if record.fix_correct else 'N'} tools={tools}"
    )


if __name__ == "__main__":
    app()
