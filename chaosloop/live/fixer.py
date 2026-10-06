"""Fix verification (HLD §6.3, §9 VERIFY, §11 Remediation).

The agent proposes; this module decides whether a proposal may touch the lab at all, and only
then applies it. Every fix passes a policy check and a server-side dry run first. The policy is
deliberately about outcomes rather than syntax - it compares the object before and after - so
a fix cannot sneak past it by phrasing: "scale to zero" is refused whether it arrives as
`spec.replicas: 0` or as a JSON patch, which matters because logs are attacker-controlled and
a prompt injection asked for exactly that.
"""

from typing import Any, Literal

import yaml
from pydantic import BaseModel

from chaosloop.live.catalog import API_VERSIONS, Experiment
from chaosloop.live.gateway import Gateway, GatewayError, ObjectRef, for_replace
from chaosloop.live.mutator import QUOTA_NAME
from chaosloop.live.paths import PathError, apply_fragment, apply_json_patch, split_path
from chaosloop.schemas.diagnosis import Fix

FIXABLE_KINDS = frozenset(
    {"Deployment", "Service", "ConfigMap", "ResourceQuota", "NetworkPolicy",
     "PersistentVolumeClaim"}
)


class FixOutcome(BaseModel):
    status: Literal["not_applicable", "rejected", "applied"]
    target: str | None = None
    reason: str | None = None


class FixRejected(ValueError):
    pass


def _owned_target(experiment: Experiment, kind: str) -> str | None:
    """An object of `kind` the experiment itself touched, if any."""
    for mutation in experiment.mutations:
        if mutation.target is not None and mutation.target.kind == kind:
            return mutation.target.name
        manifest = mutation.manifest or {}
        if manifest.get("kind") == kind:
            return (manifest.get("metadata") or {}).get("name")
    return None


def _kind_for_path(path: str) -> str:
    head = [s for s in split_path(path) if isinstance(s, str)][:2]
    if head[:1] == ["data"]:
        return "ConfigMap"
    if head[:2] == ["spec", "hard"]:
        return "ResourceQuota"
    if head[:2] in (["spec", "selector"], ["spec", "ports"], ["spec", "type"]):
        return "Service"
    if head[:2] in (["spec", "ingress"], ["spec", "egress"], ["spec", "podSelector"],
                    ["spec", "policyTypes"]):
        return "NetworkPolicy"
    if head[:2] in (["spec", "storageClassName"], ["spec", "accessModes"]):
        return "PersistentVolumeClaim"
    return "Deployment"


def resolve_target(fix: Fix, paths: list[str], experiment: Experiment) -> ObjectRef:
    if fix.target:
        kind, _, name = fix.target.partition("/")
        if not name:
            raise FixRejected(f"target {fix.target!r} is not Kind/name")
    else:
        kind = _kind_for_path(paths[0]) if paths else "Deployment"
        name = ""
    if kind not in FIXABLE_KINDS:
        raise FixRejected(f"fixes may not modify {kind} objects")
    if not name:
        name = (
            _owned_target(experiment, kind)
            or (QUOTA_NAME if kind == "ResourceQuota" else experiment.service)
        )
    return ObjectRef(API_VERSIONS[kind], kind, name, experiment.namespace)


def parse_patch(text: str) -> tuple[Any, list[str]]:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FixRejected(f"patch is not YAML: {exc}") from exc
    if isinstance(parsed, list) and all(isinstance(op, dict) and "op" in op for op in parsed):
        return parsed, [str(op.get("path", "")) for op in parsed]
    if isinstance(parsed, dict) and parsed:
        return parsed, [str(key) for key in parsed]
    raise FixRejected("patch is neither a YAML fragment keyed by path nor a JSON patch")


def _registry(image: str) -> str:
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def _pod_spec(obj: dict[str, Any]) -> dict[str, Any]:
    return ((obj.get("spec") or {}).get("template") or {}).get("spec") or {}


def policy_violations(current: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if desired == current:
        return ["the patch changes nothing"]
    for field in ("name", "namespace"):
        if (desired.get("metadata") or {}).get(field) != (current.get("metadata") or {}).get(field):
            problems.append(f"changes metadata.{field}")

    kind = current.get("kind")
    if kind == "Deployment":
        replicas = (desired.get("spec") or {}).get("replicas")
        if replicas is not None and replicas < 1:
            problems.append("scales the workload to zero")
        before, after = _pod_spec(current), _pod_spec(desired)
        old_names = [c.get("name") for c in before.get("containers") or []]
        new_names = [c.get("name") for c in after.get("containers") or []]
        if old_names != new_names:
            problems.append("adds or removes containers")
        for old, new in zip(before.get("containers") or [], after.get("containers") or [],
                            strict=False):
            if _registry(old.get("image", "")) != _registry(new.get("image", "")):
                problems.append(
                    f"moves {new.get('name')} to another registry "
                    f"({_registry(old.get('image', ''))} -> {_registry(new.get('image', ''))})"
                )
            security = new.get("securityContext") or {}
            if security.get("privileged") or security.get("allowPrivilegeEscalation"):
                problems.append(f"grants {new.get('name')} privilege")
        for flag in ("hostNetwork", "hostPID", "hostIPC"):
            if after.get(flag):
                problems.append(f"enables {flag}")
        if any("hostPath" in (v or {}) for v in after.get("volumes") or []):
            problems.append("mounts a hostPath volume")
        if after.get("serviceAccountName") != before.get("serviceAccountName"):
            problems.append("changes the service account")
    elif kind == "Service":
        old_spec, new_spec = current.get("spec") or {}, desired.get("spec") or {}
        if new_spec.get("type") != old_spec.get("type"):
            problems.append("changes the Service type")
        if new_spec.get("externalIPs"):
            problems.append("exposes external IPs")
    return problems


def apply_fix(fix: Fix, experiment: Experiment, gateway: Gateway) -> FixOutcome:
    """Policy check, server-side dry run, then apply - in that order, all or nothing."""
    if not fix.patch.strip():
        return FixOutcome(status="not_applicable", reason="no manifest change proposed")
    ref: ObjectRef | None = None
    try:
        parsed, paths = parse_patch(fix.patch)
        ref = resolve_target(fix, paths, experiment)
        current = gateway.get(ref)
        if current is None:
            raise FixRejected(f"{ref.label()} does not exist")
        desired = (
            apply_json_patch(current, parsed)
            if isinstance(parsed, list)
            else apply_fragment(current, parsed)
        )
        violations = policy_violations(current, desired)
        if violations:
            raise FixRejected("policy: " + "; ".join(violations))
        body = for_replace(desired, current)
        try:
            gateway.replace(body, dry_run=True)
        except GatewayError as exc:
            raise FixRejected(f"dry-run: {exc}") from exc
        gateway.replace(body)
        return FixOutcome(status="applied", target=ref.label())
    except (FixRejected, PathError) as exc:
        return FixOutcome(
            status="rejected", target=ref.label() if ref else None, reason=str(exc)
        )
