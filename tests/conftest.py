import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from chaosloop.agent.llm_client import ChatResult, ToolCall

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def golden_root() -> Path:
    return REPO_ROOT / "golden"


class FakeLLMClient:
    """Scripted stand-in for OpenRouter.

    Each script entry is either a final answer (str) or a list of (tool_name, arguments)
    tuples to call. Outgoing messages are kept so tests can assert on what the model was
    actually shown - which is how redaction is verified end to end.
    """

    def __init__(self, script: list[Any], tokens: int = 100, cost: float = 0.001) -> None:
        self.script = list(script)
        self.tokens = tokens
        self.cost = cost
        self.seen_messages: list[list[dict[str, Any]]] = []

    @property
    def sent_text(self) -> str:
        return "\n".join(
            str(message.get("content") or "")
            for conversation in self.seen_messages
            for message in conversation
        )

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str
    ) -> ChatResult:
        self.seen_messages.append([dict(m) for m in messages])
        step = self.script.pop(0) if self.script else '{"never": "scripted"}'

        if isinstance(step, str):
            return ChatResult(
                content=step,
                prompt_tokens=self.tokens,
                completion_tokens=self.tokens // 2,
                cost_usd=self.cost,
            )

        calls = [
            ToolCall(id=f"call_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(step)
        ]
        return ChatResult(
            content=None,
            tool_calls=calls,
            prompt_tokens=self.tokens,
            completion_tokens=self.tokens // 5,
            cost_usd=self.cost,
        )


def diagnosis_json(
    category: str = "registry_auth_missing",
    patch: str = "spec.template.spec.imagePullSecrets: [{name: regcred}]",
    confidence: float = 0.9,
) -> str:
    return json.dumps(
        {
            "category": category,
            "root_cause": "test root cause",
            "evidence": ["event: pull access denied"],
            "fix": {"summary": "test fix", "patch": patch},
            "confidence": confidence,
        }
    )


def write_case(
    directory: Path,
    case_id: str,
    category: str,
    split: str = "test",
    evidence: dict[str, Any] | None = None,
    fix_check: dict[str, Any] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    case = {
        "case_id": case_id,
        "split": split,
        "trigger": {
            "alertname": "PodImagePullBackOff",
            "service": "svc",
            "namespace": "prod",
            "pod": "svc-abc123",
        },
        "evidence": evidence
        if evidence is not None
        else {
            "get_pod_status": {"phase": "Pending"},
            "get_events": [{"message": "pull access denied, authentication required"}],
        },
        "expected": {
            "category": category,
            "root_cause": "expected root cause",
            "fix_check": fix_check
            or {"path": "spec.template.spec.imagePullSecrets", "must_exist": True},
        },
        "metadata": {
            "source": "manual",
            "k8s_version": "1.30",
            "runtime": "containerd",
            "recorded": "2026-09-30",
        },
    }
    path = directory / f"{case_id}.yaml"
    path.write_text(yaml.safe_dump(case), encoding="utf-8")
    return path
