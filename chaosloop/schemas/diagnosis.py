"""Diagnostician output contract (HLD §5.2).

Everything downstream - prompting, grading, the CI gate - is built against this model.
A response that fails validation counts as a failed case rather than being coerced.
"""

from pydantic import BaseModel, ConfigDict, Field

from chaosloop.schemas.taxonomy import Category


class Fix(BaseModel):
    # This is a Pydantic configuration that tells Pydantic to reject any fields that are not
    # explicitly defined in the model. This is a security measure to prevent malicious data
    # from being injected into the model.
    model_config = ConfigDict(extra="forbid")

    summary: str
    patch: str


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    root_cause: str
    evidence: list[str]
    fix: Fix
    confidence: float = Field(ge=0.0, le=1.0)
