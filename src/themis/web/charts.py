"""Charts, drawn on the server as SVG.

No charting library: a few dozen lines of geometry are easier to review than a JavaScript
dependency, render before any script runs, and survive a strict content-security policy —
colour comes from CSS classes, never from a `style` attribute the policy would strip. Both
themes are the same markup; the stylesheet decides what `sev-critical` looks like.

Every chart carries a text alternative, because a chart is also a sentence a screen reader
has to be able to say.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from markupsafe import Markup, escape

from themis.web.views import SEVERITY_ORDER, DayBucket


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def trend(days: Sequence[DayBucket], *, height: int = 220) -> Markup:
    """Findings per day, stacked by severity.

    Two layers: the bars stretch to whatever width the card has (an inner SVG with no fixed
    aspect ratio), while the labels sit in the outer SVG in real pixels, so a wide monitor
    gets wider bars and not twenty-point axis text.
    """
    if not days:
        return Markup("")
    units = 1000  # horizontal units of the stretched layer
    pad_left, pad_top, pad_bottom = 40, 12, 26
    plot_h = height - pad_top - pad_bottom
    peak = max(*(d.total for d in days), 1)
    # A readable ceiling: 1, 2, 5, 10, 20, ...
    step = 10 ** max(0, int(math.log10(peak)))
    ceiling = next(m * step for m in (1, 2, 5, 10) if m * step >= peak)
    slot = (units - pad_left) / len(days)
    bar_w = max(2.0, min(40.0, slot * 0.6))

    grid: list[str] = []
    labels: list[str] = []
    for fraction in (0.0, 0.5, 1.0):
        y = pad_top + plot_h * (1 - fraction)
        grid.append(
            f'<line class="chart-grid" x1="{pad_left}" x2="{units}" y1="{_fmt(y)}" '
            f'y2="{_fmt(y)}" vector-effect="non-scaling-stroke"/>'
        )
        labels.append(
            f'<text class="chart-axis" x="0" y="{_fmt(y + 3.5)}">{_fmt(ceiling * fraction)}</text>'
        )

    bars: list[str] = []
    total = 0
    every = max(1, round(len(days) / 7))
    for i, day in enumerate(days):
        x = pad_left + slot * i + (slot - bar_w) / 2
        base = pad_top + plot_h
        stamp = escape(day.day.strftime("%d %b"))
        for severity in reversed(SEVERITY_ORDER):
            count = day.severities.get(severity, 0)
            if not count:
                continue
            h = plot_h * count / ceiling
            base -= h
            bars.append(
                f'<rect class="sev-fill-{severity}" x="{_fmt(x)}" y="{_fmt(base)}" '
                f'width="{_fmt(bar_w)}" height="{_fmt(h)}">'
                f"<title>{stamp}: {count} {severity}</title></rect>"
            )
        if day.reviews and not day.total:
            bars.append(
                f'<rect class="chart-empty" x="{_fmt(x)}" y="{_fmt(pad_top + plot_h - 2)}" '
                f'width="{_fmt(bar_w)}" height="2">'
                f"<title>{stamp}: {day.reviews} review(s), no findings</title></rect>"
            )
        total += day.total
        # A date under every few bars, never so many that they collide — including the
        # last one, which is drawn only when it has room after the previous label.
        last = i == len(days) - 1
        if i % every == 0 or (last and i % every >= max(1, every // 2 + 1)):
            centre = (x + bar_w / 2) / units * 100
            labels.append(
                f'<text class="chart-axis" x="{_fmt(centre)}%" y="{height - 6}" '
                f'text-anchor="middle">{stamp}</text>'
            )

    summary = f"{total} findings over {len(days)} days"
    return Markup(
        f'<svg class="chart" height="{height}" role="img" aria-label="{escape(summary)}">'
        f'<svg width="100%" height="{height}" viewBox="0 0 {units} {height}" '
        f'preserveAspectRatio="none">'
        + "".join(grid)
        + "".join(bars)
        + "</svg>"
        + "".join(labels)
        + "</svg>"
    )


def donut(counts: dict[str, int], *, size: int = 150, stroke: int = 18) -> Markup:
    """Severity mix as a ring, total in the middle."""
    total = sum(counts.get(s, 0) for s in SEVERITY_ORDER)
    radius = (size - stroke) / 2
    circumference = 2 * math.pi * radius
    centre = size / 2
    parts = [
        f'<circle class="chart-track" cx="{centre}" cy="{centre}" r="{_fmt(radius)}" '
        f'fill="none" stroke-width="{stroke}"/>'
    ]
    offset = 0.0
    for severity in SEVERITY_ORDER:
        count = counts.get(severity, 0)
        if not count or not total:
            continue
        length = circumference * count / total
        gap = 2.0 if count != total else 0.0
        parts.append(
            f'<circle class="sev-stroke-{severity}" cx="{centre}" cy="{centre}" '
            f'r="{_fmt(radius)}" fill="none" stroke-width="{stroke}" '
            f'stroke-dasharray="{_fmt(max(length - gap, 0.5))} {_fmt(circumference)}" '
            f'stroke-dashoffset="{_fmt(-offset)}" '
            f'transform="rotate(-90 {centre} {centre})">'
            f"<title>{count} {severity}</title></circle>"
        )
        offset += length
    parts.append(
        f'<text class="donut-total" x="{centre}" y="{centre + 2}" text-anchor="middle" '
        f'dominant-baseline="middle">{total}</text>'
        f'<text class="donut-label" x="{centre}" y="{centre + 22}" text-anchor="middle">'
        "findings</text>"
    )
    label = ", ".join(f"{counts.get(s, 0)} {s}" for s in SEVERITY_ORDER if counts.get(s))
    return Markup(
        f'<svg class="donut" viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="{escape(label or "no findings")}">' + "".join(parts) + "</svg>"
    )


def sparkline(values: Sequence[int], *, width: int = 120, height: int = 32) -> Markup:
    """A day-by-day line, ending in a dot on today."""
    if len(values) < 2:
        return Markup("")
    peak = max(*values, 1)
    step = width / (len(values) - 1)
    points = [
        (i * step, height - 3 - (height - 6) * value / peak) for i, value in enumerate(values)
    ]
    line = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in points)
    area = f"0,{height} " + line + f" {width},{height}"
    last_x, last_y = points[-1]
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" aria-hidden="true" '
        f'preserveAspectRatio="none">'
        f'<polygon class="spark-area" points="{area}"/>'
        f'<polyline class="spark-line" points="{line}" fill="none" '
        'vector-effect="non-scaling-stroke"/>'
        f'<circle class="spark-dot" cx="{_fmt(last_x)}" cy="{_fmt(last_y)}" r="2.5"/>'
        "</svg>"
    )


def meter(value: float, *, width: int = 100, height: int = 6, tone: str = "accent") -> Markup:
    """A horizontal proportion, 0 to 1, as an SVG bar — a width without a style attribute."""
    share = min(max(value, 0.0), 1.0)
    return Markup(
        f'<svg class="meter" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'aria-hidden="true"><rect class="meter-track" width="{width}" height="{height}" '
        f'rx="{height / 2}"/><rect class="meter-fill tone-{escape(tone)}" '
        f'width="{_fmt(width * share)}" height="{height}" rx="{height / 2}"/></svg>'
    )


def stacked(parts: Sequence[tuple[str, int]], *, width: int = 100, height: int = 10) -> Markup:
    """One bar split into named parts, each coloured by the class `part-<name>`."""
    total = sum(n for _, n in parts)
    if not total:
        return Markup(
            f'<svg class="stacked" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
            f'aria-hidden="true"><rect class="meter-track" width="{width}" '
            f'height="{height}"/></svg>'
        )
    x = 0.0
    rects = []
    for name, n in parts:
        if not n:
            continue
        w = width * n / total
        rects.append(
            f'<rect class="part-{escape(name)}" x="{_fmt(x)}" width="{_fmt(w)}" '
            f'height="{height}"><title>{n} {escape(name)}</title></rect>'
        )
        x += w
    label = ", ".join(f"{n} {name}" for name, n in parts if n)
    return Markup(
        f'<svg class="stacked" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'role="img" aria-label="{escape(label)}">' + "".join(rects) + "</svg>"
    )
