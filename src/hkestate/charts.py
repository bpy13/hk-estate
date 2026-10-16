"""Small static SVG charts for the findings report (no plotting library needed; matplotlib is
not installed in this environment). Light background, thin 2 px lines, recessive grid,
categorical colours in a fixed order (the dataviz reference palette), a legend for two or
more series plus direct end labels. Every chart has a table next to it in the report.
"""

from __future__ import annotations

import html
import math
from pathlib import Path

import pandas as pd

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6",
          "#e34948"]
SURFACE, TEXT, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
FONT = "font-family='Segoe UI, Helvetica, Arial, sans-serif'"


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 0.01:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _text(x, y, s, size=12, color=TEXT, anchor="start", weight="normal") -> str:
    return (f"<text x='{x:.1f}' y='{y:.1f}' {FONT} font-size='{size}' fill='{color}' "
            f"text-anchor='{anchor}' font-weight='{weight}'>{html.escape(str(s))}</text>")


def line_chart(series: dict[str, pd.Series], path: Path, title: str, ylabel: str,
               log_scale: bool = False, width: int = 760, height: int = 360) -> Path:
    """Lines over dates. `series`: name -> Series indexed by Timestamp."""
    series = {k: v.dropna() for k, v in series.items() if v is not None and len(v.dropna())}
    left, right, top, bottom = 64, 150, 44, 40
    pw, ph = width - left - right, height - top - bottom
    x0 = min(s.index.min() for s in series.values())
    x1 = max(s.index.max() for s in series.values())
    tf = (lambda v: math.log(v)) if log_scale else (lambda v: v)
    lo = min(tf(s.min()) for s in series.values())
    hi = max(tf(s.max()) for s in series.values())
    pad = (hi - lo) * 0.05 or 1
    lo, hi = lo - pad, hi + pad
    span = (x1 - x0).days or 1

    def X(t):
        return left + pw * (t - x0).days / span

    def Y(v):
        return top + ph * (1 - (tf(v) - lo) / (hi - lo))

    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
           f"viewBox='0 0 {width} {height}'>",
           f"<rect width='100%' height='100%' fill='{SURFACE}'/>",
           _text(left, 24, title, 15, TEXT, weight="600")]
    if log_scale:
        vals = [v for v in (10, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1000)
                if lo <= math.log(v) <= hi]
        ticks = vals or [math.exp(lo), math.exp(hi)]
    else:
        ticks = [t for t in _nice_ticks(lo, hi) if lo <= t <= hi]
    for t in ticks:
        y = Y(t)
        out.append(f"<line x1='{left}' x2='{left + pw}' y1='{y:.1f}' y2='{y:.1f}' "
                   f"stroke='{GRID}' stroke-width='1'/>")
        out.append(_text(left - 8, y + 4, f"{t:g}", 11, MUTED, "end"))
    for yr in range(x0.year + 1, x1.year + 1):
        if (x1.year - x0.year) > 12 and yr % 5:
            continue
        x = X(pd.Timestamp(f"{yr}-01-01"))
        out.append(_text(x, top + ph + 18, yr, 11, MUTED, "middle"))
    out.append(_text(14, top + ph / 2, ylabel, 11, MUTED, "middle")
               .replace("<text ", f"<text transform='rotate(-90 14 {top + ph / 2:.0f})' "))
    ends = []
    for i, (name, s) in enumerate(series.items()):
        color = SERIES[i % len(SERIES)]
        pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in s.items())
        out.append(f"<polyline fill='none' stroke='{color}' stroke-width='2' "
                   f"stroke-linejoin='round' points='{pts}'/>")
        ends.append([Y(s.iloc[-1]), name, color, X(s.index[-1])])
    ends.sort()
    for j in range(1, len(ends)):  # keep end labels 14 px apart
        ends[j][0] = max(ends[j][0], ends[j - 1][0] + 14)
    for y, name, color, _x in ends:
        out.append(f"<rect x='{left + pw + 8}' y='{y - 5:.1f}' width='10' height='3' "
                   f"rx='1.5' fill='{color}'/>")
        out.append(_text(left + pw + 22, y + 4, name, 11, TEXT))
    out.append("</svg>")
    path.write_text("\n".join(out), "utf-8")
    return path


def dot_ci_chart(rows: pd.DataFrame, path: Path, title: str, groups: list[str],
                 width: int = 760) -> Path:
    """Estimates with 95% intervals, one row per attribute, one colour per group.
    `rows`: columns label, group, est, lo, hi (in % a year)."""
    labels = list(dict.fromkeys(rows["label"]))
    left, right, top, row_h = 330, 30, 64, 26
    height = top + row_h * len(labels) + 40
    pw = width - left - right
    lo = min(rows["lo"].min(), 0)
    hi = max(rows["hi"].max(), 0)
    pad = (hi - lo) * 0.05 or 0.5
    lo, hi = lo - pad, hi + pad

    def X(v):
        return left + pw * (v - lo) / (hi - lo)

    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
           f"viewBox='0 0 {width} {height}'>",
           f"<rect width='100%' height='100%' fill='{SURFACE}'/>",
           _text(16, 24, title, 15, TEXT, weight="600")]
    for i, g in enumerate(groups):  # legend
        x = 16 + i * 220
        out.append(f"<circle cx='{x + 5}' cy='44' r='5' fill='{SERIES[i]}'/>")
        out.append(_text(x + 16, 48, g, 12, TEXT))
    for t in _nice_ticks(lo, hi):
        if lo <= t <= hi:
            x = X(t)
            color = MUTED if abs(t) < 1e-12 else GRID
            out.append(f"<line x1='{x:.1f}' x2='{x:.1f}' y1='{top - 6}' "
                       f"y2='{height - 34}' stroke='{color}' stroke-width='1'/>")
            out.append(_text(x, height - 18, f"{t:+g}", 11, MUTED, "middle"))
    out.append(_text(left + pw / 2, height - 4, "percentage points a year (95% interval)",
                     11, MUTED, "middle"))
    for r, lab in enumerate(labels):
        yb = top + r * row_h + row_h / 2
        out.append(_text(left - 10, yb + 4, lab, 12, TEXT, "end"))
        sub = rows[rows["label"] == lab]
        for _, row in sub.iterrows():
            gi = groups.index(row["group"])
            y = yb + (gi - (len(groups) - 1) / 2) * 8
            c = SERIES[gi]
            out.append(f"<line x1='{X(row['lo']):.1f}' x2='{X(row['hi']):.1f}' y1='{y:.1f}' "
                       f"y2='{y:.1f}' stroke='{c}' stroke-width='2' stroke-linecap='round'/>")
            out.append(f"<circle cx='{X(row['est']):.1f}' cy='{y:.1f}' r='4.5' fill='{c}' "
                       f"stroke='{SURFACE}' stroke-width='2'/>")
    out.append("</svg>")
    path.write_text("\n".join(out), "utf-8")
    return path
