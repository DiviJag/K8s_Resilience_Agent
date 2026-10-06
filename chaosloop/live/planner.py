"""Rule-based Planner (HLD §10): decides what gets tested next.

The chaos layer does not reason - it executes a catalog entry. Eval results reach it only
through this function, as structured data. The MVP rule is the HLD's: run the category with the
fewest samples, or once every category has enough, the one the agent is weakest at. The planner
picks catalog entries and allowed parameter values only; it never writes a manifest.
"""

from pydantic import BaseModel

from chaosloop.eval.harness import is_attempted, is_graded
from chaosloop.live.catalog import Catalog, Experiment
from chaosloop.schemas.eval_record import EvalRecord
from chaosloop.schemas.taxonomy import Category

MIN_SAMPLES = 3


class CategoryStats(BaseModel):
    category: Category
    samples: int
    accuracy: float | None
    detection_miss: float | None


class Plan(BaseModel):
    experiment_id: str
    parameters: dict[str, object]
    reason: str


def category_stats(records: list[EvalRecord], categories: list[Category]) -> list[CategoryStats]:
    """Scoreboard across both tiers (HLD §10): offline answers count as samples too."""
    stats = []
    for category in categories:
        attempted = [r for r in records if is_attempted(r) and r.expected_category is category]
        graded = [r for r in attempted if is_graded(r)]
        live = [r for r in attempted if r.tier == "B"]
        stats.append(
            CategoryStats(
                category=category,
                samples=len(attempted),
                accuracy=sum(r.category_correct for r in graded) / len(graded) if graded else None,
                detection_miss=(
                    sum(r.detected is False for r in live) / len(live) if live else None
                ),
            )
        )
    return stats


def plan_next(
    catalog: Catalog, records: list[EvalRecord], planned: list[str] | None = None
) -> Plan:
    """`planned` lists experiment ids already queued but not yet run; each counts as a sample of
    its category and as the most recent run, so a batch spreads across the catalog."""
    planned = planned or []
    stats = {s.category: s for s in category_stats(records, catalog.categories())}
    for experiment_id in planned:
        category = catalog.get(experiment_id).category
        stats[category] = stats[category].model_copy(
            update={"samples": stats[category].samples + 1}
        )
    last_run = {r.experiment_id: i for i, r in enumerate(records) if r.experiment_id}
    for offset, experiment_id in enumerate(planned):
        last_run[experiment_id] = len(records) + offset
    experiments = catalog.plannable()
    order = {e.id: i for i, e in enumerate(experiments)}

    def recency(experiment: Experiment) -> int:
        return last_run.get(experiment.id, -1)

    under = [e for e in experiments if stats[e.category].samples < MIN_SAMPLES]
    if under:
        chosen = min(
            under,
            key=lambda e: (
                stats[e.category].samples,
                stats[e.category].accuracy if stats[e.category].accuracy is not None else -1.0,
                recency(e),
                order[e.id],
            ),
        )
        s = stats[chosen.category]
        reason = (
            f"{chosen.category.value} has {s.samples} sample(s), under the {MIN_SAMPLES} "
            "needed before its accuracy means anything"
        )
    else:
        chosen = min(
            experiments,
            key=lambda e: (
                stats[e.category].accuracy if stats[e.category].accuracy is not None else 1.0,
                -(stats[e.category].detection_miss or 0.0),
                stats[e.category].samples,
                recency(e),
                order[e.id],
            ),
        )
        s = stats[chosen.category]
        miss = f", {s.detection_miss:.0%} detection miss" if s.detection_miss else ""
        reason = (
            f"{chosen.category.value} is the weakest category: "
            f"{(s.accuracy or 0.0):.0%} accuracy over {s.samples} samples{miss}"
        )
    runs = sum(1 for r in records if r.experiment_id == chosen.id) + planned.count(chosen.id)
    parameters = {
        name: param.allowed[runs % len(param.allowed)]
        for name, param in chosen.parameters.items()
    }
    return Plan(experiment_id=chosen.id, parameters=parameters, reason=reason)


def plan_batch(catalog: Catalog, records: list[EvalRecord], count: int) -> list[Plan]:
    plans: list[Plan] = []
    for _ in range(count):
        plans.append(plan_next(catalog, records, [p.experiment_id for p in plans]))
    return plans
