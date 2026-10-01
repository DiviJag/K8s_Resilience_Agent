from pathlib import Path

import pytest
from pydantic import ValidationError

from chaosloop.schemas.diagnosis import Diagnosis, Fix
from chaosloop.schemas.golden_case import GoldenCase, GoldenCaseError, load_golden_set
from chaosloop.schemas.taxonomy import RECORDED_ONLY, SYMPTOM_TO_CATEGORIES, Category
from chaosloop.schemas.trigger import Trigger
from tests.conftest import write_case

VALID_DIAGNOSIS = {
    "category": "registry_auth_missing",
    "root_cause": "no imagePullSecrets",
    "evidence": ["pull access denied"],
    "fix": {"summary": "add imagePullSecrets", "patch": "spec.x: y"},
    "confidence": 0.91,
}


def test_diagnosis_round_trip():
    diagnosis = Diagnosis.model_validate(VALID_DIAGNOSIS)
    assert diagnosis.category is Category.REGISTRY_AUTH_MISSING
    assert Diagnosis.model_validate_json(diagnosis.model_dump_json()) == diagnosis


@pytest.mark.parametrize(
    "mutation",
    [
        {"category": "CrashLoopBackOff"},  # a symptom is not a category
        {"category": "totally_made_up"},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"extra_field": "hallucinated"},
        {"fix": {"summary": "s", "patch": "p", "unexpected": 1}},
    ],
)
def test_malformed_diagnosis_rejected(mutation):
    payload = {**VALID_DIAGNOSIS, **mutation}
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(payload)


def test_missing_required_field_rejected():
    payload = dict(VALID_DIAGNOSIS)
    del payload["fix"]
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(payload)


def test_fix_requires_both_fields():
    with pytest.raises(ValidationError):
        Fix.model_validate({"summary": "only a summary"})


def test_taxonomy_covers_every_category():
    mapped = set().union(*SYMPTOM_TO_CATEGORIES.values())
    assert mapped == set(Category)
    assert RECORDED_ONLY <= set(Category)


def test_trigger_render_includes_identifiers():
    rendered = Trigger(
        alertname="PodImagePullBackOff", service="payment", namespace="prod", pod="payment-1"
    ).render()
    assert "PodImagePullBackOff" in rendered
    assert "payment-1" in rendered


def test_evidence_keys_must_be_tool_names(tmp_path: Path):
    path = write_case(
        tmp_path / "test",
        "bad-evidence-key",
        "no_fault",
        evidence={"get_pod_status": {}, "kubectl_describe": "oops"},
    )
    with pytest.raises(GoldenCaseError, match="kubectl_describe"):
        load_golden_set(tmp_path, "test")
    assert path.exists()


def test_split_must_match_directory(tmp_path: Path):
    write_case(tmp_path / "test", "wrong-split", "no_fault", split="dev")
    with pytest.raises(GoldenCaseError, match="split"):
        load_golden_set(tmp_path, "test")


def test_repo_golden_set_is_valid(golden_root: Path):
    cases = load_golden_set(golden_root)
    assert cases, "expected hand-authored golden cases in golden/"

    for case in cases:
        assert isinstance(case, GoldenCase)
        assert case.expected.fix_check.path

    dev = {c.case_id for c in cases if c.split == "dev"}
    test = {c.case_id for c in cases if c.split == "test"}
    assert dev and test
    assert not dev & test, "a case id appears in both splits"


def test_golden_test_split_has_a_negative_control(golden_root: Path):
    """Without a no_fault case the false-positive rate is undefined (HLD §7.3)."""
    categories = {c.expected.category for c in load_golden_set(golden_root, "test")}
    assert Category.NO_FAULT in categories
