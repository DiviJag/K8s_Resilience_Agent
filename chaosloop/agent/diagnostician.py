"""ReAct diagnostician (HLD §3, §5).

Backend-agnostic: it takes any ToolBackend and never learns whether it is talking to a live
cluster or to recorded evidence. Step, token and cost budgets are enforced here in code, not
merely requested in the prompt (HLD §11).
"""

import time
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from chaosloop.agent.llm_client import ChatResult, LLMClient
from chaosloop.agent.prompts import PROMPT_VERSION, render_user_message, system_prompt
from chaosloop.schemas.diagnosis import Diagnosis, Fix
from chaosloop.schemas.eval_record import ToolCallRecord
from chaosloop.schemas.taxonomy import Category
from chaosloop.schemas.trigger import Trigger
from chaosloop.tools.interface import TOOL_SPECS, ToolBackend, dispatch
from chaosloop.tools.sanitize import wrap_untrusted

SEARCH_MEMORY = "search_memory"

SEARCH_MEMORY_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_MEMORY,
        "description": (
            "Search confirmed past incidents and runbooks for ones resembling this failure. "
            "Query with the specific error text you have observed (event messages, waiting "
            "reasons, log lines), not with the alert name. Results are precedent, not proof."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Observed error text to match on"}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

# Without a nudge, resamples open with a paragraph of prose before the JSON, which costs output
# tokens on every one of the N samples and occasionally never reaches the JSON at all. The nudge
# restates that `unknown` is allowed: "answer now" alone pressured samples into naming a cause
# the evidence did not support, so sampling changed the answer instead of measuring it.
SAMPLE_NUDGE = (
    "Using only the evidence above, give your final answer now: a single JSON object matching "
    "the schema, with no other text. The same rules apply as before - if the evidence does not "
    "identify a cause, the answer is unknown."
)


class MemorySearch(Protocol):
    snapshot: str

    def search_text(self, query: str) -> str: ...


class Budget(BaseModel):
    # 12, not 8: a live incident spanning a caller and its upstream (checkout -> cart) needs
    # steps on both sides - read the caller's logs, confirm the upstream is healthy, check
    # endpoints and network policies - and 8 cut those investigations off mid-gather. It is a
    # ceiling, not a target; the prompt still says to stop as soon as a conclusion is justified.
    max_steps: int = 12
    max_tokens: int = 40_000
    max_cost_usd: float = 0.50


class DiagnosticTrace(BaseModel):
    tool_calls: list[ToolCallRecord] = []
    tokens: dict[str, int] = {"prompt": 0, "completion": 0, "cached": 0, "total": 0}
    cost_usd: float = 0.0
    latency_s: float = 0.0
    steps: int = 0
    raw_final_message: str | None = None
    error: str | None = None
    samples: list[str | None] = []
    sample_errors: int = 0
    consistency: float | None = None
    messages: list[dict[str, Any]] = []


class Diagnostician:
    def __init__(
        self,
        backend: ToolBackend,
        model: str,
        client: LLMClient,
        budget: Budget | None = None,
        prompt_version: str = PROMPT_VERSION,
        samples: int = 0,
        sample_temperature: float = 0.7,
        memory: MemorySearch | None = None,
    ) -> None:
        self.backend = backend
        self.model = model
        self.client = client
        self.budget = budget or Budget()
        self.prompt_version = prompt_version
        self.samples = samples
        self.sample_temperature = sample_temperature
        self.memory = memory
        self.tool_specs = TOOL_SPECS + ([SEARCH_MEMORY_SPEC] if memory is not None else [])

    def diagnose(self, trigger: Trigger) -> tuple[Diagnosis | None, DiagnosticTrace]:
        started = time.monotonic()
        trace = DiagnosticTrace()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(memory_enabled=self.memory is not None)},
            {"role": "user", "content": render_user_message(trigger)},
        ]

        for step in range(1, self.budget.max_steps + 1):
            exhausted = self._exhausted(trace)
            if exhausted:
                return self._budget_exceeded(trace, messages, exhausted, started)

            trace.steps = step
            try:
                result = self.client.chat(
                    messages=messages, tools=self.tool_specs, model=self.model
                )
            except Exception as exc:
                trace.error = f"llm_error: {exc}"
                trace.messages = messages
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
            trace.messages = [*messages, result.assistant_message()]
            try:
                diagnosis = parse_diagnosis(result.content)
            except (ValidationError, ValueError) as exc:
                trace.error = f"validation_error: {exc}"
                trace.latency_s = time.monotonic() - started
                return None, trace

            if self.samples:
                self._self_consistency(messages, diagnosis, trace)
            trace.latency_s = time.monotonic() - started
            return diagnosis, trace

        return self._budget_exceeded(trace, messages, "max_steps", started)

    def _self_consistency(
        self, messages: list[dict[str, Any]], diagnosis: Diagnosis, trace: DiagnosticTrace
    ) -> None:
        """Confidence as agreement across resampled answers (HLD §7.4).

        The reported answer stays the temperature-0 one, so accuracy is unchanged by sampling;
        the samples only measure how robust that answer is. The evidence is gathered once and
        only the final answer is resampled - tools are disabled, so a sample cannot wander off
        and investigate something else. Gated by the cost budget only: with tools off and a
        fixed count, sampling cannot run away the way an investigation can.
        """
        prompt = [*messages, {"role": "user", "content": SAMPLE_NUDGE}]
        for _ in range(self.samples):
            if trace.cost_usd >= self.budget.max_cost_usd:
                break
            try:
                result = self.client.chat(
                    messages=prompt,
                    tools=self.tool_specs,
                    model=self.model,
                    temperature=self.sample_temperature,
                    tool_choice="none",
                )
            except Exception:
                # A provider failure says nothing about the answer, so it is not a vote.
                trace.sample_errors += 1
                continue
            self._account(trace, result)
            try:
                trace.samples.append(parse_diagnosis(result.content).category.value)
            except (ValidationError, ValueError):
                trace.samples.append(None)

        if trace.samples:
            agree = sum(1 for sample in trace.samples if sample == diagnosis.category.value)
            trace.consistency = agree / len(trace.samples)

    def _exhausted(self, trace: DiagnosticTrace) -> str | None:
        # Budget on billable tokens, not total: the whole conversation is re-sent every step,
        # so `total` counts the same evidence once per step. With prompt caching those re-reads
        # are cache hits that cost almost nothing - charging full price for them exhausted the
        # budget on live evidence (large logs and events) long before the step limit.
        billable = trace.tokens["prompt"] - trace.tokens["cached"] + trace.tokens["completion"]
        if billable >= self.budget.max_tokens:
            return "max_tokens"
        if trace.cost_usd >= self.budget.max_cost_usd:
            return "max_cost_usd"
        return None

    @staticmethod
    def _account(trace: DiagnosticTrace, result: ChatResult) -> None:
        trace.tokens["prompt"] += result.prompt_tokens
        trace.tokens["completion"] += result.completion_tokens
        trace.tokens["cached"] += result.cached_tokens
        trace.tokens["total"] = trace.tokens["prompt"] + trace.tokens["completion"]
        trace.cost_usd += result.cost_usd

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> tuple[Any, str | None]:
        try:
            if name == SEARCH_MEMORY and self.memory is not None:
                return self.memory.search_text(str(arguments.get("query", ""))), None
            return dispatch(self.backend, name, arguments), None
        except Exception as exc:
            message = f"tool error: {exc}"
            return message, str(exc)

    @staticmethod
    def _budget_exceeded(
        trace: DiagnosticTrace, messages: list[dict[str, Any]], limit: str, started: float
    ) -> tuple[Diagnosis, DiagnosticTrace]:
        trace.error = f"budget_exceeded: {limit}"
        trace.messages = messages
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
    candidates = _json_objects(content)
    if not candidates:
        raise ValueError("final message contained no JSON object")
    # The real answer is the last valid object: models that disobey "JSON only" tend to think in
    # prose first (e.g. a bare `{port: 9999}` mid-sentence) and put the actual diagnosis last.
    last_error: Exception | None = None
    for blob in reversed(candidates):
        try:
            return Diagnosis.model_validate_json(blob)
        except (ValidationError, ValueError) as exc:
            last_error = exc
    raise last_error if last_error else ValueError("no JSON object validated as a diagnosis")


def _json_objects(text: str) -> list[str]:
    """Every top-level {...} in the text, brace-matched and ignoring braces inside strings, so a
    diagnosis survives stray prose, markdown fences or a snippet the model wrote before it."""
    objects: list[str] = []
    depth = start = 0
    in_string = escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                objects.append(text[start : i + 1])
    return objects
