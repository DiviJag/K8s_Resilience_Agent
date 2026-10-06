"""One row per case run (HLD §7.2)."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from chaosloop.schemas.taxonomy import Category


class ToolCallRecord(BaseModel):
    step: int
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class EvalRecord(BaseModel):
    run_id: str
    case_id: str
    split: Literal["dev", "test"] | None = None
    tier: Literal["A", "B"] = "A"
    repeat: int

    model: str
    prompt_version: str
    memory_snapshot: str | None = None
    golden_version: str

    expected_category: Category
    agent_category: Category | None
    # Kept so a failed fix_check can be triaged from the run file instead of by paying for
    # another run - the usual question while authoring cases is "what did it actually propose?"
    agent_root_cause: str | None = None
    agent_fix_patch: str | None = None
    category_correct: bool
    fix_correct: bool
    is_false_positive: bool
    malformed: bool

    # The confidence the system reports: calibrated if a calibrator was applied, else raw
    # self-consistency, else the model's own number. The components are kept so calibration
    # can be refit, and self-report compared against consistency, without re-running anything.
    confidence: float | None
    self_reported_confidence: float | None = None
    consistency_confidence: float | None = None
    calibrated_confidence: float | None = None
    samples: list[str | None] = Field(default_factory=list)

    root_cause_score: float | None = None
    judge_model: str | None = None
    judge_rationale: str | None = None
    judge_cost_usd: float = 0.0

    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    tokens: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str | None = None

    # Tier B only; unset for offline replay runs. Times are epoch seconds (HLD §7.2).
    experiment_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    aborted: str | None = None
    detected: bool | None = None
    trigger_alertname: str | None = None
    fix_applied: bool | None = None
    # None when there was nothing to verify: no patch proposed, or the fix lives outside the
    # workload's manifests. A rejected patch counts as a failed fix, not as "nothing to verify".
    fix_verified: bool | None = None
    fix_rejected: str | None = None
    t_inject: float | None = None
    t_detect: float | None = None
    t_diagnose: float | None = None
    t_recover: float | None = None
