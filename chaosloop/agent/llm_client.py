"""OpenRouter chat-completions client (HLD §15)."""

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class ChatResult:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    raw_message: dict[str, Any] | None = None

    def assistant_message(self) -> dict[str, Any]:
        if self.raw_message is not None:
            return self.raw_message
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments or json.dumps(call.arguments),
                    },
                }
                for call in self.tool_calls
            ]
        return message


class LLMClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> ChatResult: ...


class LLMError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        max_attempts: int = 4,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_attempts = max_attempts
        self.transport = transport
        self.sleep = sleep

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> ChatResult:
        if model.startswith("anthropic/"):
            messages = _with_cache_breakpoints(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            # A diagnosis is a short JSON object. Capping the completion keeps cost bounded and
            # avoids providers reserving their full context window against the account balance.
            "max_tokens": self.max_tokens,
            # Asks OpenRouter to return generation cost inline, so cost tracking does not
            # depend on a hand-maintained pricing table.
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        response = self._post(payload)
        if response.status_code >= 400:
            raise LLMError(f"OpenRouter {response.status_code}: {response.text[:500]}")

        body = response.json()
        if "choices" not in body:
            raise LLMError(f"unexpected OpenRouter response: {json.dumps(body)[:500]}")

        message = body["choices"][0]["message"]
        usage = body.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return ChatResult(
            content=message.get("content"),
            tool_calls=_parse_tool_calls(message.get("tool_calls") or []),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
            cost_usd=float(usage.get("cost") or 0.0),
            raw_message=message,
        )

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """POST with retries on transient failures only.

        Rate limits and gateway errors clear on their own, and dropping the run would leave a
        hole in the eval; a 402 (out of credits) or a 400 will not, so those return at once.
        """
        for attempt in range(1, self.max_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "X-Title": "ChaosLoop",
                        },
                        json=payload,
                    )
            except httpx.TransportError:
                if attempt == self.max_attempts:
                    raise
                self.sleep(_backoff(attempt))
                continue
            if response.status_code not in RETRYABLE_STATUS or attempt == self.max_attempts:
                return response
            self.sleep(_retry_delay(response, attempt))
        raise AssertionError("unreachable")


RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_DELAY = 30.0


def _backoff(attempt: int) -> float:
    return min(2.0**attempt, MAX_RETRY_DELAY)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    try:
        return min(float(response.headers["retry-after"]), MAX_RETRY_DELAY)
    except (KeyError, ValueError):
        return _backoff(attempt)


def _with_cache_breakpoints(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the system prompt and the newest message as Anthropic cache breakpoints.

    The system prompt is identical on every call of every case, and self-consistency resends
    one conversation several times, so both prefixes are read from cache at a tenth of the
    price. Copies are returned because the caller reuses its list across calls, and breakpoints
    left behind would pile up past Anthropic's limit of four.
    """
    marked = [dict(message) for message in messages]
    for index in sorted({0, len(marked) - 1}):
        content = marked[index].get("content")
        if marked[index].get("role") != "assistant" and isinstance(content, str) and content:
            marked[index]["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
    return marked


def _parse_tool_calls(raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls):
        function = raw.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                arguments = {}
        except json.JSONDecodeError:
            arguments = {}
        calls.append(
            ToolCall(
                id=raw.get("id") or f"call_{index}",
                name=function.get("name") or "",
                arguments=arguments,
                raw_arguments=raw_arguments,
            )
        )
    return calls
