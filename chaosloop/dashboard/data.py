"""Read eval runs and traces from disk for the dashboard.

Identifiers in URLs are never used to build file paths directly: a run must appear in the
directory listing and a case run must appear in that run's records before any file is opened,
so a crafted id cannot walk out of the runs directory.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from chaosloop.eval.harness import Metrics, read_records, summarize, trace_path
from chaosloop.schemas.eval_record import EvalRecord

_RUN_STAMP = re.compile(r"^(\d{8}T\d{6}Z)-")


class RunSummary(BaseModel):
    run_id: str
    started: datetime | None
    model: str
    splits: list[str]
    prompt_version: str
    golden_version: str
    memory_snapshot: str | None
    sampled: bool
    judged: bool
    metrics: Metrics

    @property
    def split_label(self) -> str:
        return "+".join(self.splits) or "-"


def _started(run_id: str) -> datetime | None:
    match = _RUN_STAMP.match(run_id)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def summarise_run(run_id: str, records: list[EvalRecord]) -> RunSummary:
    first = records[0]
    return RunSummary(
        run_id=run_id,
        started=_started(run_id),
        model=first.model,
        splits=sorted({r.split for r in records if r.split}),
        prompt_version=first.prompt_version,
        golden_version=first.golden_version,
        memory_snapshot=first.memory_snapshot,
        sampled=any(r.consistency_confidence is not None for r in records),
        judged=any(r.root_cause_score is not None for r in records),
        metrics=summarize(records),
    )


class RunStore:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir

    def _files(self) -> dict[str, Path]:
        if not self.runs_dir.is_dir():
            return {}
        return {path.stem: path for path in self.runs_dir.glob("*.jsonl")}

    def list_runs(self) -> list[RunSummary]:
        summaries = []
        for run_id, path in self._files().items():
            try:
                records = read_records(path)
            except ValueError:
                # A run file from an older record schema is skipped, not fatal to the page.
                continue
            if records:
                summaries.append(summarise_run(run_id, records))
        epoch = datetime.min.replace(tzinfo=UTC)
        return sorted(summaries, key=lambda s: s.started or epoch, reverse=True)

    def load_run(self, run_id: str) -> tuple[RunSummary, list[EvalRecord]] | None:
        path = self._files().get(run_id)
        if path is None:
            return None
        records = read_records(path)
        if not records:
            return None
        return summarise_run(run_id, records), records

    def load_trace(self, run_id: str, case_id: str, repeat: int) -> dict[str, Any] | None:
        loaded = self.load_run(run_id)
        if loaded is None:
            return None
        _, records = loaded
        if not any(r.case_id == case_id and r.repeat == repeat for r in records):
            return None
        path = trace_path(self.runs_dir / run_id / "traces", case_id, repeat)
        if not path.resolve().is_relative_to(self.runs_dir.resolve()) or not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
