"""`chaosloop-dashboard` - serve the local eval dashboard."""

from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from chaosloop.dashboard.app import create_app

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def serve(
    runs_dir: Annotated[Path, typer.Option(help="Directory of run JSONL files")] = Path(
        "eval/runs"
    ),
    host: Annotated[str, typer.Option(help="Bind address; traces hold prompts and logs")] = (
        "127.0.0.1"
    ),
    port: int = 8700,
    thresholds: Annotated[Path, typer.Option(help="CI gate that tiles report against")] = Path(
        "golden/thresholds.yaml"
    ),
) -> None:
    """Serve run results, the reliability diagram and per-case traces."""
    uvicorn.run(create_app(runs_dir, thresholds), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
