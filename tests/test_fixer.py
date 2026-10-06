import pytest

from chaosloop.live.catalog import load_catalog
from chaosloop.live.fixer import apply_fix, parse_patch, policy_violations
from chaosloop.live.gateway import ObjectRef
from chaosloop.schemas.diagnosis import Fix
from tests.conftest import REPO_ROOT
from tests.fakes import NS, deployment, lab_cluster

CATALOG = load_catalog(REPO_ROOT / "lab" / "experiments.yaml")
BILLING = ObjectRef("apps/v1", "Deployment", "billing-service", NS)
CART_SVC = ObjectRef("v1", "Service", "cart-service", NS)


def test_reference_fix_applies_after_a_dry_run():
    cluster = lab_cluster()
    outcome = apply_fix(
        Fix(summary="s", patch="spec.template.spec.containers[0].env: [{name: X, value: y}]",
            target="Deployment/billing-service"),
        CATALOG.get("billing-missing-env-var"),
        cluster,
    )
    assert outcome.status == "applied" and outcome.target == "Deployment/billing-service"
    assert [w[2] for w in cluster.writes if w[1] == BILLING] == [True, False]  # dry run first
    env = cluster.get(BILLING)["spec"]["template"]["spec"]["containers"][0]["env"]
    assert [e["name"] for e in env] == ["DATABASE_URL", "X"]


def test_target_names_the_upstream_object_in_a_cascade():
    """The alert is on checkout-frontend; the broken object is cart-service's Service."""
    cluster = lab_cluster()
    cluster.objects[CART_SVC]["spec"]["selector"] = {"app": "cart-service-v2"}
    outcome = apply_fix(
        Fix(summary="s", patch="spec.selector: {app: cart-service}"),
        CATALOG.get("cart-selector-mismatch"),
        cluster,
    )
    assert outcome.status == "applied" and outcome.target == "Service/cart-service"
    assert cluster.get(CART_SVC)["spec"]["selector"] == {"app": "cart-service"}


def test_empty_patch_is_not_applicable_not_a_failure():
    outcome = apply_fix(Fix(summary="fix DNS", patch=""), CATALOG.get("frontend-network-partition"),
                        lab_cluster())
    assert outcome.status == "not_applicable"


def test_prompt_injected_scale_to_zero_is_refused():
    cluster = lab_cluster()
    outcome = apply_fix(Fix(summary="s", patch="spec.replicas: 0",
                            target="Deployment/billing-service"),
                        CATALOG.get("billing-missing-env-var"), cluster)
    assert outcome.status == "rejected" and "zero" in (outcome.reason or "")
    assert not cluster.writes  # nothing reached the cluster, not even a dry run


def test_json_patch_form_cannot_dodge_the_policy():
    outcome = apply_fix(
        Fix(summary="s", patch='[{"op": "replace", "path": "/spec/replicas", "value": 0}]',
            target="Deployment/billing-service"),
        CATALOG.get("billing-missing-env-var"),
        lab_cluster(),
    )
    assert outcome.status == "rejected" and "zero" in (outcome.reason or "")


@pytest.mark.parametrize(
    "patch, reason",
    [
        ('spec.template.spec.containers[0].image: "ghcr.io/other/cart:v1"', "registry"),
        ("spec.template.spec.hostNetwork: true", "hostNetwork"),
        ("spec.template.spec.containers[0].securityContext: {privileged: true}", "privilege"),
        ("spec.template.spec.serviceAccountName: admin", "service account"),
        ("spec.template.spec.containers: [{name: miner, image: x}]", "containers"),
        ("spec.template.spec.volumes: [{name: h, hostPath: {path: /}}]", "hostPath"),
    ],
)
def test_policy_refuses_dangerous_or_workaround_fixes(patch: str, reason: str):
    outcome = apply_fix(Fix(summary="s", patch=patch, target="Deployment/billing-service"),
                        CATALOG.get("billing-missing-env-var"), lab_cluster())
    assert outcome.status == "rejected"
    assert reason in (outcome.reason or "")


def test_a_patch_that_changes_nothing_is_refused():
    outcome = apply_fix(
        Fix(summary="s", patch="spec.template.spec.containers[0].env: [{name: DATABASE_URL, value: postgres://db}]",
            target="Deployment/billing-service"),
        CATALOG.get("billing-missing-env-var"),
        lab_cluster(),
    )
    assert outcome.status == "rejected" and "nothing" in (outcome.reason or "")


def test_dry_run_rejection_stops_the_apply():
    cluster = lab_cluster()
    cluster.validators.append(
        lambda new, old: "spec.selector is immutable"
        if old and new.get("kind") == "Deployment"
        and new["spec"]["selector"] != old["spec"]["selector"] else None
    )
    outcome = apply_fix(Fix(summary="s", patch="spec.selector: {matchLabels: {app: other}}",
                            target="Deployment/billing-service"),
                        CATALOG.get("billing-missing-env-var"), cluster)
    assert outcome.status == "rejected" and "dry-run" in (outcome.reason or "")
    assert [w for w in cluster.writes if not w[2]] == []  # no real write happened


def test_fixes_may_not_touch_nodes_or_secrets():
    for target in ("Node/chaosloop-worker", "Secret/regcred"):
        outcome = apply_fix(Fix(summary="s", patch="metadata.labels: {a: b}", target=target),
                            CATALOG.get("billing-missing-env-var"), lab_cluster())
        assert outcome.status == "rejected" and "may not modify" in (outcome.reason or "")


def test_unreadable_patches_are_rejected_with_a_reason():
    for text in ("Restart the pods", "{{{", "- just\n- a\n- list"):
        with pytest.raises(ValueError):
            parse_patch(text)


def test_registry_comparison_reads_implicit_docker_hub():
    old = deployment("a", image="busybox:1.36")
    new = deployment("a", image="docker.io/library/busybox:1.37")
    assert policy_violations(old, new) == []
