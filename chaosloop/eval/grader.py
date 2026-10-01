"""Deterministic grading (HLD §7.1).

Category is an exact match against the taxonomy; the fix is a structural check on the
proposed patch. Root-cause text grading is an LLM-as-judge job and lives in judge.py.
"""

import re
from typing import Any

import yaml
from pydantic import BaseModel

from chaosloop.schemas.diagnosis import Diagnosis
from chaosloop.schemas.golden_case import Expected, FixCheck
from chaosloop.schemas.taxonomy import ABSTENTION, Category


class GradeResult(BaseModel):
    category_correct: bool
    fix_correct: bool
    is_false_positive: bool
    malformed: bool


def grade_category(expected: Category, actual: Category | None) -> bool:
    return actual is not None and actual == expected


def grade_false_positive(expected: Category, actual: Category | None) -> bool:
    """True when the agent named a fault on a healthy cluster (HLD §7.3)."""
    return expected is Category.NO_FAULT and actual is not None and actual not in ABSTENTION


def grade_fix(diagnosis: Diagnosis | None, fix_check: FixCheck) -> bool:
    patch = diagnosis.fix.patch if diagnosis else ""
    found, value = find_path(patch, fix_check.path)
    if not fix_check.must_exist:
        return not found
    if not found:
        return False
    if fix_check.equals is not None:
        return value == fix_check.equals
    return True


def grade_case(diagnosis: Diagnosis | None, expected: Expected) -> GradeResult:
    actual = diagnosis.category if diagnosis else None
    return GradeResult(
        category_correct=grade_category(expected.category, actual),
        fix_correct=grade_fix(diagnosis, expected.fix_check),
        is_false_positive=grade_false_positive(expected.category, actual),
        malformed=diagnosis is None,
    )


def find_path(patch: str, path: str) -> tuple[bool, Any]:
    """Look for a spec path in a proposed patch.

    Patches arrive either as a nested YAML fragment or as a single dotted key (the form the
    HLD's examples use), so both shapes are flattened to dotted paths before matching. A
    patch that is not YAML at all (a kubectl command line) falls back to a textual check.

    The path "*" matches any non-empty patch, which is how a negative control asserts that
    the agent proposed no change at all.
    """
    if not patch or not patch.strip():
        return False, None
    if path == "*":
        return True, patch

    try:
        parsed = yaml.safe_load(patch)
    except yaml.YAMLError:
        parsed = None

    target = _normalize(path)
    if _is_json_patch(parsed):
        for operation in parsed:
            if _normalize(operation["path"]).endswith(target):
                return True, operation.get("value")
        return False, None

    if isinstance(parsed, (dict, list)):
        for candidate, value in _flatten(parsed).items():
            if (
                candidate == target
                or candidate.endswith(f".{target}")
                or target.endswith(f".{candidate}")
            ):
                return True, value
        return False, None

    last_segment = target.split(".")[-1]
    if target in _normalize(patch) or re.search(rf"\b{re.escape(last_segment)}\b", patch):
        return True, None
    return False, None


_INDEX = re.compile(r"\[\s*\d+\s*\]")


def _is_json_patch(parsed: Any) -> bool:
    """RFC-6902 keeps the path in a value, not a key, so it needs its own read."""
    return isinstance(parsed, list) and bool(parsed) and all(
        isinstance(item, dict) and "op" in item and isinstance(item.get("path"), str)
        for item in parsed
    )


def _normalize(path: str) -> str:
    """Make patch dialects comparable.

    A correct fix is just as likely to be written `containers[0].env` or as a JSON pointer
    `/spec/template/spec/containers/0/env/-`; the structural check cares which field is
    touched, not which notation was used, so indices and separators are flattened away first.
    """
    path = _INDEX.sub("", path)
    path = re.sub(r"/+", ".", path)
    path = re.sub(r"\.(?:\d+|-)(?=\.|$)", "", path)
    return path.strip(".")


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    paths: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{_normalize(str(key))}" if prefix else _normalize(str(key))
            paths[path] = value
            paths.update(_flatten(value, path))
    elif isinstance(obj, list):
        for item in obj:
            paths.update(_flatten(item, prefix))
    return paths
