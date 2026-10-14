"""`chaosloop-lab` runs live experiments; `chaosloop-review` is the human gate on the flywheel."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from dotenv import load_dotenv

from chaosloop.eval.harness import new_run_id, read_records, summarize, write_records
from chaosloop.live.candidates import CandidateQueue
from chaosloop.live.catalog import Catalog, Experiment, load_catalog
from chaosloop.live.gateway import KubeGateway
from chaosloop.live.investigators import AgentInvestigator, Investigator, OracleInvestigator
from chaosloop.live.lifecycle import LifecycleController
from chaosloop.live.monitoring import AlertmanagerSource
from chaosloop.live.mutator import Mutator
from chaosloop.live.planner import category_stats, plan_batch
from chaosloop.schemas.eval_record import EvalRecord
from chaosloop.schemas.golden_case import GoldenCaseError, load_golden_set

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Run live (Tier B) experiments.")
review_app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Review candidate golden cases."
)

CATALOG_PATH = Path("lab/experiments.yaml")
INJECTOR_KUBECONFIG = Path("lab/.kubeconfig-injector")
INVESTIGATOR_KUBECONFIG = Path("lab/.kubeconfig-investigator")
JOURNAL_DIR = Path("lab/.journal")
GOLDEN_DIR = Path("golden")
RUNS_DIR = Path("eval/runs")

SPEND_MARGIN_USD = 0.15

Params = Annotated[list[str] | None, typer.Option("--param", help="name=value; repeatable")]


def _need(path: Path) -> str:
    if not path.is_file():
        typer.secho(f"{path} not found - run ./scripts/lab-up.ps1 first", fg="red")
        raise typer.Exit(2)
    return str(path)


def _parse_params(raw: list[str] | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in raw or []:
        name, _, value = item.partition("=")
        params[name] = yaml.safe_load(value)
    return params


def _gateway() -> KubeGateway:
    return KubeGateway(kubeconfig=_need(INJECTOR_KUBECONFIG), context=None)


def _all_records(runs_dir: Path) -> list[EvalRecord]:
    records: list[EvalRecord] = []
    for path in sorted(runs_dir.glob("*.jsonl")) if runs_dir.is_dir() else []:
        try:
            records.extend(read_records(path))
        except ValueError:
            continue
    return records


@app.command("catalog")
def show_catalog(catalog: Path = CATALOG_PATH) -> None:
    """List the experiments the planner may choose from."""
    loaded = load_catalog(catalog)
    typer.echo(f"{loaded.version}  {len(loaded.experiments)} experiments\n")
    for e in loaded.experiments:
        params = ", ".join(f"{k}={p.allowed}" for k, p in e.parameters.items())
        typer.echo(f"{e.id:<36} {e.category.value:<30} {e.injector:<6} {params}")


@app.command()
def plan(
    count: Annotated[int, typer.Option(help="How many runs to plan ahead")] = 5,
    runs_dir: Path = RUNS_DIR,
    catalog: Path = CATALOG_PATH,
) -> None:
    """Show what the planner would run next, and why (HLD 10)."""
    loaded = load_catalog(catalog)
    records = _all_records(runs_dir)
    typer.echo(f"{'category':<32}{'samples':>8}{'accuracy':>10}{'detect miss':>13}")
    for s in category_stats(records, loaded.categories()):
        acc = f"{s.accuracy:.0%}" if s.accuracy is not None else "-"
        miss = f"{s.detection_miss:.0%}" if s.detection_miss is not None else "-"
        typer.echo(f"{s.category.value:<32}{s.samples:>8}{acc:>10}{miss:>13}")
    typer.echo("")
    for index, p in enumerate(plan_batch(loaded, records, count), start=1):
        params = f" {p.parameters}" if p.parameters else ""
        typer.echo(f"{index}. {p.experiment_id}{params}\n   {p.reason}")


@app.command()
def run(
    experiment: Annotated[list[str] | None, typer.Option(help="Experiment id; repeatable")] = None,
    planned: Annotated[int, typer.Option("--plan", help="Let the planner pick N runs")] = 0,
    agent: Annotated[str, typer.Option(help="oracle, or 'llm' for the LLM agent")] = "oracle",
    model: Annotated[str, typer.Option(help="OpenRouter model id for --agent llm")] = "",
    samples: int = 5,
    memory: Annotated[bool, typer.Option("--memory/--no-memory")] = False,
    repeats: int = 1,
    param: Params = None,
    max_spend: Annotated[
        float, typer.Option(help="USD; never start an investigation that could pass this")
    ] = 5.0,
    investigation_budget: Annotated[
        float, typer.Option(help="USD cap per investigation, resamples included")
    ] = 0.50,
    prometheus_url: Annotated[
        str,
        typer.Option(
            help=(
                "Prometheus base URL for steady-state upstream-error-rate probing; empty disables"
            ),
        ),
    ] = "",
    upstream_error_rate_max: Annotated[
        float,
        typer.Option(help="Steady-state ceiling for observed upstream error rate (0-1)"),
    ] = 0.05,
    catalog: Path = CATALOG_PATH,
    out_dir: Path = RUNS_DIR,
    golden_dir: Path = GOLDEN_DIR,
) -> None:
    """Run live experiments through the full lifecycle and record Tier B results."""
    loaded = load_catalog(catalog)
    queue = _queue(loaded, experiment or [], planned, _parse_params(param), out_dir)
    if not queue:
        typer.secho("nothing to run: pass --experiment or --plan", fg="red")
        raise typer.Exit(2)

    gateway = _gateway()
    mutator = Mutator(gateway, JOURNAL_DIR)
    leftovers = mutator.reconcile()
    for run_key, problems in leftovers.items():
        status = "restored" if not problems else f"PROBLEMS: {problems}"
        typer.secho(f"found an experiment left behind ({run_key}): {status}", fg="yellow")

    from chaosloop.live.lifecycle import LiveSettings
    from chaosloop.live.monitoring import PrometheusProbe
    from chaosloop.tools.live_backend import LiveBackend

    upstream = PrometheusProbe(base_url=prometheus_url) if prometheus_url else None
    controller = LifecycleController(
        gateway=gateway,
        alerts=AlertmanagerSource(),
        mutator=mutator,
        backend=LiveBackend(kubeconfig=_need(INVESTIGATOR_KUBECONFIG), context=None),
        investigator_for=_investigators(agent, model, samples, memory, investigation_budget),
        catalog_version=loaded.version,
        candidates=CandidateQueue(golden_dir),
        golden=load_golden_set(golden_dir),
        settings=LiveSettings(upstream_error_rate_max=upstream_error_rate_max),
        upstream=upstream,
        on_event=lambda message: typer.echo(f"  {datetime.now(UTC):%H:%M:%S} {message}"),
    )

    run_id = new_run_id()
    path = out_dir / f"{run_id}.jsonl"
    records: list[EvalRecord] = []
    # The per-investigation budget is checked before each model call, so one call can overshoot
    # it; the margin covers that call.
    worst_case = investigation_budget + SPEND_MARGIN_USD
    for resolved, params in queue:
        for repeat in range(1, repeats + 1):
            spent = sum(r.cost_usd or 0.0 for r in records)
            if agent == "llm" and spent + worst_case > max_spend:
                typer.secho(
                    f"\nstopping: ${spent:.2f} spent, and one more investigation could cost up "
                    f"to ${worst_case:.2f}, over the ${max_spend:.2f} limit",
                    fg="yellow",
                )
                break
            typer.secho(f"\n{resolved.id} r{repeat} {params or ''}", bold=True)
            records.append(controller.run(resolved, run_id, repeat, params))
            write_records(records, path)  # rewritten each time so a crash keeps what finished
            _echo(records[-1])
        else:
            continue
        break

    live = summarize(records).live
    if live:
        typer.echo(
            f"\nexperiments={live.n_experiments} aborted={live.n_aborted} "
            f"detection_miss={live.detection_miss_rate:.0%} "
            f"fix_success={_pct(live.fix_success_rate)} "
            f"ttd={_secs(live.mean_time_to_detect_s)} ttr={_secs(live.mean_time_to_recover_s)}"
        )
    if agent == "llm":
        typer.echo(f"LLM spend this run: ${sum(r.cost_usd or 0.0 for r in records):.2f}")
    typer.echo(f"wrote {path}")


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _secs(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}s"


def _queue(
    catalog: Catalog,
    ids: list[str],
    planned: int,
    params: dict[str, Any],
    runs_dir: Path,
) -> list[tuple[Experiment, dict[str, Any]]]:
    queue = []
    for experiment_id in ids:
        base = catalog.get(experiment_id)
        own = {k: v for k, v in params.items() if k in base.parameters}
        queue.append((base.resolve(own), own))
    if planned:
        for p in plan_batch(catalog, _all_records(runs_dir), planned):
            queue.append((catalog.get(p.experiment_id).resolve(p.parameters), p.parameters))
    return queue


def _investigators(
    agent: str, model: str, samples: int, memory: bool, budget_usd: float = 0.50
) -> Any:
    if agent == "oracle":
        return OracleInvestigator
    if agent != "llm":
        typer.secho("--agent must be oracle or llm", fg="red")
        raise typer.Exit(2)
    load_dotenv()
    from chaosloop.agent.diagnostician import Budget
    from chaosloop.agent.llm_client import OpenRouterClient
    from chaosloop.memory.retriever import Memory
    from chaosloop.memory.store import load_memory

    model = model or os.environ.get("CHAOSLOOP_MODEL", "")
    if not model:
        typer.secho("no model: pass --model or set CHAOSLOOP_MODEL", fg="red")
        raise typer.Exit(2)
    investigator: Investigator = AgentInvestigator(
        model=model,
        client=OpenRouterClient(),
        samples=samples,
        memory=Memory(load_memory(Path("memory"))) if memory else None,
        budget=Budget(max_cost_usd=budget_usd),
    )
    return lambda experiment: investigator


def _echo(record: EvalRecord) -> None:
    if record.aborted:
        typer.secho(f"  ABORTED: {record.aborted}", fg="yellow")
        return
    if record.detected is False:
        typer.secho("  DETECTION MISS: no alert fired", fg="red")
        return
    verdict = "correct" if record.category_correct else "WRONG"
    fix = {True: "recovered", False: "did not recover", None: "n/a"}[record.fix_verified]
    detect = (record.t_detect or 0) - (record.t_inject or 0)
    typer.echo(
        f"  {verdict}: {record.agent_category.value if record.agent_category else '-'} "
        f"| detect {detect:.0f}s | fix {fix}"
        + (f" | ${record.cost_usd:.3f}" if record.cost_usd else "")
        + (f" ({record.fix_rejected})" if record.fix_rejected else "")
    )


@app.command()
def inject(
    experiment: str,
    param: Params = None,
    catalog: Path = CATALOG_PATH,
) -> None:
    """Inject one fault by hand and leave it in place - for recording a case."""
    loaded = load_catalog(catalog)
    base = loaded.get(experiment)
    resolved = base.resolve(_parse_params(param))
    key = f"manual-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{experiment}"
    Mutator(_gateway(), JOURNAL_DIR).inject(resolved, key)
    typer.secho(f"injected {experiment}; undo with: chaosloop-lab restore", fg="yellow")


@app.command()
def restore() -> None:
    """Undo every injected fault still recorded in the journal."""
    results = Mutator(_gateway(), JOURNAL_DIR).reconcile()
    if not results:
        typer.echo("nothing to restore")
    for key, problems in results.items():
        if problems:
            typer.secho(f"{key}: {problems}", fg="red")
        else:
            typer.secho(f"{key}: restored", fg="green")


@app.command()
def status(namespace: str = "chaosloop-lab") -> None:
    """Is the lab at steady state, and is anything left injected?"""
    gateway = _gateway()
    from chaosloop.live.monitoring import alert_problems, deployment_problems

    problems = deployment_problems(gateway, namespace)
    try:
        problems += alert_problems(AlertmanagerSource().firing(), namespace)
    except Exception as exc:  # Alertmanager unreachable is itself the thing to report
        problems.append(f"alertmanager unreachable: {exc}")
    pending = Mutator(gateway, JOURNAL_DIR).pending()
    for journal in pending:
        typer.secho(f"still injected: {journal.experiment_id} ({journal.run_id})", fg="yellow")
    if problems:
        for problem in problems:
            typer.secho(f"not steady: {problem}", fg="red")
        raise typer.Exit(1)
    typer.secho("steady state", fg="green")


# --- review -------------------------------------------------------------------------------


@review_app.command("list")
def review_list(golden_dir: Path = GOLDEN_DIR) -> None:
    """Candidates waiting for a human to confirm their label."""
    pending = CandidateQueue(golden_dir).pending()
    if not pending:
        typer.echo("no candidates waiting")
    for case in pending:
        reason = (case.metadata.model_extra or {}).get("candidate_reason", "")
        typer.echo(f"{case.case_id:<48} {case.expected.category.value:<28} {reason}")


@review_app.command()
def show(case_id: str, golden_dir: Path = GOLDEN_DIR) -> None:
    """Print a candidate in full, evidence included."""
    case = CandidateQueue(golden_dir).get(case_id)
    typer.echo(json.dumps(case.model_dump(mode="json"), indent=2))


@review_app.command()
def approve(
    case_id: str,
    split: Annotated[str, typer.Option(help="dev or test")],
    by: Annotated[str, typer.Option("--by", help="Who checked the label")],
    golden_dir: Path = GOLDEN_DIR,
) -> None:
    """Accept a candidate's label and add it to the golden set."""
    if split not in ("dev", "test"):
        typer.secho("--split must be dev or test", fg="red")
        raise typer.Exit(2)
    try:
        path = CandidateQueue(golden_dir).approve(case_id, split, by)  # type: ignore[arg-type]
    except GoldenCaseError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    typer.secho(f"added {path}", fg="green")


@review_app.command()
def reject(
    case_id: str,
    by: Annotated[str, typer.Option("--by")],
    reason: Annotated[str, typer.Option(help="Why the label or case is wrong")],
    golden_dir: Path = GOLDEN_DIR,
) -> None:
    """Refuse a candidate, keeping the record and the reason."""
    path = CandidateQueue(golden_dir).reject(case_id, by, reason)
    typer.secho(f"rejected; kept at {path}", fg="yellow")
