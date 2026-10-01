"""`chaosloop-memory` - curate the incident memory and inspect what it retrieves."""

from pathlib import Path
from typing import Annotated

import typer

from chaosloop.memory.promote import PromotionError, incident_from_case, write_incident
from chaosloop.memory.retriever import Memory
from chaosloop.memory.store import MemoryLeakError, assert_disjoint_from, load_memory
from chaosloop.schemas.golden_case import load_golden_set

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

GoldenDir = Annotated[Path, typer.Option("--golden-dir")]
MemoryDir = Annotated[Path, typer.Option("--memory-dir")]


@app.command()
def promote(
    case_ids: Annotated[list[str] | None, typer.Argument(help="Golden case ids to promote")] = None,
    by: Annotated[str, typer.Option("--by", help="Who confirmed the label")] = "",
    all_dev: Annotated[bool, typer.Option("--all-dev", help="Promote every dev case")] = False,
    resolution: Annotated[str, typer.Option(help="What fixed it, for a single case")] = "",
    golden_dir: GoldenDir = Path("golden"),
    memory_dir: MemoryDir = Path("memory"),
) -> None:
    """Promote confirmed golden cases into memory. Test-split cases are refused."""
    if not by:
        typer.secho("--by is required: memory only holds human-confirmed incidents", fg="red")
        raise typer.Exit(2)

    cases = {c.case_id: c for c in load_golden_set(golden_dir)}
    if all_dev:
        targets = [c for c in cases.values() if c.split == "dev"]
    else:
        missing = [cid for cid in case_ids or [] if cid not in cases]
        if missing or not case_ids:
            typer.secho(f"unknown or missing case ids: {missing or 'none given'}", fg="red")
            raise typer.Exit(2)
        targets = [cases[cid] for cid in case_ids]

    failed = False
    for case in targets:
        try:
            incident = incident_from_case(
                case, confirmed_by=by, resolution=resolution if len(targets) == 1 else None
            )
        except PromotionError as exc:
            typer.secho(f"refused {case.case_id}: {exc}", fg="red")
            failed = True
            continue
        path = write_incident(incident, memory_dir)
        typer.echo(f"{case.case_id} -> {path} ({len(incident.signals)} signals)")
    if failed:
        raise typer.Exit(1)


@app.command()
def check(golden_dir: GoldenDir = Path("golden"), memory_dir: MemoryDir = Path("memory")) -> None:
    """Fail if memory holds any incident promoted from a test-split case."""
    store = load_memory(memory_dir)
    test_ids = {c.case_id for c in load_golden_set(golden_dir, "test")}
    try:
        assert_disjoint_from(store, test_ids)
    except MemoryLeakError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    typer.secho(
        f"OK - {len(store.incidents)} incidents, {len(store.sections)} runbook sections, "
        f"disjoint from {len(test_ids)} test cases. snapshot={store.snapshot}",
        fg="green",
    )


@app.command()
def search(query: str, memory_dir: MemoryDir = Path("memory")) -> None:
    """Show exactly what the agent would get back for a query."""
    typer.echo(Memory(load_memory(memory_dir)).search_text(query))


if __name__ == "__main__":
    app()
