import httpx
import pytest

from chaosloop.agent.llm_client import LLMError, OpenRouterClient

OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "{}"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.001},
}


def client_for(responses: list[httpx.Response]) -> tuple[OpenRouterClient, list[float]]:
    queue = list(responses)
    sleeps: list[float] = []
    transport = httpx.MockTransport(lambda request: queue.pop(0))
    return (
        OpenRouterClient(api_key="k", transport=transport, sleep=sleeps.append),
        sleeps,
    )


def test_rate_limit_is_retried_honouring_retry_after():
    client, sleeps = client_for(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json=OK_BODY)]
    )
    result = client.chat([{"role": "user", "content": "hi"}], [], "openai/x")
    assert result.content == "{}"
    assert sleeps == [7.0]


def test_out_of_credits_is_not_retried():
    client, sleeps = client_for([httpx.Response(402, text="add credits")])
    with pytest.raises(LLMError, match="402"):
        client.chat([{"role": "user", "content": "hi"}], [], "openai/x")
    assert sleeps == []


def test_retries_give_up_after_max_attempts():
    client, sleeps = client_for([httpx.Response(503)] * 4)
    with pytest.raises(LLMError, match="503"):
        client.chat([{"role": "user", "content": "hi"}], [], "openai/x")
    assert sleeps == [2.0, 4.0, 8.0]


def test_cache_breakpoints_never_touch_the_callers_messages():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    client = OpenRouterClient(api_key="k", transport=httpx.MockTransport(handler))
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    client.chat(messages, [], "anthropic/claude-sonnet-4.5")
    client.chat(messages, [], "anthropic/claude-sonnet-4.5")

    assert messages[0]["content"] == "sys"  # the caller's list is unchanged
    sent = seen[1]["messages"]
    marks = [p for m in sent for p in (m["content"] if isinstance(m["content"], list) else [])]
    assert len(marks) == 2 and all(p["cache_control"] == {"type": "ephemeral"} for p in marks)


def test_non_anthropic_models_get_no_cache_markers():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    client = OpenRouterClient(api_key="k", transport=httpx.MockTransport(handler))
    client.chat([{"role": "system", "content": "sys"}], [], "openai/gpt-4.1-mini")
    assert seen[0]["messages"][0]["content"] == "sys"
