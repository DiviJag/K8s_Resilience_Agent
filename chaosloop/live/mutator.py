"""Config Mutator and Runtime Injector (HLD §3 component 1, §9).

Every change is written to a journal on disk *before* it is made. If the controller dies
mid-experiment, `reconcile()` reads the journals left behind and undoes them, so the lab never
stays broken because a laptop slept (HLD §9 "crash recovery").

Rollback restores snapshots with a full replace rather than re-applying manifests: `kubectl
apply` cannot remove a field an injection *added*, so the lab would never return to steady
state. Taints are the exception - nodes change under the controller (the kubelet updates them),
so the taint is removed by key instead of restoring a stale copy of the node.
"""

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from chaosloop.live.catalog import Experiment, Mutation
from chaosloop.live.gateway import Gateway, GatewayError, ObjectRef, for_replace
from chaosloop.live.paths import apply_json_patch, assign

CONTROL_PLANE_LABEL = "node-role.kubernetes.io/control-plane"
QUOTA_NAME = "chaosloop-experiment-quota"
_LIVE_PHASES = {"Pending", "Running"}


class JournalEntry(BaseModel):
    action: Literal["replaced", "created", "tainted"]
    ref: dict[str, str | None]
    snapshot: dict[str, Any] | None = None
    taint_key: str | None = None

    def object_ref(self) -> ObjectRef:
        return ObjectRef(**self.ref)  # type: ignore[arg-type]


class Journal(BaseModel):
    run_id: str
    experiment_id: str
    started_at: float
    entries: list[JournalEntry] = []


class InjectionError(RuntimeError):
    pass


class Mutator:
    def __init__(
        self, gateway: Gateway, journal_dir: Path, clock: Callable[[], float] = time.time
    ) -> None:
        self.gateway = gateway
        self.journal_dir = journal_dir
        self.clock = clock

    def _path(self, run_id: str) -> Path:
        return self.journal_dir / f"{run_id}.json"

    def _write(self, journal: Journal) -> None:
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self._path(journal.run_id).write_text(journal.model_dump_json(indent=2), encoding="utf-8")

    def inject(self, experiment: Experiment, run_id: str) -> Journal:
        journal = Journal(run_id=run_id, experiment_id=experiment.id, started_at=self.clock())
        self._write(journal)
        try:
            for mutation in experiment.mutations:
                self._apply(mutation, experiment.namespace, journal)
        except Exception as exc:
            problems = self.rollback(journal)
            detail = f"; rollback problems: {problems}" if problems else ""
            raise InjectionError(f"{experiment.id}: injection failed: {exc}{detail}") from exc
        return journal

    def _snapshot(self, journal: Journal, ref: ObjectRef, current: dict[str, Any]) -> None:
        if not any(e.action == "replaced" and e.ref == ref.as_dict() for e in journal.entries):
            journal.entries.append(
                JournalEntry(action="replaced", ref=ref.as_dict(), snapshot=current)
            )
            self._write(journal)

    def _replace(self, journal: Journal, ref: ObjectRef, edit: Callable[[dict], dict]) -> None:
        current = self.gateway.get(ref)
        if current is None:
            raise InjectionError(f"{ref.label()} does not exist")
        self._snapshot(journal, ref, current)
        self.gateway.replace(for_replace(edit(current), current))

    def _create(self, journal: Journal, manifest: dict[str, Any]) -> None:
        ref = ObjectRef.of(manifest)
        journal.entries.append(JournalEntry(action="created", ref=ref.as_dict()))
        self._write(journal)
        self.gateway.create(manifest)

    def _apply(self, mutation: Mutation, namespace: str, journal: Journal) -> None:
        if mutation.action == "patch":
            assert mutation.target is not None
            ops = mutation.ops
            self._replace(
                journal, mutation.target.ref(namespace), lambda o: apply_json_patch(o, ops)
            )
        elif mutation.action == "restart":
            assert mutation.target is not None
            stamp = {"chaosloop.io/restartedAt": datetime.now(UTC).isoformat()}
            self._replace(
                journal,
                mutation.target.ref(namespace),
                lambda o: assign(o, "spec.template.metadata.annotations", stamp),
            )
        elif mutation.action == "create":
            assert mutation.manifest is not None
            manifest = json.loads(json.dumps(mutation.manifest))
            manifest.setdefault("metadata", {}).setdefault("namespace", namespace)
            self._create(journal, manifest)
        elif mutation.action == "freeze_pod_quota":
            pods = self.gateway.list("v1", "Pod", namespace)
            live = sum(1 for p in pods if (p.get("status") or {}).get("phase") in _LIVE_PHASES)
            self._create(
                journal,
                {
                    "apiVersion": "v1",
                    "kind": "ResourceQuota",
                    "metadata": {"name": QUOTA_NAME, "namespace": namespace},
                    "spec": {"hard": {"pods": str(live)}},
                },
            )
        elif mutation.action == "taint_workers":
            assert mutation.taint is not None
            self._taint_workers(journal, mutation.taint)

    def _taint_workers(self, journal: Journal, taint: dict[str, str]) -> None:
        for node in self.gateway.list("v1", "Node"):
            labels = (node.get("metadata") or {}).get("labels") or {}
            if CONTROL_PLANE_LABEL in labels:
                continue
            ref = ObjectRef("v1", "Node", node["metadata"]["name"])
            journal.entries.append(
                JournalEntry(action="tainted", ref=ref.as_dict(), taint_key=taint["key"])
            )
            self._write(journal)
            spec = node.setdefault("spec", {})
            taints = [t for t in spec.get("taints") or [] if t.get("key") != taint["key"]]
            spec["taints"] = [*taints, dict(taint)]
            self.gateway.replace(for_replace(node, node))

    def rollback(self, journal: Journal) -> list[str]:
        """Undo every journalled change, newest first. Returns problems; empty means clean."""
        problems: list[str] = []
        for entry in reversed(journal.entries):
            ref = entry.object_ref()
            try:
                if entry.action == "created":
                    self.gateway.delete(ref)
                elif entry.action == "tainted":
                    node = self.gateway.get(ref)
                    if node is not None:
                        spec = node.setdefault("spec", {})
                        spec["taints"] = [
                            t for t in spec.get("taints") or [] if t.get("key") != entry.taint_key
                        ]
                        self.gateway.replace(for_replace(node, node))
                elif entry.action == "replaced" and entry.snapshot is not None:
                    current = self.gateway.get(ref)
                    if current is None:
                        restored = for_replace(entry.snapshot, entry.snapshot)
                        restored["metadata"].pop("resourceVersion", None)
                        self.gateway.create(restored)
                    else:
                        self.gateway.replace(for_replace(entry.snapshot, current))
            except (GatewayError, KeyError) as exc:
                problems.append(f"{entry.action} {ref.label()}: {exc}")
        if not problems:
            self._path(journal.run_id).unlink(missing_ok=True)
        return problems

    def pending(self) -> list[Journal]:
        if not self.journal_dir.is_dir():
            return []
        return [
            Journal.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.journal_dir.glob("*.json"))
        ]

    def reconcile(self) -> dict[str, list[str]]:
        """Roll back any experiment a previous controller left behind."""
        return {journal.run_id: self.rollback(journal) for journal in self.pending()}
