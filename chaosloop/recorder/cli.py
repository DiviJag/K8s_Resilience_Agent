"""`chaosloop-record` - capture a live lab failure as a candidate golden case.

Run this straight after injecting a fault, while the evidence is still in the cluster:
events expire (HLD §12), so the snapshot has to be taken at detection time.

    chaosloop-lab inject cart-registry-auth-missing
    uv run chaosloop-record capture --service cart-service --category registry_auth_missing \
        --root-cause "No imagePullSecrets for the private registry" \
        --fix-path spec.template.spec.imagePullSecrets
"""

from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from chaosloop.recorder.recorder import Recorder, write_golden_case
from chaosloop.schemas.golden_case import CaseMetadata, Expected, FixCheck
from chaosloop.schemas.taxonomy import Category
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.live_backend import LiveBackend

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)


@app.callback()
def main() -> None:
    """Keeps `capture` an explicit subcommand; Typer would otherwise flatten a lone command."""


@app.command()
def capture(
    service: Annotated[str, typer.Option(help="Deployment and Service name")],
    category: Annotated[str, typer.Option(help="True category - you injected it, so you know")],
    root_cause: Annotated[str, typer.Option(help="One or two sentences of ground truth")],
    fix_path: Annotated[str, typer.Option(help="Spec path a correct fix must touch")],
    namespace: str = "chaosloop-lab",
    pod: Annotated[str, typer.Option(help="Defaults to the first unready pod")] = "",
    alertname: str = "",
    case_id: Annotated[str, typer.Option(help="Defaults to <category>-<n>")] = "",
    split: Annotated[str, typer.Option(help="dev or test")] = "dev",
    fix_must_exist: bool = True,
    fix_any_of: Annotated[
        list[str] | None, typer.Option(help="Another equally correct fix path; repeatable")
    ] = None,
    pvc: str = "",
    context: str = "kind-chaosloop",
    out_dir: Path = Path("golden"),
    k8s_version: str = "1.30",
) -> None:
    if category not in {c.value for c in Category}:
        typer.secho(f"unknown category {category!r}", fg=typer.colors.RED)
        raise typer.Exit(2)
    if split not in ("dev", "test"):
        typer.secho("--split must be dev or test", fg=typer.colors.RED)
        raise typer.Exit(2)

    backend = LiveBackend(context=context)
    resolved_pod = pod or backend.find_unready_pod(namespace, service)
    if not resolved_pod:
        typer.secho(f"no pods found for app={service} in {namespace}", fg=typer.colors.RED)
        raise typer.Exit(1)

    trigger = Trigger(
        alertname=alertname or _default_alertname(category),
        service=service,
        namespace=namespace,
        pod=resolved_pod,
    )
    evidence = Recorder(backend).capture(trigger, targets={"pvc": pvc or None})

    path = write_golden_case(
        case_id=case_id or _next_case_id(out_dir, category),
        split=split,  # type: ignore[arg-type]
        trigger=trigger,
        evidence=evidence,
        expected=Expected(
            category=Category(category),
            root_cause=root_cause,
            fix_check=FixCheck(
                path=fix_path, any_of=fix_any_of or [], must_exist=fix_must_exist
            ),
        ),
        metadata=CaseMetadata(
            source="chaos", k8s_version=k8s_version, runtime="containerd", recorded=date.today()
        ),
        out_dir=out_dir,
    )

    recorded = [tool for tool, value in evidence.items() if value != "not available"]
    typer.echo(f"captured {len(recorded)}/{len(evidence)} tools: {', '.join(recorded)}")
    typer.secho(f"wrote {path}", fg=typer.colors.GREEN)
    typer.echo("Review the label before committing - an unreviewed case corrupts the benchmark.")


def _default_alertname(category: str) -> str:
    if category.startswith("registry_") or category.startswith("image_"):
        return "PodImagePullBackOff"
    if category in ("missing_env_var", "bad_config_value", "failing_liveness_probe"):
        return "PodCrashLooping"
    if category in ("memory_limit_too_low", "memory_leak"):
        return "PodOOMKilled"
    if category in (
        "insufficient_resources",
        "node_selector_mismatch",
        "taint_not_tolerated",
        "pvc_unbound",
        "quota_exceeded",
    ):
        return "PodPendingTooLong"
    if category in ("selector_mismatch", "target_port_mismatch", "network_policy_block"):
        return "ServiceHasNoEndpoints"
    return "WorkloadDegraded"


def _next_case_id(out_dir: Path, category: str) -> str:
    existing = len(list(out_dir.rglob(f"{category}-*.yaml")))
    return f"{category}-{existing + 1:02d}"


if __name__ == "__main__":
    app()
