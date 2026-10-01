import pytest

from chaosloop.eval.grader import find_path, grade_case, grade_false_positive, grade_fix
from chaosloop.schemas.diagnosis import Diagnosis, Fix
from chaosloop.schemas.golden_case import Expected, FixCheck
from chaosloop.schemas.taxonomy import Category

PULL_SECRET_PATH = "spec.template.spec.imagePullSecrets"


def make_diagnosis(category: Category, patch: str = "", confidence: float = 0.8) -> Diagnosis:
    return Diagnosis(
        category=category,
        root_cause="rc",
        evidence=["e"],
        fix=Fix(summary="s", patch=patch),
        confidence=confidence,
    )


def expected(category: Category, path: str = PULL_SECRET_PATH, must_exist: bool = True) -> Expected:
    return Expected(
        category=category,
        root_cause="rc",
        fix_check=FixCheck(path=path, must_exist=must_exist),
    )


@pytest.mark.parametrize(
    "patch, found",
    [
        ("spec.template.spec.imagePullSecrets: [{name: regcred}]", True),
        (
            "spec:\n  template:\n    spec:\n      imagePullSecrets:\n        - name: regcred",
            True,
        ),
        ("imagePullSecrets: [{name: regcred}]", True),
        ("kubectl patch deploy payment -p '{\"spec\":{...imagePullSecrets...}}'", True),
        ("spec.template.spec.containers.image: payment:v1.8.3", False),
        ("", False),
        ("   ", False),
    ],
)
def test_find_path_handles_patch_shapes(patch: str, found: bool):
    assert find_path(patch, PULL_SECRET_PATH)[0] is found


@pytest.mark.parametrize(
    "patch",
    [
        "spec.template.spec.containers[0].env: [{name: DATABASE_URL, value: postgres://db}]",
        "spec:\n  template:\n    spec:\n      containers:\n        - name: billing\n"
        "          env:\n            - name: DATABASE_URL\n              value: postgres://db",
        '[{"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"X"}}]',
        "env:\n  - name: DATABASE_URL\n    value: postgres://db",
    ],
)
def test_find_path_ignores_patch_notation(patch: str):
    """An index or a JSON pointer must not make a correct fix read as a wrong one."""
    assert find_path(patch, "spec.template.spec.containers.env")[0]


def test_find_path_returns_value_for_equals_checks():
    found, value = find_path("spec.replicas: 3", "spec.replicas")
    assert found and value == 3


def test_grade_fix_must_exist():
    check = FixCheck(path=PULL_SECRET_PATH, must_exist=True)
    assert grade_fix(make_diagnosis(Category.REGISTRY_AUTH_MISSING, "imagePullSecrets: []"), check)
    assert not grade_fix(make_diagnosis(Category.IMAGE_TAG_NOT_FOUND, "image: a:v2"), check)


def test_grade_fix_must_not_exist():
    """registry_unreachable is fixed on the node, not by editing the deployment (HLD §8.1)."""
    check = FixCheck(path="spec.template.spec.containers.image", must_exist=False)
    node_fix = make_diagnosis(Category.REGISTRY_UNREACHABLE, "Fix node DNS resolution")
    assert grade_fix(node_fix, check)
    assert not grade_fix(
        make_diagnosis(
            Category.REGISTRY_UNREACHABLE, "spec.template.spec.containers.image: app:v2"
        ),
        check,
    )


def test_grade_fix_equals():
    assert grade_fix(
        make_diagnosis(Category.INSUFFICIENT_RESOURCES, "spec.replicas: 3"),
        FixCheck(path="spec.replicas", equals=3),
    )
    assert not grade_fix(
        make_diagnosis(Category.INSUFFICIENT_RESOURCES, "spec.replicas: 5"),
        FixCheck(path="spec.replicas", equals=3),
    )


def test_no_fault_abstention_is_not_a_false_positive():
    assert not grade_false_positive(Category.NO_FAULT, Category.NO_FAULT)
    assert not grade_false_positive(Category.NO_FAULT, Category.UNKNOWN)
    assert grade_false_positive(Category.NO_FAULT, Category.IMAGE_TAG_NOT_FOUND)
    assert not grade_false_positive(Category.IMAGE_TAG_NOT_FOUND, Category.MEMORY_LEAK)


def test_grade_case_correct():
    result = grade_case(
        make_diagnosis(Category.REGISTRY_AUTH_MISSING, f"{PULL_SECRET_PATH}: [{{name: regcred}}]"),
        expected(Category.REGISTRY_AUTH_MISSING),
    )
    assert result.category_correct and result.fix_correct
    assert not result.malformed and not result.is_false_positive


def test_grade_case_wrong_category_right_shape():
    result = grade_case(
        make_diagnosis(Category.IMAGE_TAG_NOT_FOUND, f"{PULL_SECRET_PATH}: [{{name: regcred}}]"),
        expected(Category.REGISTRY_AUTH_MISSING),
    )
    assert not result.category_correct
    assert result.fix_correct


def test_grade_case_symptom_answer_is_wrong():
    """An agent answering with the symptom family rather than the cause scores zero."""
    result = grade_case(
        make_diagnosis(Category.UNKNOWN, ""), expected(Category.REGISTRY_AUTH_MISSING)
    )
    assert not result.category_correct and not result.fix_correct


def test_malformed_response_fails_every_axis():
    result = grade_case(None, expected(Category.REGISTRY_AUTH_MISSING))
    assert result.malformed
    assert not result.category_correct
    assert not result.fix_correct
    assert not result.is_false_positive


def test_negative_control_false_positive_recorded():
    result = grade_case(
        make_diagnosis(Category.IMAGE_TAG_NOT_FOUND, "image: a:v2"),
        expected(Category.NO_FAULT, path="*", must_exist=False),
    )
    assert result.is_false_positive
    assert not result.category_correct
    assert not result.fix_correct


def test_no_fault_requires_an_empty_patch():
    check = FixCheck(path="*", must_exist=False)
    assert grade_fix(make_diagnosis(Category.NO_FAULT, ""), check)
    assert not grade_fix(make_diagnosis(Category.NO_FAULT, "restart the deployment"), check)
