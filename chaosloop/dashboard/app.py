"""Local dashboard: runs, scoreboard, reliability diagram, traces, run comparison (HLD §3/8).

Read-only over the eval run files. Every page renders through autoescaped templates, because
the traces it shows are full of cluster logs - attacker-influenced text, by design.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from chaosloop.dashboard.charts import reliability_series, reliability_svg
from chaosloop.dashboard.data import RunStore
from chaosloop.eval.harness import (
    Metrics,
    Thresholds,
    check_thresholds,
    differing_axes,
    is_graded,
    is_provider_error,
    load_thresholds,
)
from chaosloop.schemas.eval_record import EvalRecord

HERE = Path(__file__).parent


def _ece(metrics: Metrics) -> float | None:
    system = metrics.calibration.get("system")
    return system.ece if system else None


def tile_gates(metrics: Metrics, thresholds: Thresholds | None) -> dict[str, dict[str, Any]]:
    """Per-tile verdict against the CI gate, so tile colour reports something real."""
    if thresholds is None or not metrics.n_scored:
        return {}
    checks = {
        "category": (metrics.category_accuracy, thresholds.category_accuracy_min, "min", True),
        "fix": (metrics.fix_accuracy, thresholds.fix_accuracy_min, "min", True),
        "false_positive": (
            metrics.false_positive_rate, thresholds.false_positive_rate_max, "max", True
        ),
        "ece": (_ece(metrics), thresholds.ece_max, "max", False),
    }
    gates = {}
    for key, (value, limit, kind, as_percent) in checks.items():
        if value is None:
            continue
        ok = value >= limit if kind == "min" else value <= limit
        shown = f"{limit * 100:.0f}%" if as_percent else f"{limit:.2f}"
        gates[key] = {"ok": ok, "rule": f"{'≥' if kind == 'min' else '≤'} {shown}"}
    return gates


# (label, attribute, higher is better, decimal places)
COMPARED_METRICS = (
    ("Category accuracy", "category_accuracy", True, 3),
    ("Fix accuracy", "fix_accuracy", True, 3),
    ("Root-cause score", "root_cause_score", True, 3),
    ("False-positive rate", "false_positive_rate", False, 3),
    ("Calibration error (ECE)", None, False, 3),
    ("Cost per case ($)", "mean_cost_usd", False, 4),
)


def _direction(delta: float | None, higher_is_better: bool) -> str:
    if delta is None or abs(delta) < 1e-9:
        return "same"
    return "better" if (delta > 0) == higher_is_better else "worse"


def compare_rows(before: Metrics, after: Metrics) -> list[dict[str, Any]]:
    rows = []
    for label, attribute, higher_is_better, places in COMPARED_METRICS:
        a = getattr(before, attribute) if attribute else _ece(before)
        b = getattr(after, attribute) if attribute else _ece(after)
        delta = b - a if a is not None and b is not None else None
        rows.append(
            {
                "label": label,
                "before": a,
                "after": b,
                "delta": delta,
                "places": places,
                "direction": _direction(delta, higher_is_better),
            }
        )
    return rows


def _num(value: float | None, places: int = 2) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _signed(value: float | None, places: int = 2) -> str:
    return "-" if value is None else f"{value:+.{places}f}"


def _secs(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f} s"


def _since(end: float | None, start: float | None) -> float | None:
    return end - start if end is not None and start is not None else None


def _fix_outcome(record: EvalRecord) -> str:
    if record.fix_rejected:
        return "rejected"
    if record.fix_verified is None:
        return "-"
    return "recovered" if record.fix_verified else "not recovered"


def _outcome(record: EvalRecord) -> str:
    if record.aborted:
        return "aborted"
    if record.detected is False:
        return "missed"
    if is_provider_error(record):
        return "error"
    if record.malformed:
        return "malformed"
    return "pass" if record.category_correct else "fail"


def trace_steps(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Flatten a chat transcript into display steps."""
    steps: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, indent=2)
        if role == "system":
            steps.append({"kind": "system", "title": "System prompt", "body": text or ""})
        elif role == "user":
            steps.append({"kind": "user", "title": "Input", "body": text or ""})
        elif role == "tool":
            steps.append(
                {"kind": "tool", "title": f"Result: {message.get('name', 'tool')}", "body": text}
            )
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                steps.append(
                    {
                        "kind": "call",
                        "title": f"Calls {function.get('name', '?')}",
                        "body": function.get("arguments") or "{}",
                    }
                )
            if text and text.strip():
                steps.append({"kind": "answer", "title": "Answer", "body": text})
    return steps


def create_app(runs_dir: Path, thresholds_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="ChaosLoop", docs_url=None, redoc_url=None, openapi_url=None)
    store = RunStore(runs_dir)
    # Without a thresholds file there is no gate to report against, so no tile is coloured
    # pass or fail - better silent than measured against permissive defaults.
    thresholds = (
        load_thresholds(thresholds_path)
        if thresholds_path is not None and thresholds_path.is_file()
        else None
    )
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(num=_num, pct=_pct, signed=_signed, secs=_secs)
    templates.env.globals.update(since=_since, fix_outcome=_fix_outcome)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html", {"runs": store.list_runs()})

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str) -> HTMLResponse:
        loaded = store.load_run(run_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="no such run")
        summary, records = loaded
        series = reliability_series([r for r in records if is_graded(r)])
        # The CI gate is defined on the golden test split; a live run is a different population.
        gate = thresholds if "live" not in summary.splits else None
        cases_per_category = {
            category: len({r.case_id for r in records if r.expected_category.value == category})
            for category in summary.metrics.by_category
        }
        return templates.TemplateResponse(
            request,
            "run.html",
            {
                "summary": summary,
                "metrics": summary.metrics,
                "records": sorted(records, key=lambda r: (r.case_id, r.repeat)),
                "outcome": _outcome,
                "series": series,
                "chart": Markup(reliability_svg(series)) if series else None,
                "cases_per_category": cases_per_category,
                "gates": tile_gates(summary.metrics, gate),
                "violations": check_thresholds(summary.metrics, gate) if gate else None,
            },
        )

    @app.get("/runs/{run_id}/cases/{case_id}/{repeat}", response_class=HTMLResponse)
    def trace_page(request: Request, run_id: str, case_id: str, repeat: int) -> HTMLResponse:
        loaded = store.load_run(run_id)
        records = loaded[1] if loaded else []
        record = next(
            (r for r in records if r.case_id == case_id and r.repeat == repeat), None
        )
        if loaded is None or record is None:
            raise HTTPException(status_code=404, detail="no such case run")
        trace = store.load_trace(run_id, case_id, repeat)
        return templates.TemplateResponse(
            request,
            "trace.html",
            {
                "summary": loaded[0],
                "record": record,
                "outcome": _outcome(record),
                "trace": trace,
                "steps": trace_steps(trace.get("messages", [])) if trace else [],
            },
        )

    @app.get("/compare", response_class=HTMLResponse)
    def compare_page(request: Request, baseline: str, candidate: str) -> HTMLResponse:
        base, cand = store.load_run(baseline), store.load_run(candidate)
        if base is None or cand is None:
            raise HTTPException(status_code=404, detail="no such run")
        b, c = base[0].metrics, cand[0].metrics
        categories = sorted(set(b.by_category) | set(c.by_category))
        rows = []
        for category in categories:
            before = (b.by_category.get(category) or {}).get("category_accuracy")
            after = (c.by_category.get(category) or {}).get("category_accuracy")
            delta = after - before if before is not None and after is not None else None
            rows.append(
                {
                    "category": category,
                    "before": before,
                    "after": after,
                    "delta": delta,
                    "direction": _direction(delta, higher_is_better=True),
                }
            )
        return templates.TemplateResponse(
            request,
            "compare.html",
            {
                "baseline": base[0],
                "candidate": cand[0],
                "is_lift": base[0].memory_snapshot is None
                and cand[0].memory_snapshot is not None,
                "same_cases": {r.case_id for r in base[1]} == {r.case_id for r in cand[1]},
                "mismatches": differing_axes(base[1], cand[1]),
                "metric_rows": compare_rows(b, c),
                "rows": rows,
            },
        )

    return app
