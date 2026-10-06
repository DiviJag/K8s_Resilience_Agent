"""The experiment catalog (HLD §3 Planner, §10, §11).

Every live fault the lab can cause is declared here, with its ground-truth label, the services
it is allowed to degrade, and a reference fix. The Planner may only pick from this catalog and
only set parameters to values listed for them - it never writes manifests (HLD §11).

The reference fix is what turns the catalog into something checkable: an oracle run applies
it, and if the service does not recover, the experiment - not the agent - is broken.
"""

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from chaosloop.live.gateway import ObjectRef
from chaosloop.schemas.golden_case import FixCheck
from chaosloop.schemas.taxonomy import Category

API_VERSIONS = {
    "Deployment": "apps/v1",
    "Service": "v1",
    "ConfigMap": "v1",
    "PersistentVolumeClaim": "v1",
    "ResourceQuota": "v1",
    "NetworkPolicy": "networking.k8s.io/v1",
    "Node": "v1",
    "Pod": "v1",
    "NetworkChaos": "chaos-mesh.org/v1alpha1",
    "DNSChaos": "chaos-mesh.org/v1alpha1",
}

_PARAM = re.compile(r"\$\{(\w+)\}")


class Parameter(BaseModel):
    default: Any
    allowed: list[Any] = Field(min_length=1)

    @model_validator(mode="after")
    def _default_allowed(self) -> "Parameter":
        if self.default not in self.allowed:
            raise ValueError(f"default {self.default!r} is not in allowed {self.allowed!r}")
        return self


class Target(BaseModel):
    kind: str
    name: str

    def ref(self, namespace: str) -> ObjectRef:
        if self.kind not in API_VERSIONS:
            raise ValueError(f"unknown kind {self.kind!r}")
        scoped = None if self.kind == "Node" else namespace
        return ObjectRef(API_VERSIONS[self.kind], self.kind, self.name, scoped)


class Mutation(BaseModel):
    action: Literal["patch", "create", "taint_workers", "restart", "freeze_pod_quota"]
    target: Target | None = None
    ops: list[dict[str, Any]] = []
    manifest: dict[str, Any] | None = None
    taint: dict[str, str] | None = None

    @model_validator(mode="after")
    def _complete(self) -> "Mutation":
        needs = {
            "patch": self.target is not None and bool(self.ops),
            "create": self.manifest is not None,
            "taint_workers": self.taint is not None,
            "restart": self.target is not None,
            "freeze_pod_quota": True,
        }
        if not needs[self.action]:
            raise ValueError(f"{self.action} mutation is missing its target, ops or manifest")
        return self


class ReferenceFix(BaseModel):
    summary: str
    patch: str = ""
    target: str | None = None


class Experiment(BaseModel):
    id: str
    category: Category
    injector: Literal["config", "chaos"]
    service: str
    blast_radius: list[str]
    mutations: list[Mutation]
    root_cause: str
    fix_check: FixCheck
    reference_fix: ReferenceFix
    parameters: dict[str, Parameter] = {}
    namespace: str = "chaosloop-lab"
    detect_timeout_s: int = 240
    recover_timeout_s: int = 300
    # False keeps an experiment available for recording cases by hand (`chaosloop-lab inject`)
    # while the planner never schedules it.
    plannable: bool = True

    @model_validator(mode="after")
    def _service_in_blast_radius(self) -> "Experiment":
        if self.service not in self.blast_radius:
            raise ValueError(f"{self.id}: alerting service must be inside its own blast radius")
        return self

    def resolve(self, values: dict[str, Any] | None = None) -> "Experiment":
        """Substitute `${name}` parameters, refusing any value the catalog does not allow."""
        chosen = {name: p.default for name, p in self.parameters.items()}
        for name, value in (values or {}).items():
            if name not in self.parameters:
                raise ValueError(f"{self.id} has no parameter {name!r}")
            if value not in self.parameters[name].allowed:
                raise ValueError(
                    f"{self.id}: {name}={value!r} is outside {self.parameters[name].allowed!r}"
                )
            chosen[name] = value
        data = _substitute(self.model_dump(mode="json", exclude={"parameters"}), chosen)
        return Experiment.model_validate({**data, "parameters": {}})


def _substitute(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _PARAM.fullmatch(value)
        if whole and whole.group(1) in params:
            return copy.deepcopy(params[whole.group(1)])
        return _PARAM.sub(lambda m: str(params.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {key: _substitute(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, params) for item in value]
    return value


class Catalog(BaseModel):
    experiments: list[Experiment]
    version: str

    def get(self, experiment_id: str) -> Experiment:
        for experiment in self.experiments:
            if experiment.id == experiment_id:
                return experiment
        raise KeyError(f"no experiment {experiment_id!r} in the catalog")

    def plannable(self) -> list[Experiment]:
        return [e for e in self.experiments if e.plannable]

    def categories(self) -> list[Category]:
        """Categories the planner covers."""
        return sorted({e.category for e in self.plannable()}, key=lambda c: c.value)


def load_catalog(path: Path) -> Catalog:
    raw = path.read_bytes()
    data = yaml.safe_load(raw) or {}
    experiments = [Experiment.model_validate(item) for item in data.get("experiments", [])]
    ids = [e.id for e in experiments]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate experiment ids: {', '.join(duplicates)}")
    for experiment in experiments:
        unresolved = _unresolved(experiment)
        if unresolved:
            raise ValueError(f"{experiment.id} uses undeclared parameters: {unresolved}")
    return Catalog(
        experiments=experiments, version=f"cat:sha256:{hashlib.sha256(raw).hexdigest()[:12]}"
    )


def _unresolved(experiment: Experiment) -> list[str]:
    text = yaml.safe_dump(experiment.model_dump(mode="json", exclude={"parameters"}))
    return sorted(set(_PARAM.findall(text)) - set(experiment.parameters))
