"""Root-cause LLM-as-judge (HLD §7.1) - Phase 2.

Kept as a real module so the harness has a stable import target: wiring in the judge later
is additive rather than a refactor.
"""

from typing import Any


def judge_root_cause(*args: Any, **kwargs: Any) -> float:
    raise NotImplementedError(
        "Root-cause LLM-as-judge is Phase 2 scope (HLD §7.1). Phase 1 gates on category "
        "exact-match plus the structural fix_check only."
    )
