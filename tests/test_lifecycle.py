"""The live loop end to end against a simulated lab - no cluster, no model, no waiting."""

from pathlib import Path
from typing import Any

import pytest

from chaosloop.agent.diagnostician import DiagnosticTrace
from chaosloop.eval.harness import summarize
from chaosloop.live.candidates import CandidateQueue
from chaosloop.live.catalog import load_catalog
from chaosloop.live.gateway import ObjectRef
from chaosloop.live.investigators import OracleInvestigator
from chaosloop.live.lifecycle import LifecycleController, LiveSettings
from chaosloop.live.mutator import Mutator
from chaosloop.schemas.diagnosis import Diagnosis, Fix
from chaosloop.tools.replay_backend import ReplayBackend
from tests.conftest import REPO_ROOT
from tests.fakes import NS, lab_cluster, service

CATALOG = load_catalog(REPO_ROOT / "lab" / "experiments.yaml")
BILLING = ObjectRef("apps/v1", "Deployment", "billing-service", NS)
CART_SVC = ObjectRef("v1", "Service", "cart-service", NS)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def billing_crashing(cluster) -> bool:
    env = cluster.get(BILLING)["spec"]["template"]["spec"]["containers"][0].get("env") or []
    return not any(e["name"] == "DATABASE_URL" for e in env)


def cart_unreachable(cluster) -> bool:
    return cluster.get(CART_SVC)["spec"]["selector"] != {"app": "cart-service"}


class SimulatedAlerts:
    """Each rule fires after `delay` seconds of its fault holding - like a rule's `for:` - and
    clears as soon as the fault is gone."""

    def __init__(self, cluster, clock: Clock, delay: float = 30.0) -> None:
        self.cluster, self.clock, self.delay = cluster, clock, delay
        self.rules: dict[str, tuple[Any, dict[str, str]]] = {
            "billing": (billing_crashing,
                        {"alertname": "PodCrashLooping", "service": "billing-service"}),
            "cart": (cart_unreachable,
                     {"alertname": "UpstreamErrors", "service": "checkout-frontend"}),
        }
        self.since: dict[str, float] = {}
        self.extra: list[dict[str, Any]] = []

    def firing(self) -> list[dict[str, Any]]:
        alerts = []
        for name, (broken, labels) in self.rules.items():
            if broken(self.cluster):
                start = self.since.setdefault(name, self.clock.now())
                if self.clock.now() - start >= self.delay:
                    alerts.append({"labels": {**labels, "namespace": NS},
                                   "startsAt": str(start), "status": {"state": "active"}})
            else:
                self.since.pop(name, None)
        return alerts + self.extra


class FixedInvestigator:
    prompt_version = "test"

    def __init__(self, category: str, patch: str = "", target: str | None = None) -> None:
        self.name = "fixed"
        self.diagnosis = Diagnosis(category=category, root_cause="rc", evidence=["e"],
                                   fix=Fix(summary="s", patch=patch, target=target),
                                   confidence=0.8)

    def investigate(self, trigger, backend):
        self.trigger = trigger
        return self.diagnosis, DiagnosticTrace()


def make(tmp_path: Path, investigator=None, delay: float = 30.0, **kwargs):
    cluster, clock = lab_cluster(), Clock()
    cluster.add(service("checkout-frontend"))
    alerts = SimulatedAlerts(cluster, clock, delay)
    controller = LifecycleController(
        gateway=cluster,
        alerts=alerts,
        mutator=Mutator(cluster, tmp_path / "journal", clock=clock.now),
        backend=ReplayBackend({}),
        investigator_for=(lambda e: investigator) if investigator else OracleInvestigator,
        catalog_version="cat:test",
        settings=LiveSettings(poll_s=5, cooldown_timeout_s=60),
        clock=clock.now,
        sleep=clock.sleep,
        **kwargs,
    )
    return controller, cluster, alerts, clock


def test_oracle_run_detects_fixes_recovers_and_rolls_back(tmp_path: Path):
    controller, cluster, _, _ = make(tmp_path)
    original = cluster.get(BILLING)["spec"]

    record = controller.run(CATALOG.get("billing-missing-env-var"), "run-a")

    assert record.tier == "B" and record.aborted is None
    assert record.detected and record.trigger_alertname == "PodCrashLooping"
    assert record.category_correct and record.fix_correct
    assert record.fix_applied and record.fix_verified
    assert record.t_inject < record.t_detect <= record.t_diagnose <= record.t_recover
    assert 30 <= record.t_detect - record.t_inject <= 40  # the rule's delay, polled every 5s
    # Rollback put the lab back exactly as it was - including undoing the applied fix.
    assert cluster.get(BILLING)["spec"] == original
    assert not list((tmp_path / "journal").glob("*.json"))


def test_cascade_fix_lands_on_the_upstream_service(tmp_path: Path):
    controller, cluster, _, _ = make(tmp_path)
    record = controller.run(CATALOG.get("cart-selector-mismatch"), "run-b")
    assert record.detected and record.fix_verified
    assert cluster.get(CART_SVC)["spec"]["selector"] == {"app": "cart-service"}


def test_an_alert_that_never_fires_is_a_detection_miss(tmp_path: Path):
    controller, cluster, alerts, _ = make(tmp_path)
    alerts.rules.pop("billing")
    record = controller.run(CATALOG.get("billing-missing-env-var"), "run-c")
    assert record.detected is False and record.error == "detection_miss"
    assert not billing_crashing(cluster)  # still rolled back


def test_degradation_outside_the_blast_radius_aborts(tmp_path: Path):
    controller, cluster, alerts, _ = make(tmp_path)

    original_firing = alerts.firing

    def with_collateral():
        found = original_firing()
        if billing_crashing(cluster):
            found.append({"labels": {"alertname": "PodNotReady", "service": "reporting-service",
                                     "namespace": NS}, "status": {"state": "active"}})
        return found

    alerts.firing = with_collateral
    record = controller.run(CATALOG.get("billing-missing-env-var"), "run-d")
    assert record.aborted and "reporting-service" in record.aborted
    assert not billing_crashing(cluster)


def test_precheck_refuses_to_inject_into_a_sick_lab(tmp_path: Path):
    controller, cluster, alerts, _ = make(tmp_path)
    alerts.extra.append({"labels": {"alertname": "PodCrashLooping", "service": "cart-service",
                                    "namespace": NS}, "status": {"state": "active"}})
    record = controller.run(CATALOG.get("billing-missing-env-var"), "run-e")
    assert record.aborted and record.aborted.startswith("precheck")
    assert cluster.writes == []


def test_a_rejected_fix_is_a_failed_fix_and_still_rolls_back(tmp_path: Path):
    investigator = FixedInvestigator("missing_env_var", patch="spec.replicas: 0",
                                     target="Deployment/billing-service")
    controller, cluster, _, _ = make(tmp_path, investigator)
    record = controller.run(CATALOG.get("billing-missing-env-var"), "run-f")
    assert record.category_correct
    assert record.fix_applied is False and record.fix_verified is False
    assert "zero" in (record.fix_rejected or "")
    assert cluster.get(BILLING)["spec"]["replicas"] == 2
    assert not billing_crashing(cluster)


def test_a_fix_that_does_not_recover_is_unverified(tmp_path: Path):
    investigator = FixedInvestigator(
        "missing_env_var", patch="spec.template.spec.containers[0].env: [{name: WRONG, value: x}]",
        target="Deployment/billing-service",
    )
    controller, _, _, _ = make(tmp_path, investigator)
    experiment = CATALOG.get("billing-missing-env-var").model_copy(update={"recover_timeout_s": 60})
    record = controller.run(experiment, "run-g")
    assert record.fix_applied and record.fix_verified is False and record.t_recover is None


def test_infrastructure_fixes_are_not_applicable(tmp_path: Path):
    investigator = FixedInvestigator("selector_mismatch", patch="")
    controller, _, _, _ = make(tmp_path, investigator)
    record = controller.run(CATALOG.get("cart-selector-mismatch"), "run-h")
    assert record.fix_applied is False and record.fix_verified is None


def test_a_wrong_answer_is_queued_for_human_review(tmp_path: Path):
    queue = CandidateQueue(tmp_path / "golden")
    investigator = FixedInvestigator("bad_config_value")
    controller, _, _, _ = make(tmp_path, investigator, candidates=queue)
    record = controller.run(CATALOG.get("billing-missing-env-var"), "run-i")
    assert not record.category_correct
    pending = queue.pending()
    assert len(pending) == 1
    candidate = pending[0]
    assert candidate.expected.category.value == "missing_env_var"  # ground truth, not the guess
    assert candidate.metadata.model_extra["candidate_reason"] == "the agent got it wrong"
    assert candidate.trigger.alertname == "PodCrashLooping"


def test_the_agent_sees_the_same_trigger_shape_as_tier_a(tmp_path: Path):
    investigator = FixedInvestigator("missing_env_var")
    controller, _, _, _ = make(tmp_path, investigator)
    controller.run(CATALOG.get("billing-missing-env-var"), "run-j")
    assert investigator.trigger.service == "billing-service"
    assert investigator.trigger.namespace == NS


def test_live_metrics_summarise_a_tier_b_run(tmp_path: Path):
    controller, _, alerts, _ = make(tmp_path)
    detected = controller.run(CATALOG.get("billing-missing-env-var"), "run-k")
    alerts.rules.pop("cart")
    missed = controller.run(CATALOG.get("cart-selector-mismatch"), "run-k")

    metrics = summarize([detected, missed])
    live = metrics.live
    assert live is not None
    assert live.n_experiments == 2 and live.n_aborted == 0
    assert live.detection_miss_rate == pytest.approx(0.5)
    assert live.fix_success_rate == 1.0 and live.n_fix_attempts == 1
    assert live.mean_time_to_detect_s == pytest.approx(detected.t_detect - detected.t_inject)
    # The missed fault was never investigated, so it does not count against accuracy.
    assert metrics.n_scored == 1 and metrics.category_accuracy == 1.0
    assert metrics.by_category["selector_mismatch"]["detection_miss"] == 1.0


def test_snapshot_targets_the_unhealthy_pod_and_claim_when_the_alert_names_neither(tmp_path):
    """Workload alerts (DeploymentReplicasMismatch) carry no pod label; the candidate case
    must still hold pod status, logs and claim status, or it cannot be replayed fairly."""

    class Backend(ReplayBackend):
        def find_unready_pod(self, namespace, service):
            return f"{service}-7d9f8b6c5-abcde"

    backend = Backend({
        "get_pod_status": {"phase": "Running"},
        "get_logs": {"current": "boot", "previous": None},
        "get_pvc_status": {"phase": "Bound"},
        "get_deployment_spec": {"spec": {"template": {"spec": {"volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "reporting-data"}},
        ]}}}},
    })
    queue = CandidateQueue(tmp_path / "golden")
    controller, _, _, _ = make(tmp_path, FixedInvestigator("bad_config_value"), candidates=queue)
    controller.backend = backend
    controller.run(CATALOG.get("billing-missing-env-var"), "run-k")
    evidence = queue.pending()[0].evidence
    assert evidence["get_pod_status"] == {"phase": "Running"}
    assert evidence["get_logs"]["current"] == "boot"
    assert evidence["get_pvc_status"] == {"phase": "Bound"}


def test_a_crashing_investigation_is_recorded_and_still_rolled_back(tmp_path: Path):
    class Crashing:
        name, prompt_version = "crash", "v3"

        def investigate(self, trigger, backend):
            raise AttributeError("'V1Probe' object has no attribute 'exec'")

    controller, cluster, _, _ = make(tmp_path, Crashing())
    original = cluster.get(BILLING)["spec"]
    record = controller.run(CATALOG.get("billing-missing-env-var"), "run-l")
    assert record.error and record.error.startswith("crash:")
    assert record.detected is True and not record.category_correct
    assert cluster.get(BILLING)["spec"] == original


def _event(name: str) -> dict:
    return {"apiVersion": "v1", "kind": "Event",
            "metadata": {"name": name, "namespace": NS},
            "reason": "Stale", "message": "from a previous experiment"}


def test_stale_events_are_cleared_before_injection(tmp_path: Path):
    controller, cluster, _, _ = make(tmp_path)
    cluster.add(_event("old-event-1"))
    cluster.add(_event("old-event-2"))
    assert len(cluster.list("v1", "Event", NS)) == 2

    controller.run(CATALOG.get("billing-missing-env-var"), "run-events")
    # The experiment's own run leaves none behind in the fake, so an empty log proves the
    # pre-injection sweep ran.
    assert cluster.list("v1", "Event", NS) == []


def test_event_clearing_is_best_effort(tmp_path: Path):
    controller, cluster, _, _ = make(tmp_path)
    cluster.add(_event("stuck"))

    real_delete = cluster.delete

    def refuse_event_deletes(ref):
        if ref.kind == "Event":
            raise RuntimeError("forbidden: injector may not delete events")
        return real_delete(ref)

    cluster.delete = refuse_event_deletes  # type: ignore[method-assign]
    # The run completes and rolls back despite the delete being refused.
    record = controller.run(CATALOG.get("billing-missing-env-var"), "run-events-2")
    assert record.aborted is None and record.category_correct
