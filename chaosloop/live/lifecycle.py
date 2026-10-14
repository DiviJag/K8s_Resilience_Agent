"""Lifecycle Controller (HLD §9): one live experiment, start to finish.

    PRECHECK  steady state healthy and no alerts? otherwise abort - a pre-existing fault would
              corrupt the label
    INJECT    journal first, then change the cluster; T_inject
    WATCH     wait for an alert inside the blast radius (T_detect); abort if anything outside
              it degrades; a timeout is a detection miss, which is a finding about monitoring
    DIAGNOSE  the investigator works the live cluster through the read-only backend; T_diagnose
    VERIFY    the proposed fix goes through policy and a dry run, then is applied; T_recover is
              when the service is healthy again
    ROLLBACK  always, whatever happened above
    COOLDOWN  wait for steady state before the next experiment

Times are measured, not estimated: time to detect is T_detect - T_inject (monitoring), time to
diagnose is T_diagnose - T_detect (the agent), time to recover is T_recover - T_inject (the
whole system).
"""

import time
from collections.abc import Callable
from datetime import date
from typing import Any

from pydantic import BaseModel

from chaosloop.eval.grader import grade_case
from chaosloop.live.candidates import CandidateQueue, surprise
from chaosloop.live.catalog import Experiment
from chaosloop.live.fixer import FixOutcome, apply_fix
from chaosloop.live.gateway import Gateway, ObjectRef
from chaosloop.live.investigators import Investigator
from chaosloop.live.monitoring import (
    AlertSource,
    UpstreamProbe,
    alert_problems,
    deployment_problems,
    upstream_problems,
)
from chaosloop.live.mutator import InjectionError, Mutator
from chaosloop.live.normalizer import group_alerts, trigger_for_group
from chaosloop.recorder.recorder import Recorder
from chaosloop.schemas.eval_record import EvalRecord
from chaosloop.schemas.golden_case import CaseMetadata, Expected, GoldenCase
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.interface import ToolBackend


class LiveSettings(BaseModel):
    poll_s: float = 5.0
    cooldown_timeout_s: float = 300.0
    # Upstream error-rate ceiling for steady state. Pod availability alone cannot see a Chaos
    # Mesh DNS or network injection that leaked residual state after rollback - the pods come
    # back Ready but requests keep erroring. An error rate above this flags the lab as unsteady.
    upstream_error_rate_max: float = 0.05


class LifecycleController:
    def __init__(
        self,
        gateway: Gateway,
        alerts: AlertSource,
        mutator: Mutator,
        backend: ToolBackend,
        investigator_for: Callable[[Experiment], Investigator],
        catalog_version: str,
        candidates: CandidateQueue | None = None,
        golden: list[GoldenCase] | None = None,
        settings: LiveSettings | None = None,
        upstream: UpstreamProbe | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.alerts = alerts
        self.mutator = mutator
        self.backend = backend
        self.investigator_for = investigator_for
        self.catalog_version = catalog_version
        self.candidates = candidates
        self.golden = golden or []
        self.settings = settings or LiveSettings()
        self.upstream = upstream
        self.clock = clock
        self.sleep = sleep
        self.on_event = on_event or (lambda message: None)

    # --- steady state ---------------------------------------------------------------------

    def steady_state_problems(self, namespace: str, services: list[str] | None = None) -> list[str]:
        return (
            deployment_problems(self.gateway, namespace, services)
            + alert_problems(self.alerts.firing(), namespace, services)
            + upstream_problems(
                self.upstream, namespace, services, self.settings.upstream_error_rate_max
            )
        )

    def _wait_steady(
        self, namespace: str, timeout_s: float, services: list[str] | None = None
    ) -> tuple[float | None, list[str]]:
        deadline = self.clock() + timeout_s
        problems: list[str] = []
        while self.clock() <= deadline:
            problems = self.steady_state_problems(namespace, services)
            if not problems:
                return self.clock(), []
            self.sleep(self.settings.poll_s)
        return None, problems

    # --- watch ----------------------------------------------------------------------------

    def _watch(self, experiment: Experiment, t_inject: float) -> tuple[Trigger | None, str | None]:
        """Returns (trigger, None) on detection, (None, reason) on abort, (None, None) on miss."""
        deadline = t_inject + experiment.detect_timeout_s
        while self.clock() <= deadline:
            firing = [
                a for a in self.alerts.firing()
                if (a.get("labels") or {}).get("namespace") == experiment.namespace
            ]
            groups = group_alerts(firing)
            outside = sorted({svc for (_, svc) in groups if svc not in experiment.blast_radius})
            if outside:
                return None, f"blast radius exceeded: {', '.join(outside)} degraded"
            inside = {svc: group for (_, svc), group in groups.items()}
            if inside:
                service = experiment.service if experiment.service in inside else sorted(inside)[0]
                return trigger_for_group(inside[service]), None
            self.sleep(self.settings.poll_s)
        return None, None

    # --- one experiment ---------------------------------------------------------------------

    def run(
        self,
        experiment: Experiment,
        run_id: str,
        repeat: int = 1,
        parameters: dict[str, Any] | None = None,
    ) -> EvalRecord:
        investigator = self.investigator_for(experiment)
        expected = Expected(
            category=experiment.category,
            root_cause=experiment.root_cause,
            fix_check=experiment.fix_check,
        )
        record: dict[str, Any] = {
            "run_id": run_id,
            "case_id": experiment.id,
            "experiment_id": experiment.id,
            "parameters": parameters or {},
            "tier": "B",
            "repeat": repeat,
            "model": investigator.name,
            "prompt_version": investigator.prompt_version,
            "golden_version": self.catalog_version,
            "expected_category": experiment.category,
            "agent_category": None,
            "category_correct": False,
            "fix_correct": False,
            "is_false_positive": False,
            "malformed": False,
            "confidence": None,
        }

        problems = self.steady_state_problems(experiment.namespace)
        if problems:
            self.on_event(f"{experiment.id}: precheck failed: {problems}")
            return EvalRecord(**record, aborted=f"precheck: {'; '.join(problems)}")

        # Start from a clean event log: Kubernetes keeps events for an hour, so a prior
        # incident's events would still be the newest the agent sees and would mislead it (and
        # the snapshot) about this one. Best-effort - a stale event is not worth aborting over.
        cleared = self._clear_events(experiment.namespace)
        if cleared:
            self.on_event(f"{experiment.id}: cleared {cleared} stale events")

        experiment_key = f"{run_id}-{experiment.id}-r{repeat}"
        t_inject = self.clock()
        try:
            journal = self.mutator.inject(experiment, experiment_key)
        except InjectionError as exc:
            return EvalRecord(**record, aborted=f"injection: {exc}")
        self.on_event(f"{experiment.id}: injected")
        record["t_inject"] = t_inject

        try:
            trigger, abort = self._watch(experiment, t_inject)
            if abort:
                self.on_event(f"{experiment.id}: aborted - {abort}")
                record["aborted"] = abort
            elif trigger is None:
                self.on_event(f"{experiment.id}: no alert within {experiment.detect_timeout_s}s")
                record["detected"] = False
                record["error"] = "detection_miss"
            else:
                try:
                    record.update(self._investigate(experiment, expected, investigator, trigger))
                except Exception as exc:
                    # One experiment's bug must not end a batch; the record says what happened
                    # and the rollback below still runs.
                    self.on_event(f"{experiment.id}: investigation crashed - {exc!r}")
                    record.update(detected=True, trigger_alertname=trigger.alertname,
                                  error=f"crash: {exc!r}")
        finally:
            rollback_problems = self.mutator.rollback(journal)
            if rollback_problems:
                record["error"] = f"rollback: {'; '.join(rollback_problems)}"
            self.on_event(f"{experiment.id}: rolled back")
            # A Chaos Mesh injection can leak residual state after its CR is deleted - cached
            # DNS answers in the caller's resolver, half-open connections, iptables rules the
            # chaos-dns-server has not yet reconciled away. Pod availability does not catch any
            # of that. Replacing the blast-radius pods forces fresh resolvers and new sockets,
            # which closes the gap deterministically; config injections leave no such residue
            # so we skip the extra churn there.
            if experiment.injector == "chaos":
                cleaned = self._chaos_residual_cleanup(experiment)
                if cleaned:
                    self.on_event(
                        f"{experiment.id}: cleared {cleaned} residual pod(s) in blast radius"
                    )

        _, still_wrong = self._wait_steady(experiment.namespace, self.settings.cooldown_timeout_s)
        if still_wrong:
            note = f"cooldown incomplete: {'; '.join(still_wrong)}"
            record["error"] = f"{record['error']}; {note}" if record.get("error") else note
        return EvalRecord(**record)

    def _investigate(
        self,
        experiment: Experiment,
        expected: Expected,
        investigator: Investigator,
        trigger: Trigger,
    ) -> dict[str, Any]:
        t_detect = self.clock()
        self.on_event(f"{experiment.id}: detected via {trigger.alertname}")
        # Evidence is snapshotted at detection: events expire, and the flywheel needs what the
        # cluster showed at the time, not after the fix and rollback have rewritten it.
        evidence = Recorder(self.backend).capture(trigger, self._evidence_targets(trigger))

        diagnosis, trace = investigator.investigate(trigger, self.backend)
        t_diagnose = self.clock()
        exhausted = bool(trace.error and trace.error.startswith("budget_exceeded"))
        grade = grade_case(diagnosis, expected, budget_exhausted=exhausted)
        self.on_event(
            f"{experiment.id}: diagnosed {diagnosis.category.value if diagnosis else 'nothing'}"
        )

        outcome = (
            apply_fix(diagnosis.fix, experiment, self.gateway)
            if diagnosis is not None and not exhausted
            else FixOutcome(status="not_applicable", reason="no diagnosis")
        )
        fix_verified: bool | None = None
        t_recover: float | None = None
        if outcome.status == "applied":
            self.on_event(f"{experiment.id}: applied fix to {outcome.target}")
            t_recover, _ = self._wait_steady(
                experiment.namespace, experiment.recover_timeout_s, experiment.blast_radius
            )
            fix_verified = t_recover is not None
        elif outcome.status == "rejected":
            self.on_event(f"{experiment.id}: fix rejected - {outcome.reason}")
            fix_verified = False

        if self.candidates is not None:
            self._maybe_propose(experiment, trigger, evidence, expected, grade.category_correct)

        return {
            "detected": True,
            "trigger_alertname": trigger.alertname,
            "t_detect": t_detect,
            "t_diagnose": t_diagnose,
            "t_recover": t_recover,
            "agent_category": diagnosis.category if diagnosis else None,
            "agent_root_cause": diagnosis.root_cause if diagnosis else None,
            "agent_fix_patch": diagnosis.fix.patch if diagnosis else None,
            "category_correct": grade.category_correct,
            "fix_correct": grade.fix_correct,
            "is_false_positive": grade.is_false_positive,
            "malformed": grade.malformed,
            "confidence": diagnosis.confidence if diagnosis and not exhausted else None,
            "self_reported_confidence": diagnosis.confidence if diagnosis and not exhausted
            else None,
            "consistency_confidence": trace.consistency,
            "samples": trace.samples,
            "tool_calls": trace.tool_calls,
            "tokens": trace.tokens,
            "cost_usd": trace.cost_usd,
            "latency_s": t_diagnose - t_detect,
            "fix_applied": outcome.status == "applied",
            "fix_verified": fix_verified,
            "fix_rejected": outcome.reason if outcome.status == "rejected" else None,
            "error": trace.error,
        }

    def _clear_events(self, namespace: str) -> int:
        cleared = 0
        try:
            events = self.gateway.list("v1", "Event", namespace)
        except Exception:
            return 0
        for event in events:
            try:
                self.gateway.delete(ObjectRef.of(event))
                cleared += 1
            except Exception:
                continue  # already gone, or no permission - neither should stop the experiment
        return cleared

    def _evidence_targets(self, trigger: Trigger) -> dict[str, str | None]:
        """Workload-level alerts name no pod or claim; without them the snapshot would record
        pod status, logs and claim status as "not available" and the case could never be
        replayed fairly."""
        find = getattr(self.backend, "find_unready_pod", None)
        pod = trigger.pod or (find(trigger.namespace, trigger.service) if find else None)
        pvc = None
        spec = self.backend.get_deployment_spec(trigger.namespace, trigger.service)
        if isinstance(spec, dict):
            volumes = (((spec.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
                "volumes"
            ) or []
            claims = [
                (v.get("persistentVolumeClaim") or {}).get("claimName")
                for v in volumes
                if isinstance(v, dict)
            ]
            pvc = next((c for c in claims if c), None)
        return {"pod": pod or None, "pvc": pvc}

    def _maybe_propose(
        self,
        experiment: Experiment,
        trigger: Trigger,
        evidence: dict[str, Any],
        expected: Expected,
        agent_correct: bool,
    ) -> None:
        case = GoldenCase(
            case_id=f"{experiment.id}-{int(self.clock())}",
            split="dev",
            trigger=trigger,
            evidence=evidence,
            expected=expected,
            metadata=CaseMetadata(
                source="chaos", k8s_version=self._k8s_version(), runtime="containerd",
                recorded=date.today(),
            ),
        )
        reason = surprise(case, agent_correct, self.golden)
        if reason and self.candidates is not None:
            path = self.candidates.propose(case, reason)
            self.on_event(f"{experiment.id}: queued candidate {path.name} ({reason})")

    def _k8s_version(self) -> str:
        for node in self.gateway.list("v1", "Node"):
            version = ((node.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion")
            if version:
                return str(version).lstrip("v")
        return "unknown"

    def _chaos_residual_cleanup(self, experiment: Experiment) -> int:
        """Replace pods owned by any Deployment in the blast radius so cached DNS answers and
        stuck connections from a Chaos Mesh injection cannot leak into the next experiment.

        Best-effort: a missing pod or a deny from RBAC is not worth aborting the run over, so
        every failure is swallowed. Returns the count of pods actually deleted."""
        if not experiment.blast_radius:
            return 0
        wanted = set(experiment.blast_radius)
        try:
            pods = self.gateway.list("v1", "Pod", experiment.namespace)
        except Exception:
            return 0
        deleted = 0
        for pod in pods:
            meta = pod.get("metadata") or {}
            labels = meta.get("labels") or {}
            # Pods are matched by the `app` label the lab's services share with their Deployment
            # name; chaos-mesh-owned pods carry no such label and are skipped so we never kick
            # the injector's own workers.
            app = labels.get("app")
            if app not in wanted:
                continue
            try:
                self.gateway.delete(ObjectRef("v1", "Pod", meta["name"], experiment.namespace))
                deleted += 1
            except Exception:
                continue
        return deleted
