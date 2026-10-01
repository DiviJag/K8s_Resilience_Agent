"""Tier A harness: offline replay of the golden set (HLD §6.2)."""

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from chaosloop.agent.diagnostician import SEARCH_MEMORY, Budget, Diagnostician, DiagnosticTrace
from chaosloop.agent.llm_client import LLMClient
from chaosloop.agent.prompts import PROMPT_VERSION
from chaosloop.eval.calibration import (
    IsotonicCalibrator,
    brier_score,
    expected_calibration_error,
    mean_gap,
)
from chaosloop.eval.grader import grade_case
from chaosloop.eval.judge import RootCauseJudge
from chaosloop.memory.retriever import Memory
from chaosloop.memory.store import assert_disjoint_from
from chaosloop.schemas.diagnosis import Diagnosis
from chaosloop.schemas.eval_record import EvalRecord
from chaosloop.schemas.golden_case import GoldenCase, Split, load_golden_set
from chaosloop.schemas.taxonomy import Category
from chaosloop.tools.replay_backend import ReplayBackend

LLM_ERROR_PREFIX = "llm_error"

CONFIDENCE_SOURCES = {
    "system": "confidence",
    "self_reported": "self_reported_confidence",
    "consistency": "consistency_confidence",
    "calibrated": "calibrated_confidence",
}


class CalibrationSummary(BaseModel):
    n: int
    ece: float
    brier: float
    gap: float


class Metrics(BaseModel):
    n_cases: int
    n_repeats: int
    n_runs: int
    n_scored: int
    n_errors: int
    category_accuracy: float
    fix_accuracy: float
    false_positive_rate: float
    malformed_rate: float
    error_rate: float
    mean_cost_usd: float
    mean_latency_s: float
    total_cost_usd: float
    root_cause_score: float | None = None
    memory_usage_rate: float | None = None
    calibration: dict[str, CalibrationSummary] = {}
    by_category: dict[str, dict[str, float | None]] = {}


def is_provider_error(record: EvalRecord) -> bool:
    """A run that never reached the model is not a measurement of the agent.

    Scoring a provider outage as a wrong answer would blame the agent for infrastructure, so
    these runs are reported separately and excluded from accuracy (HLD §12).
    """
    return bool(record.error and record.error.startswith(LLM_ERROR_PREFIX))


class Thresholds(BaseModel):
    category_accuracy_min: float = 0.0
    fix_accuracy_min: float = 0.0
    false_positive_rate_max: float = 1.0
    malformed_rate_max: float = 1.0
    error_rate_max: float = 0.1
    ece_max: float = 1.0


def load_thresholds(path: Path) -> Thresholds:
    if not path.is_file():
        return Thresholds()
    return Thresholds.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def check_thresholds(metrics: Metrics, thresholds: Thresholds) -> list[str]:
    violations = []
    if metrics.n_runs and not metrics.n_scored:
        # Otherwise a total provider outage would pass the gate on vacuous 0-of-0 metrics.
        return [f"no runs reached the model ({metrics.n_errors} provider errors)"]
    if metrics.error_rate > thresholds.error_rate_max:
        violations.append(
            f"error_rate {metrics.error_rate:.3f} > {thresholds.error_rate_max:.3f}"
        )
    if metrics.category_accuracy < thresholds.category_accuracy_min:
        violations.append(
            f"category_accuracy {metrics.category_accuracy:.3f} "
            f"< {thresholds.category_accuracy_min:.3f}"
        )
    if metrics.fix_accuracy < thresholds.fix_accuracy_min:
        violations.append(
            f"fix_accuracy {metrics.fix_accuracy:.3f} < {thresholds.fix_accuracy_min:.3f}"
        )
    if metrics.false_positive_rate > thresholds.false_positive_rate_max:
        violations.append(
            f"false_positive_rate {metrics.false_positive_rate:.3f} "
            f"> {thresholds.false_positive_rate_max:.3f}"
        )
    if metrics.malformed_rate > thresholds.malformed_rate_max:
        violations.append(
            f"malformed_rate {metrics.malformed_rate:.3f} > {thresholds.malformed_rate_max:.3f}"
        )
    system = metrics.calibration.get("system")
    if system is not None and system.ece > thresholds.ece_max:
        violations.append(f"ece {system.ece:.3f} > {thresholds.ece_max:.3f}")
    return violations


def golden_version(golden_dir: Path) -> str:
    """Content hash of the golden set.

    Hashing contents rather than reading a git SHA keeps eval records reproducible on a repo
    with uncommitted or unstaged case edits, which is the normal state while authoring cases.
    """
    digest = hashlib.sha256()
    for path in sorted(golden_dir.rglob("*.yaml")):
        digest.update(path.relative_to(golden_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()[:12]}"


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


class TierAHarness:
    def __init__(
        self,
        golden_dir: Path,
        model: str,
        client: LLMClient,
        split: Split = "test",
        repeats: int = 3,
        prompt_version: str = PROMPT_VERSION,
        budget: Budget | None = None,
        run_id: str | None = None,
        case_filter: str | None = None,
        samples: int = 0,
        memory: Memory | None = None,
        judge: RootCauseJudge | None = None,
        calibrator: IsotonicCalibrator | None = None,
        trace_dir: Path | None = None,
    ) -> None:
        self.golden_dir = golden_dir
        self.model = model
        self.client = client
        self.split = split
        self.repeats = repeats
        self.prompt_version = prompt_version
        self.budget = budget
        self.run_id = run_id or new_run_id()
        self.case_filter = case_filter
        self.samples = samples
        self.memory = memory
        self.judge = judge
        self.calibrator = calibrator
        self.trace_dir = trace_dir
        self.golden_version = golden_version(golden_dir)

        if memory is not None:
            # Checked whichever split is being run: memory must never hold a test case, and a
            # dev run that silently tolerated one would be a test leak waiting to be reported.
            test_ids = {c.case_id for c in load_golden_set(golden_dir, "test")}
            assert_disjoint_from(memory.store, test_ids)

    def cases(self) -> list[GoldenCase]:
        cases = load_golden_set(self.golden_dir, self.split)
        if self.case_filter:
            cases = [c for c in cases if c.case_id == self.case_filter]
        return cases

    def run(self, on_record: Callable[[EvalRecord], None] | None = None) -> list[EvalRecord]:
        records: list[EvalRecord] = []
        for case in self.cases():
            for repeat in range(1, self.repeats + 1):
                record = self.run_once(case, repeat)
                records.append(record)
                if on_record:
                    on_record(record)
        return records

    def run_once(self, case: GoldenCase, repeat: int) -> EvalRecord:
        # Leave-one-out: a dev case promoted into memory must not retrieve itself.
        memory = self.memory.excluding({case.case_id}) if self.memory is not None else None
        agent = Diagnostician(
            backend=ReplayBackend(case),
            model=self.model,
            client=self.client,
            budget=self.budget,
            prompt_version=self.prompt_version,
            samples=self.samples,
            memory=memory,
        )
        diagnosis, trace = agent.diagnose(case.trigger)
        budget_exhausted = bool(trace.error and trace.error.startswith("budget_exceeded"))
        grade = grade_case(diagnosis, case.expected, budget_exhausted=budget_exhausted)

        # The budget fallback's 0.0 is a harness default, not the model's estimate; recording it
        # as self-reported would make "wrong at 0% confidence" look perfectly calibrated.
        self_reported = diagnosis.confidence if diagnosis and not budget_exhausted else None
        consistency = trace.consistency
        calibrated = (
            self.calibrator.predict(consistency)
            if self.calibrator is not None and consistency is not None
            else None
        )
        confidence = next(
            (c for c in (calibrated, consistency, self_reported) if c is not None), None
        )

        judged = (
            self.judge.grade(case.expected.root_cause, diagnosis.root_cause)
            if self.judge is not None and diagnosis is not None
            else None
        )

        record = EvalRecord(
            run_id=self.run_id,
            case_id=case.case_id,
            split=case.split,
            tier="A",
            repeat=repeat,
            model=self.model,
            prompt_version=self.prompt_version,
            memory_snapshot=self.memory.snapshot if self.memory is not None else None,
            golden_version=self.golden_version,
            expected_category=case.expected.category,
            agent_category=diagnosis.category if diagnosis else None,
            agent_root_cause=diagnosis.root_cause if diagnosis else None,
            agent_fix_patch=diagnosis.fix.patch if diagnosis else None,
            category_correct=grade.category_correct,
            fix_correct=grade.fix_correct,
            is_false_positive=grade.is_false_positive,
            malformed=grade.malformed,
            confidence=confidence,
            self_reported_confidence=self_reported,
            consistency_confidence=consistency,
            calibrated_confidence=calibrated,
            samples=trace.samples,
            root_cause_score=judged.verdict.score if judged and judged.verdict else None,
            judge_model=self.judge.model if judged is not None and self.judge else None,
            judge_rationale=(
                (judged.verdict.rationale if judged.verdict else judged.error) if judged else None
            ),
            judge_cost_usd=judged.cost_usd if judged else 0.0,
            tool_calls=trace.tool_calls,
            tokens=trace.tokens,
            cost_usd=trace.cost_usd,
            latency_s=trace.latency_s,
            error=trace.error,
        )
        if self.trace_dir is not None:
            write_trace(self.trace_dir, case, repeat, diagnosis, trace)
        return record


def trace_path(trace_dir: Path, case_id: str, repeat: int) -> Path:
    return trace_dir / f"{case_id}-r{repeat}.json"


def write_trace(
    trace_dir: Path,
    case: GoldenCase,
    repeat: int,
    diagnosis: Diagnosis | None,
    trace: DiagnosticTrace,
) -> Path:
    """Full per-step trace for the dashboard: every message, tool result and resample."""
    path = trace_path(trace_dir, case.case_id, repeat)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "case_id": case.case_id,
        "repeat": repeat,
        "trigger": case.trigger.model_dump(mode="json"),
        "expected": case.expected.model_dump(mode="json"),
        "diagnosis": diagnosis.model_dump(mode="json") if diagnosis else None,
        "raw_final_message": trace.raw_final_message,
        "samples": trace.samples,
        "sample_errors": trace.sample_errors,
        "messages": trace.messages,
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def summarize(records: list[EvalRecord]) -> Metrics:
    empty = Metrics(
        n_cases=0,
        n_repeats=0,
        n_runs=0,
        n_scored=0,
        n_errors=0,
        category_accuracy=0.0,
        fix_accuracy=0.0,
        false_positive_rate=0.0,
        malformed_rate=0.0,
        error_rate=0.0,
        mean_cost_usd=0.0,
        mean_latency_s=0.0,
        total_cost_usd=0.0,
    )
    if not records:
        return empty

    total = len(records)
    scored = [r for r in records if not is_provider_error(r)]
    negatives = [r for r in scored if r.expected_category is Category.NO_FAULT]
    judged = [r.root_cause_score for r in scored if r.root_cause_score is not None]
    with_memory = [r for r in scored if r.memory_snapshot is not None]

    by_category: dict[str, dict[str, float | None]] = {}
    for category in sorted({r.expected_category.value for r in scored}):
        rows = [r for r in scored if r.expected_category.value == category]
        confident = [r for r in rows if r.confidence is not None]
        scores = [r.root_cause_score for r in rows if r.root_cause_score is not None]
        by_category[category] = {
            "runs": float(len(rows)),
            "category_accuracy": _ratio([r.category_correct for r in rows]),
            "fix_accuracy": _ratio([r.fix_correct for r in rows]),
            "mean_confidence": (
                sum(r.confidence or 0.0 for r in confident) / len(confident) if confident else None
            ),
            "calib_gap": (
                mean_gap([r.confidence or 0.0 for r in confident],
                         [r.category_correct for r in confident])
                if confident
                else None
            ),
            "root_cause_score": sum(scores) / len(scores) if scores else None,
        }

    return empty.model_copy(
        update={
            "n_cases": len({r.case_id for r in records}),
            "n_repeats": max(r.repeat for r in records),
            "n_runs": total,
            "n_scored": len(scored),
            "n_errors": total - len(scored),
            "category_accuracy": _ratio([r.category_correct for r in scored]),
            "fix_accuracy": _ratio([r.fix_correct for r in scored]),
            "false_positive_rate": _ratio([r.is_false_positive for r in negatives]),
            "malformed_rate": _ratio([r.malformed for r in scored]),
            "error_rate": (total - len(scored)) / total,
            "mean_cost_usd": sum(r.cost_usd for r in records) / total,
            "mean_latency_s": sum(r.latency_s for r in records) / total,
            "total_cost_usd": sum(r.cost_usd + r.judge_cost_usd for r in records),
            "root_cause_score": sum(judged) / len(judged) if judged else None,
            "memory_usage_rate": (
                _ratio([any(c.tool == SEARCH_MEMORY for c in r.tool_calls) for r in with_memory])
                if with_memory
                else None
            ),
            "calibration": calibration_summaries(scored),
            "by_category": by_category,
        }
    )


def calibration_summaries(records: list[EvalRecord]) -> dict[str, CalibrationSummary]:
    summaries: dict[str, CalibrationSummary] = {}
    for name, field in CONFIDENCE_SOURCES.items():
        pairs = [
            (getattr(r, field), r.category_correct)
            for r in records
            if getattr(r, field) is not None
        ]
        if not pairs:
            continue
        confidences = [float(c) for c, _ in pairs]
        correct = [o for _, o in pairs]
        summaries[name] = CalibrationSummary(
            n=len(pairs),
            ece=expected_calibration_error(confidences, correct),
            brier=brier_score(confidences, correct),
            gap=mean_gap(confidences, correct),
        )
    return summaries


def apply_calibrator(
    records: list[EvalRecord], calibrator: IsotonicCalibrator
) -> list[EvalRecord]:
    """Re-score a stored run with a calibrator, without calling the model again."""
    updated = []
    for record in records:
        if record.consistency_confidence is None:
            updated.append(record)
            continue
        calibrated = calibrator.predict(record.consistency_confidence)
        updated.append(
            record.model_copy(
                update={"calibrated_confidence": calibrated, "confidence": calibrated}
            )
        )
    return updated


def _ratio(flags: list[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0


def write_records(records: list[EvalRecord], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.model_dump(mode="json")) + "\n")
    return path


def read_records(path: Path) -> list[EvalRecord]:
    return [
        EvalRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
