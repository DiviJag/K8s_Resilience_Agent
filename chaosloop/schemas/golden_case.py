"""Golden case format and loader (HLD §6.1).

Cases are validated at load time so a malformed case fails fast with a file path, rather
than surfacing later as a mysterious grading discrepancy.
"""

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from chaosloop.schemas.taxonomy import Category
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.interface import TOOL_NAMES

Split = Literal["dev", "test"]


class CaseMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: Literal["manual", "chaos", "prod"]
    k8s_version: str
    runtime: str
    recorded: date


class FixCheck(BaseModel):
    """Structural check on the proposed patch (HLD §7.1).

    `any_of` lists further paths that are equally correct, for faults with more than one
    legitimate fix - an unbound claim can be fixed on the claim or on the pod's volume, an
    exhausted quota by raising the quota or by lowering replicas.
    """

    path: str
    any_of: list[str] = []
    must_exist: bool = True
    equals: Any | None = None

    @property
    def paths(self) -> list[str]:
        return [self.path, *self.any_of]


class Expected(BaseModel):
    category: Category
    root_cause: str
    fix_check: FixCheck


class GoldenCase(BaseModel):
    case_id: str
    split: Split
    trigger: Trigger
    evidence: dict[str, Any]
    expected: Expected
    metadata: CaseMetadata

    @field_validator("evidence")
    @classmethod
    def _known_tools_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value) - set(TOOL_NAMES))
        if unknown:
            raise ValueError(
                f"evidence keys must be tool names; unknown: {', '.join(unknown)}. "
                f"Known tools: {', '.join(TOOL_NAMES)}"
            )
        return value


class GoldenCaseError(ValueError):
    pass


def load_golden_case(path: Path) -> GoldenCase:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return GoldenCase.model_validate(raw)
    except Exception as exc:
        raise GoldenCaseError(f"{path}: {exc}") from exc


def load_golden_set(root: Path, split: Split | None = None) -> list[GoldenCase]:
    search_dirs = [root / split] if split else [root / "dev", root / "test"]
    cases: list[GoldenCase] = []
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            case = load_golden_case(path)
            expected_split = directory.name
            if case.split != expected_split:
                raise GoldenCaseError(
                    f"{path}: split is {case.split!r} but the file lives in {expected_split}/"
                )
            cases.append(case)
    return cases
