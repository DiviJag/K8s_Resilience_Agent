"""Who diagnoses a live incident.

The lifecycle controller does not care whether the answer comes from the model or from the
catalog. The oracle answers with the experiment's ground truth and reference fix, which proves
the loop - detection, fix verification, recovery, rollback - end to end for free, and checks
the lab: if the reference fix does not recover the service, the experiment is what is broken.
"""

from typing import Protocol

from chaosloop.agent.diagnostician import Budget, Diagnostician, DiagnosticTrace, MemorySearch
from chaosloop.agent.llm_client import LLMClient
from chaosloop.agent.prompts import PROMPT_VERSION
from chaosloop.live.catalog import Experiment
from chaosloop.schemas.diagnosis import Diagnosis, Fix
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.interface import ToolBackend


class Investigator(Protocol):
    name: str
    prompt_version: str

    def investigate(
        self, trigger: Trigger, backend: ToolBackend
    ) -> tuple[Diagnosis | None, DiagnosticTrace]: ...


class OracleInvestigator:
    prompt_version = "oracle"

    def __init__(self, experiment: Experiment) -> None:
        self.experiment = experiment
        self.name = "oracle"

    def investigate(
        self, trigger: Trigger, backend: ToolBackend
    ) -> tuple[Diagnosis | None, DiagnosticTrace]:
        reference = self.experiment.reference_fix
        diagnosis = Diagnosis(
            category=self.experiment.category,
            root_cause=self.experiment.root_cause,
            evidence=[f"oracle: ground truth for {self.experiment.id}"],
            fix=Fix(summary=reference.summary, patch=reference.patch, target=reference.target),
            confidence=1.0,
        )
        return diagnosis, DiagnosticTrace()


class AgentInvestigator:
    def __init__(
        self,
        model: str,
        client: LLMClient,
        samples: int = 0,
        memory: MemorySearch | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.name = model
        self.prompt_version = PROMPT_VERSION
        self.client = client
        self.samples = samples
        self.memory = memory
        self.budget = budget

    def investigate(
        self, trigger: Trigger, backend: ToolBackend
    ) -> tuple[Diagnosis | None, DiagnosticTrace]:
        return Diagnostician(
            backend=backend,
            model=self.name,
            client=self.client,
            budget=self.budget,
            samples=self.samples,
            memory=self.memory,
        ).diagnose(trigger)
