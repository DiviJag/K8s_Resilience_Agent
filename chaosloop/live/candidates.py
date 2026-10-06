"""The flywheel's candidate queue (HLD §6.5).

A live run that surprises us - a wrong answer, or evidence unlike anything in the golden set -
is recorded as a candidate case. Nothing reaches the benchmark without a person approving the
label: one mislabelled case silently corrupts every number computed from it.
"""

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from chaosloop.memory.promote import extract_signals
from chaosloop.memory.retriever import tokenize
from chaosloop.schemas.golden_case import GoldenCase, GoldenCaseError, Split, load_golden_set

NOVELTY_THRESHOLD = 0.5
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def _tokens(evidence: dict[str, Any]) -> set[str]:
    return set(tokenize(" ".join(extract_signals(evidence))))


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Jaccard overlap of the distinguishing-signal vocabulary of two evidence sets."""
    left, right = _tokens(a), _tokens(b)
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def surprise(case: GoldenCase, agent_correct: bool, golden: list[GoldenCase]) -> str | None:
    """Why this live run deserves a human's attention, or None if it taught us nothing."""
    if not agent_correct:
        return "the agent got it wrong"
    peers = [g for g in golden if g.expected.category == case.expected.category]
    if not peers:
        return "first case of this category"
    closest = max(similarity(case.evidence, g.evidence) for g in peers)
    if closest < NOVELTY_THRESHOLD:
        return f"evidence unlike existing cases (closest overlap {closest:.2f})"
    return None


def _annotate(case: GoldenCase, **fields: str) -> None:
    # Metadata allows extra keys; setting them as attributes records them on the model.
    for key, value in fields.items():
        setattr(case.metadata, key, value)


class CandidateQueue:
    def __init__(self, golden_dir: Path) -> None:
        self.golden_dir = golden_dir
        self.root = golden_dir / "candidates"

    def _path(self, case_id: str) -> Path:
        if not _SAFE_ID.match(case_id):
            raise GoldenCaseError(f"unsafe case id {case_id!r}")
        return self.root / f"{case_id}.yaml"

    def propose(self, case: GoldenCase, reason: str) -> Path:
        case = case.model_copy(deep=True)
        _annotate(case, candidate_reason=reason, proposed_at=datetime.now(UTC).isoformat())
        path = self._path(case.case_id)
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(case.model_dump(mode="json"), sort_keys=False),
                        encoding="utf-8")
        return path

    def pending(self) -> list[GoldenCase]:
        if not self.root.is_dir():
            return []
        return [
            GoldenCase.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
            for p in sorted(self.root.glob("*.yaml"))
        ]

    def get(self, case_id: str) -> GoldenCase:
        path = self._path(case_id)
        if not path.is_file():
            raise GoldenCaseError(f"no candidate {case_id!r}")
        return GoldenCase.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def approve(self, case_id: str, split: Split, reviewer: str) -> Path:
        case = self.get(case_id)
        if any(c.case_id == case_id for c in load_golden_set(self.golden_dir)):
            raise GoldenCaseError(f"{case_id} is already in the golden set")
        approved = case.model_copy(update={"split": split}, deep=True)
        _annotate(approved, reviewed_by=reviewer, reviewed_at=date.today().isoformat())
        GoldenCase.model_validate(approved.model_dump(mode="json"))
        target = self.golden_dir / split / f"{case_id}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(approved.model_dump(mode="json"), sort_keys=False),
                          encoding="utf-8")
        self._path(case_id).unlink()
        return target

    def reject(self, case_id: str, reviewer: str, reason: str) -> Path:
        case = self.get(case_id)
        _annotate(case, rejected_by=reviewer, rejection_reason=reason)
        target = self.root / "rejected" / f"{case_id}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(case.model_dump(mode="json"), sort_keys=False),
                          encoding="utf-8")
        self._path(case_id).unlink()
        return target
