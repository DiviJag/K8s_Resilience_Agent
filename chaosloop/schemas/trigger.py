"""Normalized trigger schema (HLD §2.2).

A live Alertmanager webhook and a recorded golden-case alert both convert to this, so the
agent receives identical input in Tier A and Tier B.
"""

from typing import Any

from pydantic import BaseModel, Field


class Trigger(BaseModel):
    alertname: str
    service: str
    namespace: str
    pod: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict) #extra is a dictionary 
    #with string keys and any type of values. If nothing is provided, 
    # initialize it as an empty dictionary.

    def render(self) -> str:
        lines = [
            f"alertname: {self.alertname}",
            f"namespace: {self.namespace}",
            f"service: {self.service}",
        ]
        if self.pod:
            lines.append(f"pod: {self.pod}")
        for key, value in self.extra.items():
            lines.append(f"{key}: {value}")
        return "\n".join(lines)

