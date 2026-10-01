"""Memory contents: confirmed incidents and runbooks (HLD §3 component 6, §6.5).

Only verified or human-confirmed incidents go in, each with provenance, and never one from the
test split - a confirmed case may go into memory or into the test set, never both. That rule
is enforced at load time, not left to convention, because a single leaked case silently turns
"memory lift" into "the answer was in memory".
"""

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from chaosloop.schemas.taxonomy import Category


class Provenance(BaseModel):
    source: Literal["golden", "prod", "manual"]
    source_case_id: str | None = None
    confirmed_by: str
    confirmed_at: date


class Incident(BaseModel):
    incident_id: str
    category: Category
    symptom: str
    summary: str
    signals: list[str] = Field(min_length=1)
    root_cause: str
    resolution: str
    provenance: Provenance


class RunbookSection(BaseModel):
    runbook_id: str
    title: str
    heading: str
    categories: list[Category]
    body: str


class MemoryLeakError(ValueError):
    pass


class MemoryStore(BaseModel):
    incidents: list[Incident]
    sections: list[RunbookSection]
    snapshot: str


def load_memory(root: Path) -> MemoryStore:
    incidents = [
        Incident.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted((root / "incidents").glob("*.yaml"))
    ]
    seen: set[str] = set()
    for incident in incidents:
        if incident.incident_id in seen:
            raise ValueError(f"duplicate incident_id {incident.incident_id!r}")
        seen.add(incident.incident_id)

    sections = [
        section
        for path in sorted((root / "runbooks").glob("*.md"))
        for section in parse_runbook(path)
    ]
    return MemoryStore(incidents=incidents, sections=sections, snapshot=memory_snapshot(root))


def assert_disjoint_from(store: MemoryStore, test_case_ids: set[str]) -> None:
    leaked = sorted(
        i.incident_id
        for i in store.incidents
        if i.provenance.source_case_id and i.provenance.source_case_id in test_case_ids
    )
    if leaked:
        raise MemoryLeakError(
            f"memory holds incidents promoted from test-split cases: {', '.join(leaked)}. "
            "A case may be in memory or in the test set, never both (HLD §6.5)."
        )


def memory_snapshot(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return f"mem:sha256:{digest.hexdigest()[:12]}"


_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.S)
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.M)


def parse_runbook(path: Path) -> list[RunbookSection]:
    """Split a runbook into its `##` sections.

    Retrieval returns sections rather than whole runbooks, so the agent gets the paragraph on
    telling auth failures from missing tags instead of three pages of ImagePullBackOff lore.
    """
    match = _FRONT_MATTER.match(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"{path}: runbook needs YAML front matter between --- lines")
    meta = yaml.safe_load(match.group(1)) or {}
    body = match.group(2)

    runbook_id = meta.get("runbook_id") or path.stem
    title = meta.get("title") or runbook_id
    categories = [Category(c) for c in meta.get("categories", [])]

    headings = list(_HEADING.finditer(body))
    if not headings:
        return [RunbookSection(runbook_id=runbook_id, title=title, heading=title,
                               categories=categories, body=body.strip())]

    sections = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        text = body[heading.end() : end].strip()
        if text:
            sections.append(
                RunbookSection(
                    runbook_id=runbook_id,
                    title=title,
                    heading=heading.group(1),
                    categories=categories,
                    body=text,
                )
            )
    return sections
