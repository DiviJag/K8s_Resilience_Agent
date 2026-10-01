"""Reliability diagram, rendered server-side as SVG (HLD §3 component 8, §7.3).

Each confidence source is plotted as observed accuracy against mean confidence per bin; the
diagonal is perfect calibration. Points below it are overconfident. Colours come from CSS
tokens so light and dark themes swap in one place; identity is also carried by marker shape
and direct labels, never by colour alone.
"""

import math
from dataclasses import dataclass
from html import escape

from chaosloop.eval.calibration import ReliabilityBin, reliability_bins
from chaosloop.eval.harness import CONFIDENCE_SOURCES
from chaosloop.schemas.eval_record import EvalRecord


@dataclass(frozen=True)
class SeriesStyle:
    source: str
    label: str
    color_var: str
    shape: str


# Three slots at most: the first three palette slots are the only ones validated for every
# pair, which a dot chart needs because any two series can sit next to each other.
SERIES = (
    SeriesStyle("self_reported", "Self-reported", "--series-1", "circle"),
    SeriesStyle("consistency", "Self-consistency", "--series-2", "square"),
    SeriesStyle("calibrated", "Calibrated", "--series-3", "diamond"),
)


@dataclass(frozen=True)
class SeriesBins:
    style: SeriesStyle
    bins: list[ReliabilityBin]

    @property
    def points(self) -> list[ReliabilityBin]:
        return [b for b in self.bins if b.count]


# Runs recorded before confidence was split into sources carry only the combined value.
REPORTED = SeriesStyle("system", "Reported confidence", "--series-1", "circle")


def reliability_series(records: list[EvalRecord], n_bins: int = 10) -> list[SeriesBins]:
    series = _series_for(records, SERIES, n_bins)
    return series or _series_for(records, (REPORTED,), n_bins)


def _series_for(
    records: list[EvalRecord], styles: tuple[SeriesStyle, ...], n_bins: int
) -> list[SeriesBins]:
    series = []
    for style in styles:
        field = CONFIDENCE_SOURCES[style.source]
        pairs = [
            (float(getattr(r, field)), r.category_correct)
            for r in records
            if getattr(r, field) is not None
        ]
        if pairs:
            bins = reliability_bins([c for c, _ in pairs], [o for _, o in pairs], n_bins)
            series.append(SeriesBins(style, bins))
    return series


WIDTH, HEIGHT = 640, 420
# Right margin sized for the longest end label ("Reported confidence", ~125px at 12px) plus its
# line key, so no label is ever clipped by the viewBox.
LEFT, RIGHT, TOP, BOTTOM = 56, 176, 20, 52
PLOT_W, PLOT_H = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
TICKS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
LABEL_GAP = 16


def _x(value: float) -> float:
    return LEFT + value * PLOT_W


def _y(value: float) -> float:
    return TOP + (1 - value) * PLOT_H


def _marker(shape: str, cx: float, cy: float, color_var: str) -> str:
    style = f'fill="var({color_var})" stroke="var(--surface-1)" stroke-width="2"'
    if shape == "square":
        return f'<rect x="{cx - 4.5:.1f}" y="{cy - 4.5:.1f}" width="9" height="9" rx="1" {style}/>'
    if shape == "diamond":
        corners = ((cx, cy - 6), (cx + 6, cy), (cx, cy + 6), (cx - 6, cy))
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in corners)
        return f'<polygon points="{points}" {style}/>'
    return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" {style}/>'


def reliability_svg(series: list[SeriesBins]) -> str:
    parts = [
        f'<svg class="reliability" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        'aria-labelledby="reliability-title" xmlns="http://www.w3.org/2000/svg">',
        '<title id="reliability-title">Reliability diagram: observed accuracy against '
        "stated confidence, per confidence source</title>",
    ]

    for tick in TICKS:
        parts.append(
            f'<line x1="{LEFT}" x2="{LEFT + PLOT_W}" y1="{_y(tick):.1f}" y2="{_y(tick):.1f}" '
            'class="grid"/>'
        )
        parts.append(
            f'<line y1="{TOP}" y2="{TOP + PLOT_H}" x1="{_x(tick):.1f}" x2="{_x(tick):.1f}" '
            'class="grid"/>'
        )
        parts.append(
            f'<text x="{LEFT - 8}" y="{_y(tick) + 4:.1f}" class="tick" text-anchor="end">'
            f"{tick:.1f}</text>"
        )
        parts.append(
            f'<text x="{_x(tick):.1f}" y="{TOP + PLOT_H + 18}" class="tick" '
            f'text-anchor="middle">{tick:.1f}</text>'
        )
    parts.append(
        f'<line x1="{LEFT}" x2="{LEFT + PLOT_W}" y1="{TOP + PLOT_H}" y2="{TOP + PLOT_H}" '
        'class="axis"/>'
    )
    parts.append(f'<line x1="{LEFT}" x2="{LEFT}" y1="{TOP}" y2="{TOP + PLOT_H}" class="axis"/>')
    parts.append(
        f'<text x="{LEFT + PLOT_W / 2:.1f}" y="{HEIGHT - 10}" class="axis-label" '
        'text-anchor="middle">Stated confidence</text>'
    )
    parts.append(
        f'<text transform="translate(14 {TOP + PLOT_H / 2:.1f}) rotate(-90)" '
        'class="axis-label" text-anchor="middle">Observed accuracy</text>'
    )

    parts.append(
        f'<line x1="{_x(0):.1f}" y1="{_y(0):.1f}" x2="{_x(1):.1f}" y2="{_y(1):.1f}" '
        'class="diagonal"/>'
    )
    # Placed low on the diagonal: agents state high confidence, so the upper end is where the
    # data points crowd, and a label there would collide with them.
    label_x, label_y = _x(0.22), _y(0.22) - 8
    angle = -math.degrees(math.atan2(PLOT_H, PLOT_W))
    parts.append(
        f'<text x="{label_x:.1f}" y="{label_y:.1f}" class="diagonal-label" '
        f'transform="rotate({angle:.1f} {label_x:.1f} {label_y:.1f})">perfect calibration</text>'
    )

    for entry in series:
        points = sorted(entry.points, key=lambda b: b.mean_confidence or 0)
        if len(points) > 1:
            coords = " ".join(
                f"{_x(b.mean_confidence or 0):.1f},{_y(b.accuracy or 0):.1f}" for b in points
            )
            parts.append(
                f'<polyline points="{coords}" fill="none" stroke="var({entry.style.color_var})" '
                'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            )

    for entry in series:
        for b in entry.points:
            cx, cy = _x(b.mean_confidence or 0), _y(b.accuracy or 0)
            parts.append(_marker(entry.style.shape, cx, cy, entry.style.color_var))
            # Transparent hit area well beyond the painted mark: a 10px dot is a pinpoint.
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="12" class="hit" tabindex="0" '
                f'data-series="{escape(entry.style.label)}" '
                f'data-color-var="{entry.style.color_var}" '
                f'data-accuracy="{b.accuracy:.2f}" data-confidence="{b.mean_confidence:.2f}" '
                f'data-count="{b.count}" data-bin="{b.lower:.1f}-{b.upper:.1f}">'
                f"<title>{escape(entry.style.label)}: accuracy {b.accuracy:.2f} at "
                f"confidence {b.mean_confidence:.2f} ({b.count} runs)</title></circle>"
            )

    parts.extend(_end_labels(series))
    parts.append("</svg>")
    return "".join(parts)


def _end_labels(series: list[SeriesBins]) -> list[str]:
    """Label each series at its rightmost point, using leader lines rather than stacking.

    Labels sit in the right margin; when two would collide they are spread apart and joined to
    their points by a hairline, so each label stays attached to the series it names.
    """
    anchors = []
    for entry in series:
        points = entry.points
        if not points:
            continue
        last = max(points, key=lambda b: b.mean_confidence or 0)
        anchors.append(
            (_y(last.accuracy or 0), _x(last.mean_confidence or 0), entry.style)
        )
    anchors.sort(key=lambda a: a[0])

    placed: list[float] = []
    for target_y, _, _ in anchors:
        y = target_y if not placed else max(target_y, placed[-1] + LABEL_GAP)
        placed.append(y)
    overflow = (placed[-1] - (TOP + PLOT_H)) if placed else 0
    if overflow > 0:
        placed = [y - overflow for y in placed]

    label_x = LEFT + PLOT_W + 24
    parts = []
    for (target_y, point_x, style), y in zip(anchors, placed, strict=True):
        parts.append(
            f'<line x1="{point_x + 8:.1f}" y1="{target_y:.1f}" x2="{label_x - 4:.1f}" '
            f'y2="{y:.1f}" class="leader"/>'
        )
        parts.append(
            f'<line x1="{label_x:.1f}" x2="{label_x + 12:.1f}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="var({style.color_var})" stroke-width="2" stroke-linecap="round"/>'
        )
        parts.append(
            f'<text x="{label_x + 18:.1f}" y="{y + 4:.1f}" class="end-label">'
            f"{escape(style.label)}</text>"
        )
    return parts
