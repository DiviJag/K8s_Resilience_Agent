"""Lexical retrieval over confirmed incidents and runbooks.

Kubernetes failures announce themselves in fixed phrases - "manifest unknown",
"toomanyrequests", "no such host" - so BM25 over normalised error text finds the right
precedent without an embedding model, and gives the same answer every time for the same
memory snapshot, which keeps eval runs reproducible.
"""

import math
import re
from collections import Counter
from typing import TypeVar

from chaosloop.memory.store import Incident, MemoryStore, RunbookSection

T = TypeVar("T")

_POD_SUFFIX = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}\b")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_DIGEST = re.compile(r"sha256:[0-9a-f]{12,}")
_TOKEN = re.compile(r"[a-z][a-z0-9_]+")

# Deliberately short, and without "not" or "no": in error text the negation is the signal.
_STOPWORDS = frozenset(
    "the a an and or to of in on for is are was were be been it its this that with from by as "
    "at into than then there their them they you your we our can could should would will".split()
)

MAX_INCIDENTS = 3
MAX_RUNBOOK_CHARS = 1500
RELATIVE_FLOOR = 0.2


def tokenize(text: str) -> list[str]:
    text = text.lower()
    text = _DIGEST.sub(" ", text)
    text = _POD_SUFFIX.sub(" ", text)
    text = _IP.sub(" ", text)
    return [token for token in _TOKEN.findall(text) if token not in _STOPWORDS]


class BM25Index:
    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.term_counts = [Counter(doc) for doc in documents]
        self.lengths = [len(doc) for doc in documents]
        self.average_length = (sum(self.lengths) / len(self.lengths)) if documents else 0.0
        self.document_frequency = Counter(term for doc in documents for term in set(doc))
        self.size = len(documents)

    def idf(self, term: str) -> float:
        df = self.document_frequency.get(term, 0)
        return math.log(1 + (self.size - df + 0.5) / (df + 0.5))

    def scores(self, query: list[str]) -> list[float]:
        terms = set(query)
        results = []
        for counts, length in zip(self.term_counts, self.lengths, strict=True):
            relative = length / self.average_length if self.average_length else 0.0
            norm = 1 - self.b + self.b * relative
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if tf:
                    score += self.idf(term) * tf * (self.k1 + 1) / (tf + self.k1 * norm)
            results.append(score)
        return results


def _incident_text(incident: Incident) -> str:
    # Signals are repeated so the distinguishing evidence outweighs the narrative summary.
    signals = " ".join(incident.signals)
    category = incident.category.value.replace("_", " ")
    return f"{incident.summary} {signals} {signals} {incident.root_cause} {category}"


def _section_text(section: RunbookSection) -> str:
    return f"{section.title} {section.heading} {section.body}"


class Memory:
    """Searchable view over a MemoryStore, optionally excluding incidents by source case.

    The exclusion is what makes dev-split evaluation with memory honest: a dev case promoted
    into memory must not retrieve itself while it is the case being graded.
    """

    def __init__(self, store: MemoryStore, excluded_case_ids: frozenset[str] = frozenset()) -> None:
        self.store = store
        self.snapshot = store.snapshot
        self.excluded_case_ids = excluded_case_ids
        self.incidents = [
            i for i in store.incidents if i.provenance.source_case_id not in excluded_case_ids
        ]
        self.sections = store.sections
        self._incident_index = BM25Index([tokenize(_incident_text(i)) for i in self.incidents])
        self._section_index = BM25Index([tokenize(_section_text(s)) for s in self.sections])

    def excluding(self, case_ids: set[str] | frozenset[str]) -> "Memory":
        return Memory(self.store, frozenset(case_ids))

    def search(self, query: str) -> tuple[list[tuple[Incident, float]], RunbookSection | None]:
        terms = tokenize(query)
        if not terms:
            return [], None

        incidents = _top(self.incidents, self._incident_index.scores(terms), MAX_INCIDENTS)
        sections = _top(self.sections, self._section_index.scores(terms), 1)
        return incidents, sections[0][0] if sections else None

    def search_text(self, query: str) -> str:
        incidents, section = self.search(query)
        if not incidents and section is None:
            return "No confirmed incidents or runbook sections matched this query."

        lines: list[str] = []
        if incidents:
            lines.append("Similar confirmed incidents (precedent, not proof):")
            for rank, (incident, score) in enumerate(incidents, start=1):
                lines.append(
                    f"{rank}. [{incident.incident_id}] category={incident.category.value} "
                    f"symptom={incident.symptom} (match {score:.1f})"
                )
                lines.append(f"   summary: {incident.summary}")
                lines.append("   distinguishing signals:")
                lines.extend(f"     - {signal}" for signal in incident.signals)
                lines.append(f"   root cause: {incident.root_cause}")
                lines.append(f"   resolution: {incident.resolution}")
        if section is not None:
            body = section.body
            if len(body) > MAX_RUNBOOK_CHARS:
                body = body[:MAX_RUNBOOK_CHARS].rstrip() + " ..."
            lines.append("")
            lines.append(f"Runbook - {section.title} / {section.heading}:")
            lines.append(body)
        return "\n".join(lines)


def _top(items: list[T], scores: list[float], limit: int) -> list[tuple[T, float]]:
    ranked = sorted(zip(items, scores, strict=True), key=lambda pair: pair[1], reverse=True)
    if not ranked or ranked[0][1] <= 0:
        return []
    # Drop weak matches rather than padding the answer: an irrelevant "precedent" is worse
    # than none, because the agent may anchor on it.
    floor = ranked[0][1] * RELATIVE_FLOOR
    return [(item, score) for item, score in ranked[:limit] if score > 0 and score >= floor]
