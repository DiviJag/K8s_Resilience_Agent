"""Calibration (HLD §7.3, §7.4).

A well-calibrated agent's "90% confident" answers are right about 90% of the time, which is
what later makes it safe to auto-apply fixes above a measured threshold. The model's
self-reported number is not trusted directly: confidence comes from self-consistency and is
then calibrated against dev-set outcomes with isotonic regression.
"""

import json
import math
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel


class ReliabilityBin(BaseModel):
    lower: float
    upper: float
    count: int
    mean_confidence: float | None
    accuracy: float | None

    @property
    def gap(self) -> float | None:
        if self.mean_confidence is None or self.accuracy is None:
            return None
        return self.mean_confidence - self.accuracy


def _bin_index(confidence: float, n_bins: int) -> int:
    # The epsilon keeps values that sit exactly on an edge (0.6 from 3/5) out of the bin below
    # when float error lands them at 5.9999999.
    return min(max(math.floor(confidence * n_bins + 1e-9), 0), n_bins - 1)


def _validate(confidences: Sequence[float], correct: Sequence[bool]) -> None:
    if len(confidences) != len(correct):
        raise ValueError("confidences and outcomes must be the same length")


def reliability_bins(
    confidences: Sequence[float], correct: Sequence[bool], n_bins: int = 10
) -> list[ReliabilityBin]:
    _validate(confidences, correct)
    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for confidence, outcome in zip(confidences, correct, strict=True):
        grouped[_bin_index(confidence, n_bins)].append((confidence, outcome))

    bins = []
    for index, members in enumerate(grouped):
        bins.append(
            ReliabilityBin(
                lower=index / n_bins,
                upper=(index + 1) / n_bins,
                count=len(members),
                mean_confidence=(
                    sum(c for c, _ in members) / len(members) if members else None
                ),
                accuracy=sum(o for _, o in members) / len(members) if members else None,
            )
        )
    return bins


def expected_calibration_error(
    confidences: Sequence[float], correct: Sequence[bool], n_bins: int = 10
) -> float:
    if not confidences:
        return 0.0
    total = len(confidences)
    return sum(
        (b.count / total) * abs(b.gap)
        for b in reliability_bins(confidences, correct, n_bins)
        if b.count and b.gap is not None
    )


def brier_score(confidences: Sequence[float], correct: Sequence[bool]) -> float:
    _validate(confidences, correct)
    if not confidences:
        return 0.0
    return sum(
        (c - float(o)) ** 2 for c, o in zip(confidences, correct, strict=True)
    ) / len(confidences)


def mean_gap(confidences: Sequence[float], correct: Sequence[bool]) -> float:
    """Mean confidence minus accuracy: positive means overconfident (HLD §10 calib_gap)."""
    _validate(confidences, correct)
    if not confidences:
        return 0.0
    return sum(confidences) / len(confidences) - sum(correct) / len(correct)


class IsotonicCalibrator(BaseModel):
    """Monotone map from raw confidence to observed accuracy, fit by pool-adjacent-violators.

    Written directly rather than imported: it is a dozen lines, and keeping it inspectable
    matters because the fitted points are the evidence behind any auto-remediation threshold.
    """

    x: list[float]
    y: list[float]
    n: int
    fitted_on: str

    @classmethod
    def fit(
        cls, confidences: Sequence[float], correct: Sequence[bool], fitted_on: str
    ) -> "IsotonicCalibrator":
        _validate(confidences, correct)
        if not confidences:
            raise ValueError("cannot fit a calibrator on zero outcomes")

        # Collapse ties first: self-consistency confidence is discrete (k/N), and PAV must
        # treat every outcome at the same confidence as one weighted observation.
        totals: dict[float, list[float]] = {}
        for confidence, outcome in zip(confidences, correct, strict=True):
            bucket = totals.setdefault(confidence, [0.0, 0.0])
            bucket[0] += float(outcome)
            bucket[1] += 1.0

        blocks: list[list[float]] = []  # [value, weight, x_low, x_high]
        for x in sorted(totals):
            hits, weight = totals[x]
            blocks.append([hits / weight, weight, x, x])
            while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
                value_b, weight_b, _, high_b = blocks.pop()
                value_a, weight_a, low_a, _ = blocks.pop()
                merged = weight_a + weight_b
                blocks.append(
                    [(value_a * weight_a + value_b * weight_b) / merged, merged, low_a, high_b]
                )

        xs: list[float] = []
        ys: list[float] = []
        for value, _, low, high in blocks:
            xs.append(low)
            ys.append(value)
            if high != low:
                xs.append(high)
                ys.append(value)
        return cls(x=xs, y=ys, n=len(confidences), fitted_on=fitted_on)

    def predict(self, confidence: float) -> float:
        if confidence <= self.x[0]:
            return self.y[0]
        if confidence >= self.x[-1]:
            return self.y[-1]
        for i in range(1, len(self.x)):
            if confidence <= self.x[i]:
                x0, x1, y0, y1 = self.x[i - 1], self.x[i], self.y[i - 1], self.y[i]
                if x1 == x0:
                    return y1
                return y0 + (y1 - y0) * (confidence - x0) / (x1 - x0)
        return self.y[-1]

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "IsotonicCalibrator":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
