from pathlib import Path

import pytest

from chaosloop.live.catalog import Experiment, load_catalog
from chaosloop.live.gateway import ObjectRef
from chaosloop.live.mutator import QUOTA_NAME, InjectionError, Mutator
from tests.conftest import REPO_ROOT
from tests.fakes import NS, lab_cluster

CATALOG = REPO_ROOT / "lab" / "experiments.yaml"
DEPLOY = ObjectRef("apps/v1", "Deployment", "billing-service", NS)


def experiment(**overrides) -> Experiment:
    base = {
        "id": "x",
        "category": "missing_env_var",
        "injector": "config",
        "service": "billing-service",
        "blast_radius": ["billing-service"],
        "mutations": [],
        "root_cause": "rc",
        "fix_check": {"path": "spec.template.spec.containers.env"},
        "reference_fix": {"summary": "s"},
    }
    return Experiment.model_validate({**base, **overrides})


# --- catalog ------------------------------------------------------------------------------


def test_repo_catalog_is_valid_and_covers_distinct_categories():
    catalog = load_catalog(CATALOG)
    assert len(catalog.experiments) >= 15
    assert catalog.version.startswith("cat:sha256:")
    for item in catalog.experiments:
        assert item.service in item.blast_radius
        if item.reference_fix.patch:
            assert item.reference_fix.target, f"{item.id} reference fix needs a target"


def test_parameters_only_take_allowed_values():
    catalog = load_catalog(CATALOG)
    latency = catalog.get("cart-high-latency")
    assert latency.resolve({"latency_ms": 2500}).mutations[0].manifest["spec"]["delay"][
        "latency"
    ] == "2500ms"
    with pytest.raises(ValueError, match="outside"):
        latency.resolve({"latency_ms": 99999})
    with pytest.raises(ValueError, match="no parameter"):
        latency.resolve({"replicas": 0})


def test_alerting_service_must_be_inside_the_blast_radius():
    with pytest.raises(ValueError, match="blast radius"):
        experiment(blast_radius=["cart-service"])


def test_undeclared_parameters_are_rejected(tmp_path: Path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "experiments:\n"
        "  - id: a\n    category: high_latency\n    injector: chaos\n    service: s\n"
        "    blast_radius: [s]\n    mutations: []\n    root_cause: 'about ${ghost}ms'\n"
        "    fix_check: {path: '*', must_exist: false}\n    reference_fix: {summary: s}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ghost"):
        load_catalog(path)


# --- mutator ------------------------------------------------------------------------------


def test_patch_then_rollback_restores_the_original(tmp_path: Path):
    cluster = lab_cluster()
    before = cluster.get(DEPLOY)
    mutator = Mutator(cluster, tmp_path)
    journal = mutator.inject(
        experiment(mutations=[{
            "action": "patch",
            "target": {"kind": "Deployment", "name": "billing-service"},
            "ops": [{"op": "add", "path": "/spec/template/spec/nodeSelector", "value": {"a": "b"}}],
        }]),
        "run-1",
    )
    assert cluster.get(DEPLOY)["spec"]["template"]["spec"]["nodeSelector"] == {"a": "b"}
    assert (tmp_path / "run-1.json").exists()

    assert mutator.rollback(journal) == []
    after = cluster.get(DEPLOY)
    # An *added* field is gone again - the case kubectl apply could not undo.
    assert "nodeSelector" not in after["spec"]["template"]["spec"]
    assert after["spec"] == before["spec"]
    assert not (tmp_path / "run-1.json").exists()


def test_two_patches_to_one_object_snapshot_the_original_once(tmp_path: Path):
    cluster = lab_cluster()
    original_env = cluster.get(DEPLOY)["spec"]["template"]["spec"]["containers"][0]["env"]
    mutator = Mutator(cluster, tmp_path)
    target = {"kind": "Deployment", "name": "billing-service"}
    journal = mutator.inject(
        experiment(mutations=[
            {"action": "patch", "target": target, "ops": [
                {"op": "replace", "path": "/spec/template/spec/containers/0/env", "value": []}
            ]},
            {"action": "restart", "target": target},
        ]),
        "run-2",
    )
    assert [e.action for e in journal.entries] == ["replaced"]
    patched = cluster.get(DEPLOY)["spec"]["template"]
    assert patched["spec"]["containers"][0]["env"] == []
    assert "chaosloop.io/restartedAt" in patched["metadata"]["annotations"]
    mutator.rollback(journal)
    assert cluster.get(DEPLOY)["spec"]["template"]["spec"]["containers"][0]["env"] == original_env


def test_taints_go_on_workers_only_and_come_off_by_key(tmp_path: Path):
    cluster = lab_cluster()
    mutator = Mutator(cluster, tmp_path)
    journal = mutator.inject(
        experiment(mutations=[{
            "action": "taint_workers",
            "taint": {"key": "chaosloop", "value": "experiment", "effect": "NoSchedule"},
        }]),
        "run-3",
    )
    taints = {
        r.name: [t["key"] for t in o["spec"]["taints"]]
        for r, o in cluster.objects.items() if r.kind == "Node"
    }
    assert taints["chaosloop-worker"] == ["chaosloop"]
    assert "chaosloop" not in taints["chaosloop-control-plane"]

    # Something else updates the node meanwhile; rollback must not revert it.
    worker = ObjectRef("v1", "Node", "chaosloop-worker")
    cluster.objects[worker]["metadata"]["labels"]["touched-by-kubelet"] = "yes"
    mutator.rollback(journal)
    restored = cluster.get(worker)
    assert restored["spec"]["taints"] == []
    assert restored["metadata"]["labels"]["touched-by-kubelet"] == "yes"


def test_pod_quota_freezes_at_live_pods_and_is_deleted(tmp_path: Path):
    cluster = lab_cluster()
    mutator = Mutator(cluster, tmp_path)
    journal = mutator.inject(experiment(mutations=[{"action": "freeze_pod_quota"}]), "run-4")
    quota = cluster.get(ObjectRef("v1", "ResourceQuota", QUOTA_NAME, NS))
    assert quota["spec"]["hard"]["pods"] == "5"  # the Succeeded pod does not count
    mutator.rollback(journal)
    assert cluster.get(ObjectRef("v1", "ResourceQuota", QUOTA_NAME, NS)) is None


def test_failed_injection_rolls_back_what_it_already_did(tmp_path: Path):
    cluster = lab_cluster()
    mutator = Mutator(cluster, tmp_path)
    with pytest.raises(InjectionError, match="does not exist"):
        mutator.inject(
            experiment(mutations=[
                {"action": "create", "manifest": {
                    "apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": "extra"}, "data": {}}},
                {"action": "restart", "target": {"kind": "Deployment", "name": "ghost"}},
            ]),
            "run-5",
        )
    assert cluster.get(ObjectRef("v1", "ConfigMap", "extra", NS)) is None
    assert mutator.pending() == []


def test_reconcile_undoes_an_experiment_left_behind_by_a_crash(tmp_path: Path):
    cluster = lab_cluster()
    crashed = Mutator(cluster, tmp_path)
    crashed.inject(
        experiment(mutations=[{
            "action": "patch",
            "target": {"kind": "Service", "name": "cart-service"},
            "ops": [{"op": "replace", "path": "/spec/selector", "value": {"app": "nope"}}],
        }]),
        "run-6",
    )
    # A fresh controller process starts and finds the journal.
    restarted = Mutator(cluster, tmp_path)
    assert restarted.reconcile() == {"run-6": []}
    assert cluster.get(ObjectRef("v1", "Service", "cart-service", NS))["spec"]["selector"] == {
        "app": "cart-service"
    }
    assert restarted.pending() == []


def test_rollback_recreates_an_object_deleted_mid_experiment(tmp_path: Path):
    cluster = lab_cluster()
    mutator = Mutator(cluster, tmp_path)
    journal = mutator.inject(
        experiment(mutations=[{
            "action": "patch",
            "target": {"kind": "ConfigMap", "name": "billing-config"},
            "ops": [{"op": "replace", "path": "/data/MAX_WORKERS", "value": "many"}],
        }]),
        "run-7",
    )
    cluster.delete(ObjectRef("v1", "ConfigMap", "billing-config", NS))
    assert mutator.rollback(journal) == []
    assert cluster.get(ObjectRef("v1", "ConfigMap", "billing-config", NS))["data"] == {
        "MAX_WORKERS": "4"
    }
