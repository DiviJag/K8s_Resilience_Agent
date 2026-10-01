"""ReAct diagnostician (HLD §3, §5).

Backend-agnostic: it takes any ToolBackend and never learns whether it is talking to a live
cluster or to recorded evidence. Step, token and cost budgets are enforced here in code, not
merely requested in the prompt (HLD §11).
"""

import re
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from chaosloop.agent.llm_client import ChatResult, LLMClient
from chaosloop.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT, render_user_message
from chaosloop.schemas.diagnosis import Diagnosis, Fix
from chaosloop.schemas.eval_record import ToolCallRecord
from chaosloop.schemas.taxonomy import Category
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.interface import TOOL_SPECS, ToolBackend, dispatch
from chaosloop.tools.sanitize import wrap_untrusted

_FENCE_START = re.compile(r"^```[a-zA-Z]*\s*")
_FENCE_END = re.compile(r"\s*```$")


class Budget(BaseModel):
    max_steps: int = 8
    max_tokens: int = 40_000
    max_cost_usd: float = 0.50


class DiagnosticTrace(BaseModel):
    tool_calls: list[ToolCallRecord] = []
    tokens: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}
    cost_usd: float = 0.0
    latency_s: float = 0.0
    steps: int = 0
    raw_final_message: str | None = None
    error: str | None = None


class Diagnostician:
    def __init__(
        self,
        backend: ToolBackend,
        model: str,
        client: LLMClient,
        budget: Budget | None = None,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self.backend = backend
        self.model = model
        self.client = client
        self.budget = budget or Budget()
        self.prompt_version = prompt_version

    def diagnose(self, trigger: Trigger) -> tuple[Diagnosis | None, DiagnosticTrace]:
        started = time.monotonic()
        trace = DiagnosticTrace()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": render_user_message(trigger)},
        ]

        for step in range(1, self.budget.max_steps + 1):
            exhausted = self._exhausted(trace)
            if exhausted:
                return self._budget_exceeded(trace, exhausted, started)

            trace.steps = step
            try:
                result = self.client.chat(messages=messages, tools=TOOL_SPECS, model=self.model)
            except Exception as exc:
                trace.error = f"llm_error: {exc}"
                trace.latency_s = time.monotonic() - started
                return None, trace

            self._account(trace, result)

            if result.tool_calls:
                messages.append(result.assistant_message())
                for call in result.tool_calls:
                    output, error = self._run_tool(call.name, call.arguments)
                    trace.tool_calls.append(
                        ToolCallRecord(
                            step=step, tool=call.name, arguments=call.arguments, error=error
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": wrap_untrusted(call.name, output),
                        }
                    )
                continue

            trace.raw_final_message = result.content
            trace.latency_s = time.monotonic() - started
            try:
                return parse_diagnosis(result.content), trace
            except (ValidationError, ValueError) as exc:
                trace.error = f"validation_error: {exc}"
                return None, trace

        return self._budget_exceeded(trace, "max_steps", started)

    def _exhausted(self, trace: DiagnosticTrace) -> str | None:
        if trace.tokens["total"] >= self.budget.max_tokens:
            return "max_tokens"
        if trace.cost_usd >= self.budget.max_cost_usd:
            return "max_cost_usd"
        return None

    @staticmethod
    def _account(trace: DiagnosticTrace, result: ChatResult) -> None:
        trace.tokens["prompt"] += result.prompt_tokens
        trace.tokens["completion"] += result.completion_tokens
        trace.tokens["total"] = trace.tokens["prompt"] + trace.tokens["completion"]
        trace.cost_usd += result.cost_usd

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> tuple[Any, str | None]:
        try:
            return dispatch(self.backend, name, arguments), None
        except Exception as exc:
            message = f"tool error: {exc}"
            return message, str(exc)

    @staticmethod
    def _budget_exceeded(
        trace: DiagnosticTrace, limit: str, started: float
    ) -> tuple[Diagnosis, DiagnosticTrace]:
        trace.error = f"budget_exceeded: {limit}"
        trace.latency_s = time.monotonic() - started
        diagnosis = Diagnosis(
            category=Category.UNKNOWN,
            root_cause="Investigation stopped before a cause could be established.",
            evidence=[f"{limit} budget exhausted after {trace.steps} steps"],
            fix=Fix(summary="Insufficient evidence gathered; escalate to a human.", patch=""),
            confidence=0.0,
        )
        return diagnosis, trace


def parse_diagnosis(content: str | None) -> Diagnosis:
    if not content or not content.strip():
        raise ValueError("final message was empty")
    text = _FENCE_END.sub("", _FENCE_START.sub("", content.strip()))
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("final message contained no JSON object")
    return Diagnosis.model_validate_json(text[start : end + 1])
