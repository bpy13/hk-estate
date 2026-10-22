"""Findings report: `hkestate report` writes data/reports/findings_<date>.md with charts in
data/reports/findings_<date>/. Generated from `study.run_settings` (several settings from one
snapshot) or `study.run` (one window); never committed (the repo is public and the report
holds the data).

Order: a plain-language summary first (the owner relays it to non-technical people), then
coverage, method, the verdicts by attribute and market phase (flats sold twice are the main
evidence), the model-valued cross-check, the short-hold sensitivity, returns by attribute,
districts, indices, first-hand vs second-hand, checks, limits.

Words (decisions.md "Report wording"): the code says "pairs"; text people read says "flats
sold twice" (recorded: real + Ricacorp), "model-valued flats" (estimated) and "Ricacorp
reported gains". `plain_words` translates the study's own labels and notes.
"""

from __future__ import annotations

import math
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from hkestate import charts, growth
from hkestate.config import get_data_dir
from hkestate.study import DISTRICT_GROUPS, ESTIMATED_INDEX_WINDOW, MIN_PAIRS, Study, district_label

TYPE_NAMES = {
    "real": "Flats with two recorded sales",
    "ricacorp": "Ricacorp reported gains (previous sale from Ricacorp's days-held and % gain)",
    "recorded": "Flats sold twice (recorded sales: the two kinds above combined)",
    "estimated": "Model-valued flats (sale before 2018 vs the model's value today)",
}
# short names for sentences like "return to an owner (model-valued, median)"
KIND = {"recorded": "sold twice", "estimated": "model-valued",
        "real": "two recorded sales", "ricacorp": "Ricacorp reported gains"}

# the study's own labels and notes (study.py, not edited here) still say "pairs"
_PLAIN = [
    (r"estimated-pair candidates", "model-valued flat candidates"),
    (r"estimated pairs", "model-valued flats"),
    (r"Ricacorp pairs", "Ricacorp reported gains"),
    (r"recorded pairs", "flats sold twice"),
    (r"Pairs", "Flats sold twice"),
    (r"pairs", "flats sold twice"),
    (r"pair", "flat sold twice"),
]


def plain_words(text: str | None) -> str | None:
    """`text` with "pair(s)" in the words the owner uses; backticked names stay as they are."""
    if not text:
        return text
    out = []
    for part in re.split(r"(`[^`]*`)", text):
        if not part.startswith("`"):
            for pattern, new in _PLAIN:
                part = re.sub(rf"\b{pattern}\b", new, part)
        out.append(part)
    return "".join(out)

# the market-phase settings (study.STANDARD) and how the summary names them
PHASES = {
    "crash-1997-2003": "the 1997-2003 crash", "recovery-2003-2008": "the 2003-2008 recovery",
    "boom-2008-2021": "the 2008-2021 boom", "fall-2021-2024": "the 2021-2024 fall",
    "recent-2024-now": "the market since late 2024",
}
FALLING = {"crash-1997-2003", "fall-2021-2024"}
RISING = {"recovery-2003-2008", "boom-2008-2021", "recent-2024-now"}


def _pct(x, digits=1, sign=False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)) or pd.isna(x):
        return "–"
    return f"{x:+.{digits}%}" if sign else f"{x:.{digits}%}"


def _pp(x) -> str:
    """An annual log-growth difference as percentage points a year."""
    return "–" if pd.isna(x) else f"{100 * x:+.2f}"


def _n(x) -> str:
    return "–" if pd.isna(x) else f"{int(x):,}"


def _table(df: pd.DataFrame) -> list[str]:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return lines


def _plain_effect(coef: float) -> str:
    way = "faster" if coef > 0 else "slower"
    return f"about {abs(100 * coef):.1f} percentage points a year {way}"


SHORT = {
    "size_small": "small flats (under 430 sq ft)", "size_large": "large flats (753-1,075 sq ft)",
    "size_xl": "very large flats (1,076+ sq ft)", "floor_low": "low floors",
    "floor_high": "high floors", "mtr_near": "flats within 500 m of an MTR station",
    "mtr_far": "flats over 1 km from an MTR station", "big_estate": "flats in large estates",
    "special_unit": "special units (roof, duplex, top floor)",
    "facing_south": "south-facing flats", "sea_view": "sea-view flats",
    "first_hand_start": "flats bought from the developer",
}


# ---- the attribute x setting matrix ------------------------------------------------------------


def cell(s: Study | None, attr: str, key: str = "recorded") -> dict | None:
    """One matrix cell: the effect and its verdict for `attr` in setting `s`, or None when
    the setting had too few pairs for a verdict (or never tested the attribute)."""
    if s is None:
        return None
    v = s.verdicts.get(key)
    if v is None or v.empty or attr not in v.index:
        return None
    r = v.loc[attr]
    return {"coef": float(r["coef"]), "robust": bool(r["robust"]),
            "significant": bool(r["significant"]), "p": float(r["p"])}


def cell_text(c: dict | None) -> str:
    """'**+2.20**' robust, '+0.61' significant but not robust, '+0.03 n.s.', '–' no verdict."""
    if c is None:
        return "–"
    if c["robust"]:
        return f"**{_pp(c['coef'])}**"
    return _pp(c["coef"]) if c["significant"] else f"{_pp(c['coef'])} n.s."


def _way(coef: float) -> str:
    return "faster" if coef > 0 else "slower"


def phase_reading(attr: str, cells: dict[str, dict | None], phases: list[str] | None = None
                  ) -> str:
    """A plain-language line for one attribute across the market phases: which phases it
    holds in, as a reader would ask ("helps in rising and falling markets", "only in the
    2008-2021 boom", ...). `cells`: setting name -> `cell` (None = no verdict there);
    `phases`: the phase settings to read, in order (default: every PHASES key present)."""
    phases = [p for p in (phases or list(PHASES)) if p in cells]
    tested = [p for p in phases if cells[p] is not None]
    base = cells.get("all")
    short = SHORT.get(attr, attr)
    head = f"**{short[0].upper() + short[1:]}.**"  # not .capitalize(): it lowers "MTR"
    if base is not None:
        head += (f" All data: {_pp(base['coef'])} pp a year, "
                 + ("robust" if base["robust"] else
                    "significant but not robust" if base["significant"] else
                    "not significant") + ".")
    if not tested:
        return head + " No market phase had enough flats sold twice to test it on its own."
    name = {p: PHASES.get(p, p) for p in phases}

    def with_effect(ps):
        return ", ".join(f"{name[p]} ({_pp(cells[p]['coef'])})" for p in ps)

    # the direction to judge against: the all-data effect, else the first robust phase
    any_robust = [p for p in tested if cells[p]["robust"]]
    sign = np.sign(base["coef"]) if base else np.sign(
        cells[(any_robust or tested)[0]]["coef"])
    same = [p for p in tested if np.sign(cells[p]["coef"]) == sign]
    robust_in = [p for p in same if cells[p]["robust"]]
    sig_same = [p for p in same if cells[p]["significant"]]
    if robust_in and len(robust_in) == len(tested) and len(same) == len(tested):
        kinds = {"falling" for p in tested if p in FALLING} | {"rising" for p in tested
                                                                 if p in RISING}
        where = ("in rising and falling markets" if kinds == {"rising", "falling"}
                 else f"in {'rising' if kinds == {'rising'} else 'falling'} markets only, the "
                      "only kind with enough flats sold twice")
        return (f"{head} Grew {_way(sign)} in every market phase tested: "
                f"{with_effect(tested)}. Holds {where}.")
    if robust_in:
        rest = [p for p in tested if p not in robust_in]
        parts = []
        for p in rest:
            c = cells[p]
            if np.sign(c["coef"]) != sign and c["significant"]:
                parts.append(f"the other way in {name[p]} ({_pp(c['coef'])})")
            elif c["significant"]:
                parts.append(f"significant but not robust in {name[p]} ({_pp(c['coef'])})")
            else:
                parts.append(f"not significant in {name[p]}")
        return (f"{head} Robust only in {with_effect(robust_in)}"
                + (f"; {'; '.join(parts)}" if parts else "") + ".")
    if len(sig_same) == len(tested):
        return (f"{head} Points the same way ({_way(sign)}) in every phase tested, "
                f"{with_effect(tested)}, but fails an out-of-sample check in each: a weak, "
                "consistent lean rather than a proven effect.")
    if not any(cells[p]["significant"] for p in tested):
        return (f"{head} Not significant in any phase tested "
                f"({', '.join(name[p] for p in tested)}): no phase-level effect found.")
    flips = [p for p in tested if p not in same and cells[p]["significant"]]
    return (f"{head} No consistent effect across phases: {with_effect(tested)}"
            + (f"; it points the other way in {', '.join(name[p] for p in flips)}" if flips
               else "") + ".")


def matrix_lines(main: list[Study], key: str = "recorded") -> list[str]:
    """The attribute x setting table plus a reading per attribute."""
    attrs = [a for a in growth.ATTRIBUTES
             if any(cell(s, a, key) is not None for s in main)]
    if not attrs:
        return ["No setting had enough flats sold twice for a verdict.", ""]
    rows = []
    for a in attrs:
        row = {"Attribute": SHORT[a]}
        for s in main:
            row[s.setting] = cell_text(cell(s, a, key))
        rows.append(row)
    lines = _table(pd.DataFrame(rows)) + ["",
             "Effects in percentage points of growth a year, like for like (same district, "
             "purchase year, price level and building age). **Bold** = robust (p < 0.05 and "
             "the same sign in both halves of a time split and of a district split); plain = "
             "significant but failed a split; n.s. = not significant; – = too few flats sold "
             "twice for a verdict there.", ""]
    phases = [s.setting for s in main if s.setting in PHASES]
    if phases:
        lines += ["**How to read each attribute across the market phases:**", ""]
        for a in attrs:
            cells = {s.setting: cell(s, a, key) for s in main}
            lines.append(f"- {phase_reading(a, cells, phases)}")
        lines.append("")
    return lines


def holds_everywhere(main: list[Study], key: str = "recorded"
                     ) -> tuple[list, list, list, list]:
    """(attributes robust the same way in every phase tested, robust in every phase but
    not always the same way, robust in some phases, robust in none), among the phase
    settings with a verdict."""
    phases = [s for s in main if s.setting in PHASES and s.enough and key in s.verdicts
              and len(s.verdicts[key])]
    every, mixed, some, none = [], [], [], []
    for a in growth.ATTRIBUTES:
        cells = [cell(s, a, key) for s in phases]
        cells = [c for c in cells if c is not None]
        if not cells:
            continue
        rob = [c for c in cells if c["robust"]]
        signs = {np.sign(c["coef"]) for c in rob}
        if rob and len(rob) == len(cells) and len(signs) == 1:
            every.append((a, rob[0]["coef"], len(cells)))
        elif rob and len(rob) == len(cells):
            mixed.append((a, len(cells)))
        elif rob:
            some.append((a, len(rob), len(cells)))
        else:
            none.append(a)
    return every, mixed, some, none


# ---- sections ----------------------------------------------------------------------------------


def _summary(s: Study, main: list[Study], sens: list[Study]) -> list[str]:
    cov = s.coverage
    rec = s.frames.get("recorded")
    est = s.frames.get("estimated")
    lines = ["## Summary in plain language", ""]
    lt = s.rvd_long
    if not lt.empty:
        allp = lt[(lt["series"] == "Price index") & (lt["class"] == "ALL")]
        if not allp.empty:
            r = allp.iloc[0]
            lines.append(
                f"- **The market as a whole.** By the government's price index (RVD), Hong "
                f"Kong flat prices rose about {_pct(r['ann_growth'])} a year from "
                f"{r['from']:%Y} to {r['to']:%Y}, but fell {_pct(r['drawdown'], 0)} from "
                f"their {r['peak']:%Y} peak to the {r['trough']:%Y} low on the way. Every size "
                f"of flat fell by roughly the same share in that crash.")
    dd = s.drawdowns
    if not dd.empty:
        r = dd[(dd["pairs_type"] == "recorded") & (dd["group"] == "all")]
        if not r.empty and pd.notna(r.iloc[0]["drawdown"]):
            r = r.iloc[0]
            lines.append(
                f"- **Worst fall in our own resale prices:** prices fell "
                f"{_pct(r['drawdown'], 0)} from {r['peak']:%B %Y} to {r['trough']:%B %Y}.")
    v = s.verdicts.get("recorded")
    if v is not None and not v.empty:
        rob = v[v["robust"]]
        who = "flats sold twice (two real prices for the same flat, all years)"
        if rob.empty:
            lines.append(f"- **Attributes, {who}:** no attribute passed every check.")
        else:
            parts = [f"{SHORT[a]} grew {_plain_effect(c)}" for a, c in rob["coef"].items()]
            lines.append(f"- **Attributes that held up, {who}:** " + "; ".join(parts) +
                         " (compared with similar flats in the same district, bought in the same "
                         "year, at a similar price and building age).")
        weak = v[v["significant"] & ~v["robust"]]
        if not weak.empty:
            lines.append(f"  Looked significant but failed an out-of-sample check: "
                         f"{', '.join(SHORT[a] for a in weak.index)}.")
    phases = [m for m in main if m.setting in PHASES]
    if phases:
        tested = [m for m in phases if m.enough]
        if tested:
            every, mixed, some, none = holds_everywhere(main)
            text = (f"- **Across market phases** ({len(tested)} of {len(phases)} phases had "
                    "enough flats sold twice: "
                    + ", ".join(PHASES[m.setting] for m in tested) + "). ")
            if every:
                text += ("Held the same way in every phase tested: " + "; ".join(
                    f"{SHORT[a]} ({_way(c)}, {n} phases)" for a, c, n in every) + ". ")
            if mixed:
                text += ("Clear in every phase but not always the same way: " + "; ".join(
                    f"{SHORT[a]} ({n} phases)" for a, n in mixed) + ". ")
            if some:
                text += ("Held only in some: " + "; ".join(
                    f"{SHORT[a]} ({k} of {n})" for a, k, n in some) + ". ")
            if none:
                text += "Robust in no phase: " + ", ".join(SHORT[a] for a in none) + "."
            lines.append(text + " The table in \"Verdicts by attribute and market phase\" has "
                         "the effect in each phase and a line per attribute.")
        else:
            lines.append(f"- **Across market phases:** none of the {len(phases)} phases run "
                         "had enough flats sold twice (held 3+ years) for a verdict of its own.")
        short = [m for m in phases if not m.enough]
        if short:
            lines.append("  Not tested: " + "; ".join(
                f"{PHASES[m.setting]} has {len(m.pairs['recorded']):,} "
                "flat" + ("" if len(m.pairs["recorded"]) == 1 else "s") + " sold twice, held "
                "3+ years" for m in short) + ": too few for a verdict (a phase shorter than "
                "about four years holds almost no flats sold twice after a 3-year hold).")
    cross = crosscheck_line(s)
    if cross:
        lines.append(cross)
    for m in sens:
        n = len(m.pairs["recorded"])
        v = m.verdicts.get("recorded")
        if v is not None and len(v):
            rob = [SHORT[a] for a in v.index if v.loc[a, "robust"]]
            verdict = ("robust: " + ", ".join(rob)) if rob else "no attribute robust"
        else:
            verdict = "too few for a verdict"
        what = plain_words(m.label or m.setting).removeprefix("Sensitivity: ")
        lines.append(f"- **Short-hold sensitivity (`{m.setting}`):** {what}: {n:,} flats "
                     f"sold twice, held {m.min_hold_years:g}+ years; {verdict}. A side-check only "
                     "(see its own section); not part of the main verdicts.")
    fh = s.first_hand
    if not fh.empty:
        for k in ("recorded", "estimated"):
            part = fh[fh["pairs_type"] == k].set_index("start")
            if {"first-hand", "second-hand"} <= set(part.index):
                lines.append(
                    f"- **First-hand vs second-hand ({KIND[k]}):** flats bought from the "
                    f"developer grew {_pct(part.loc['first-hand', 'growth'])} a year (median) "
                    f"vs {_pct(part.loc['second-hand', 'growth'])} for resales.")
    for k, f in (("recorded", rec), ("estimated", est)):
        if f is not None and "total_return" in f:
            lines.append(
                f"- **Return to an owner ({KIND[k]}, median):** price growth "
                f"{_pct(f['ann_growth'].median())} a year; after buying and selling costs "
                f"(stamp duty for a buyer who owns another flat, agents, lawyers) "
                f"{_pct(f['net_ann_growth'].median())}; plus rent of about "
                f"{_pct(f['rental_yield'].median())} a year gives a total of "
                f"{_pct(f['total_return'].median())} a year (before rates, management fees "
                f"and tax; no mortgage).")
    if not s.districts.empty:
        d = s.districts[(s.districts["pairs_type"] == "recorded")
                        & (s.districts["district"] != "unknown")]
        d = d[d["pairs"] >= MIN_PAIRS].sort_values("growth")
        if len(d) >= 2:
            lo, hi = d.iloc[0], d.iloc[-1]
            lines.append(
                f"- **Districts (flats sold twice, medians):** growth ran from "
                f"{_pct(lo['growth'])} a year in {district_label(lo['district'])} to "
                f"{_pct(hi['growth'])} in {district_label(hi['district'])}"
                f"{worst_fall_clause(d)}.")
    lines.append(
        f"- **How much data:** {_n(cov['clean'])} clean sales; "
        f"{_n(len(s.pairs['real']))} flats sold twice with two recorded sales, "
        f"{_n(len(s.pairs['ricacorp']))} Ricacorp reported gains, "
        f"{_n(len(s.pairs['estimated']))} model-valued flats. Flats sold twice span "
        f"{_years(s.pairs['recorded'])}.")
    lines += ["", "Words used below: *flats sold twice* have two recorded prices (bought, then "
              "sold). *Ricacorp reported gains* are flats where Ricacorp states the previous "
              "sale. *Model-valued flats* have an old sale and today's value from the model. "
              "*Robust* means the effect is statistically clear (p < 0.05) and points the same "
              "way when the flats are split by time and, separately, by district. "
              "*Drawdown* is the largest fall from a peak to a later low.", ""]
    return lines


def crosscheck_line(s: Study) -> str | None:
    """The summary's model-valued cross-check line, or None when the setting has no verdicts
    for both kinds. It ends with the understatement warning (decisions.md "Report wording";
    the reason is in "How it was done")."""
    if "recorded" not in s.verdicts or "estimated" not in s.verdicts:
        return None
    a, b = s.verdicts["recorded"], s.verdicts["estimated"]
    common = a.index.intersection(b.index)
    both = [x for x in common if a.loc[x, "robust"] and b.loc[x, "robust"]
            and np.sign(a.loc[x, "coef"]) == np.sign(b.loc[x, "coef"])]
    clash = [x for x in common if a.loc[x, "robust"] and b.loc[x, "robust"]
             and np.sign(a.loc[x, "coef"]) != np.sign(b.loc[x, "coef"])]
    text = "- **Cross-check with model-valued flats** (a flat's last sale before 2018 " \
           "against the model's value today): "
    text += (f"agree and robust in both: {', '.join(SHORT[x] for x in both)}."
             if both else "no attribute is robust in both.")
    if clash:
        text += f" Robust in both but opposite: {', '.join(SHORT[x] for x in clash)}."
    return text + " (this check tends to understate differences; see How it was done)"


def worst_fall_clause(d: pd.DataFrame) -> str:
    """The summary's "; the worst fall was about 45% in a typical district (peak 1997, low
    2003)" from the district rows' drawdowns, with the median district's peak and low years
    (the period used to be hard-coded as the 1997-2003 crash, wrong for a window that does
    not cover it). Empty when no district has a drawdown."""
    dd = d.dropna(subset=["drawdown"])
    if dd.empty:
        return ""
    peak = int(pd.to_datetime(dd["peak"]).dt.year.median())
    trough = int(pd.to_datetime(dd["trough"]).dt.year.median())
    return (f"; the worst fall was about {_pct(dd['drawdown'].median(), 0)} in a typical "
            f"district (peak {peak}, low {trough})")


def _years(p: pd.DataFrame) -> str:
    if p is None or p.empty or "start_date" not in p:
        return "no years"
    return f"purchases {p['start_date'].min():%Y}-{p['start_date'].max():%Y}, sales " \
           f"{p['end_date'].min():%Y}-{p['end_date'].max():%Y}"


def _coverage(s: Study) -> list[str]:
    cov = s.coverage
    lines = ["## Data coverage", "",
             f"Database snapshot: {cov['snapshot_at']}. Clean sales: {_n(cov['clean'])} of "
             f"{_n(cov['deals'])} stored ({_n(cov['pilot_clean'])} in the pilot estates), in "
             f"{_n(cov['buildings_clean'])} buildings and {_n(cov['estates_clean'])} estates. "
             f"Quarantined LOHAS Park rows not read: {_n(cov['quarantined'])}.", ""]
    if cov["running"]:
        lines += ["Marked as running in run_log when the snapshot was taken (loads still going; "
                  "a run that stopped abnormally can stay marked): "
                  + "; ".join(cov["running"][:8]) + ".", ""]
    bs = cov["by_source"].reset_index()
    bs = pd.DataFrame({"Source": bs["source"], "Sales stored": bs["deals"].map(_n),
                       "First": bs["first"].dt.strftime("%Y-%m"),
                       "Last": bs["last"].dt.strftime("%Y-%m")})
    lines += _table(bs) + [""]
    yrs = cov["clean_by_year"]
    lines += ["Clean sales by year: " + ", ".join(f"{int(y)}: {n:,}" for y, n in yrs.items()
                                                  if y >= 1995) + ".", ""]
    pc = pd.DataFrame([
        {"Kind": TYPE_NAMES.get(k, k), "Flats": _n(len(v)),
         "Pilot share": _pct(v["is_pilot"].mean(), 0) if len(v) and "is_pilot" in v else "–",
         "Start years": f"{v['start_date'].min():%Y}-{v['start_date'].max():%Y}"
         if len(v) and v["start_date"].notna().any() else "unknown",
         "End years": f"{v['end_date'].min():%Y}-{v['end_date'].max():%Y}" if len(v) else "–"}
        for k, v in s.pairs.items() if k != "recorded"])
    lines += _table(pc) + [""]
    fc = cov["feature_cover"]
    lines += ["Attribute coverage among clean sales: " + ", ".join(
        f"{k} {_pct(v, 0)}" for k, v in fc.items()) + ". Facing comes only from listings that "
        "state it, so south-facing is tested on few flats.", ""]
    ex = cov["exclusions"]
    lines += ["Sales left out (reason: count): " + ", ".join(f"{k}: {v:,}" for k, v in ex.items())
              + ".", ""]
    return lines


# decisions.md "Report wording": the model leans back towards the old price, so the
# model-valued check understates real differences
CAVEAT = ("For flats whose last sale was before 2018, we compare that sale with the model's "
          "estimate of the flat's value today. The model learned partly from that same old "
          "sale, so its estimate leans back towards the old price. This hides part of any real "
          "difference between kinds of flat: when this check shows 'no effect', trust it less "
          "than the result from flats sold twice.")


def _window_text(m: Study) -> str:
    start, end = m.window
    return (f"bought from {start or 'the earliest data'}, sold by {end or 'the latest data'}, "
            f"held {m.min_hold_years:g}+ years")


def _method(s: Study, main: list[Study], sens: list[Study]) -> list[str]:
    a = s.anchor
    checks = {k: v.attrs.get("checks", {}) for k, v in s.verdicts.items()}
    lines = [
        "## How it was done", "",
        "- **Flats sold twice.** Two sales of the same flat (building + floor + flat), "
        "consecutive among clean sales, held at least 3 years. Clean: not a duplicate report, "
        "parking-space sale, house, bulk sale, outlier, part-share-like price (under half or "
        "over double the building's index-adjusted median), non-market transfer, or wrongly "
        "attached LOHAS Park row. A developer sale may be the first sale and is tagged "
        "first-hand. Ricacorp reported gains (a flat's previous sale taken from Ricacorp's "
        "days held and % gain) are counted with them.",
    ]
    if len(main) + len(sens) > 1:
        lines.append("- **Settings.** The same study in several date windows from one snapshot "
                     "(a flat counts when both its sales fall inside):")
        for m in main + sens:
            lines.append(f"  - `{m.setting}`: {plain_words(m.label or m.setting)}; "
                         f"{_window_text(m)}; "
                         f"{len(m.pairs['recorded']):,} flats sold twice"
                         + ("" if m.enough else " (too few for a verdict)")
                         + (" **[sensitivity]**" if m.sensitivity else "") + ".")
    lines += [
        "- **Regression.** Annual growth (log) on the attributes, controlling for district, "
        "building age at purchase (and its square), purchase year, sale year and purchase "
        "price per sq ft. Errors are clustered by building. Effects are in percentage points "
        "of growth a year.",
        "- **Out-of-sample checks.** Time split: flats sold by 2018 vs later where both "
        "sides have 200+ flats, otherwise by purchase year at the median inside the window "
        "(model-valued flats all end today, so they always split by purchase year). Districts "
        "are dealt into two halves of similar size. Robust = p < 0.05 on all flats and the "
        "same sign in both halves of both splits. Splits used in the headline setting:",
    ]
    for k, c in checks.items():
        if c:
            lines.append(f"  - {KIND[k]}: time split = {plain_words(c.get('time'))}; "
                         f"district split = {plain_words(c.get('district'))}.")
    lines += [
        "- **Model-valued flats.** The fair-price model values each flat last sold before 2018 "
        "as of the latest RVD index month. Model error (80% interval about ±11%) is in each "
        "flat's growth; clustering by building covers errors shared by a building. Where the "
        "model's out-of-time error differs by attribute, an estimated effect smaller than "
        "that tilt is not called robust. Model-valued flats are the cross-check, not the main "
        "evidence: their end price is a model value, and the model learns today's price "
        "differences between attributes. " + CAVEAT,
        "- **Costs.** Buyer: Hong Kong permanent resident who owns another flat (decisions.md), "
        "so 2013-2024 purchases pay the higher Scale 1 / 15% / 7.5% rates; first-home buyer "
        "shown as a sensitivity. Agent 1% each side, legal 0.3% on purchase, stamp duty from "
        "the IRD tables at the purchase date. No special or buyer's stamp duty (3+ year holds, "
        "permanent resident).",
        f"- **Rent.** RVD average rent / average price by size class and region, averaged over "
        f"the holding period, times an anchor from current rental listings: "
        f"{a.get('factor', 1):.2f} ({a.get('basis')}; {a.get('buildings', 0)} buildings, "
        f"{a.get('rent_listings', 0)} rent listings; listing yield "
        f"{_pct(a.get('listing_median_yield'))} vs RVD {_pct(a.get('rvd_median_yield'))}). "
        "Asking rents run above achieved rents, so the anchor may overstate yields slightly.",
        "- **Drawdown.** A monthly repeat-sales index per group and per district (log price "
        "changes explained by month effects, lightly smoothed), worst peak-to-trough fall "
        "inside the setting's window. Model-valued flats: "
        f"{ESTIMATED_INDEX_WINDOW[0][:4]}-{ESTIMATED_INDEX_WINDOW[1][:4]} (their purchase "
        "dates; their end is always today).",
        "",
    ]
    return lines


def _verdict_table(v: pd.DataFrame) -> list[str]:
    rows = []
    for _a, r in v.iterrows():
        lo, hi = r["coef"] - 1.96 * r["se"], r["coef"] + 1.96 * r["se"]
        rows.append({
            "Attribute": r["label"], "Flats with it": _n(r["n_with"]),
            "Effect (pp/yr)": _pp(r["coef"]), "95% range": f"{_pp(lo)} to {_pp(hi)}",
            "p": "<0.001" if r["p"] < 0.001 else f"{r['p']:.3f}",
            "Time halves": f"{_pp(r['time_a'])} / {_pp(r['time_b'])}",
            "District halves": f"{_pp(r['district_a'])} / {_pp(r['district_b'])}",
            **({"Model tilt": _pp(r["model_tilt"])} if "model_tilt" in v.columns else {}),
            "Verdict": "**robust**" if r["robust"] else (
                "significant, not robust" if r["significant"] else "not significant"),
        })
    return _table(pd.DataFrame(rows)) + ["", "Time / district halves: the estimate in each "
                                         "half (– = too few flats there to estimate, which "
                                         "counts as not holding).", ""]


def _groups_table(s: Study, key: str) -> list[str]:
    f = s.frames.get(key)
    v = s.verdicts.get(key)
    if f is None or v is None:
        return []
    dd = s.drawdowns
    dd = dd[dd["pairs_type"] == key].set_index("group") if not dd.empty else pd.DataFrame()
    rows = []
    for a in v.index:
        for lvl, name in ((1, "yes"), (0, "no")):
            part = f[f[a] == lvl]
            g = a if lvl else f"not {a}"
            rows.append({
                "Attribute": SHORT[a], "Has it": name, "Flats": _n(len(part)),
                "Growth/yr": _pct(part["ann_growth"].median()),
                "After costs": _pct(part["net_ann_growth"].median()),
                "Rent yield": _pct(part["rental_yield"].median()),
                "Total return": _pct(part["total_return"].median()),
                "Drawdown": _pct(dd["drawdown"].get(g), 0) if not dd.empty else "–",
            })
    return _table(pd.DataFrame(rows))


def _verdict_sections(s: Study, main: list[Study], fig_dir: Path, rel: str) -> list[str]:
    lines = ["## Verdicts by attribute and market phase (flats sold twice: the main evidence)",
             "", "Flats sold twice have two real prices. Each column is one "
             "setting (a date window; both sales inside it; held 3+ years).", ""]
    if len(main) > 1:
        lines += matrix_lines(main)
        rows = []
        for m in main:
            v = m.verdicts.get("recorded")
            if v is None or v.empty:
                continue
            for a, r in v.iterrows():
                rows.append({"label": SHORT[a], "group": m.setting, "est": 100 * r["coef"],
                             "lo": 100 * (r["coef"] - 1.96 * r["se"]),
                             "hi": 100 * (r["coef"] + 1.96 * r["se"])})
        if rows:
            cr = pd.DataFrame(rows)
            groups = [m.setting for m in main if m.setting in set(cr["group"])]
            charts.dot_ci_chart(cr, fig_dir / "effects_settings.svg",
                                "Attribute effects by setting (flats sold twice)", groups)
            lines += [f"![Attribute effects by setting]({rel}/effects_settings.svg)", ""]
    v = s.verdicts.get("recorded")
    if v is not None and not v.empty:
        lines += [f"### Detail: setting `{s.setting}`, {TYPE_NAMES['recorded'].lower()}", "",
                  f"{len(s.frames['recorded']):,} flats sold twice.", ""] + _verdict_table(v)
        vr = s.verdicts.get("real")
        n_rica = len(s.pairs["ricacorp"])
        if vr is not None and not vr.empty and n_rica:
            same = (vr.index.equals(v.index) and (vr["robust"] == v["robust"]).all()
                    and (vr["significant"] == v["significant"]).all())
            if same:
                lines += [f"Flats with two recorded sales alone ({len(s.frames['real']):,}, "
                          f"without the {n_rica} Ricacorp reported gains) give the same "
                          "verdicts; Ricacorp reported gains alone are too few for a "
                          f"regression (needs {MIN_PAIRS}).", ""]
            else:
                lines += [f"### Flats with two recorded sales alone "
                          f"({len(s.frames['real']):,}, without the {n_rica} Ricacorp "
                          "reported gains)", ""] + _verdict_table(vr)
        keys = [k for k in ("recorded", "estimated")
                if k in s.verdicts and len(s.verdicts[k])]
        cr = pd.DataFrame([{"label": SHORT[a], "group": k, "est": 100 * r["coef"],
                            "lo": 100 * (r["coef"] - 1.96 * r["se"]),
                            "hi": 100 * (r["coef"] + 1.96 * r["se"])}
                           for k in keys for a, r in s.verdicts[k].iterrows()])
        cr["group"] = cr["group"].map(KIND)
        charts.dot_ci_chart(cr, fig_dir / "effects.svg",
                            f"Attribute effects on annual growth, setting {s.setting}",
                            [KIND[k] for k in keys])
        lines += [f"![Attribute effects]({rel}/effects.svg)", ""]
    elif s.enough is False:
        lines += [f"Setting `{s.setting}` has too few flats sold twice for a verdict.", ""]
    return lines


def _estimated_section(s: Study, fig_dir: Path, rel: str) -> list[str]:
    v = s.verdicts.get("estimated")
    if v is None or v.empty:
        return []
    lines = ["## Cross-check: model-valued flats", "",
             f"{TYPE_NAMES['estimated']}: {len(s.frames['estimated']):,} flats in setting "
             f"`{s.setting}`. Their end price is a model value, so they are a check on the "
             "flats sold twice (do the long-run purchases 1995-2017 tell the same story?), not "
             "evidence on their own. " + CAVEAT, ""]
    rv = s.verdicts.get("recorded")
    if rv is not None and not rv.empty:
        rows = []
        for a in growth.ATTRIBUTES:
            cr, ce = cell(s, a, "recorded"), cell(s, a, "estimated")
            if cr is None and ce is None:
                continue
            agree = ("yes" if cr and ce and np.sign(cr["coef"]) == np.sign(ce["coef"])
                     and cr["robust"] and ce["robust"] else
                     "same sign" if cr and ce and np.sign(cr["coef"]) == np.sign(ce["coef"])
                     else "no")
            rows.append({"Attribute": SHORT[a], "Sold twice": cell_text(cr),
                         "Model-valued": cell_text(ce),
                         "Agree (robust in both, same way)": agree})
        lines += _table(pd.DataFrame(rows)) + ["", "(The chart above shows both sets of "
                                               "effects side by side.)", ""]
    lines += ["### Model-valued flats in detail", ""] + _verdict_table(v)
    return lines


def _sensitivity_section(sens: list[Study]) -> list[str]:
    if not sens:
        return []
    lines = ["## Short-hold sensitivity (1-year minimum hold; not part of the main verdicts)",
             "",
             "The two most recent phases are too short for 3-year holds, so each is rerun "
             "with flats held at least 1 year. Short holds are a different population (quick "
             "flips, forced sales) and the verdicts here are a side-check only: they are "
             "never mixed into the table above. After-cost figures here ignore Special Stamp "
             "Duty (10-20% on resales within 36 months until 28 Feb 2024), so read the gross "
             "growth and the verdicts, not the after-cost numbers.", ""]
    for m in sens:
        n = len(m.pairs["recorded"])
        lines += [f"### `{m.setting}`: {plain_words(m.label or m.setting)}", "",
                  f"{_window_text(m).capitalize()}: {n:,} flats sold twice "
                  f"({len(m.pairs['real']):,} with two recorded sales, "
                  f"{len(m.pairs['ricacorp']):,} Ricacorp reported gains)."]
        f = m.frames.get("recorded")
        if f is not None and len(f):
            lines.append(f"Median hold {f['years'].median():.1f} years; median growth "
                         f"{_pct(f['ann_growth'].median())} a year"
                         + (f", total return {_pct(f['total_return'].median())}"
                            if "total_return" in f else "") + ".")
        lines.append("")
        v = m.verdicts.get("recorded")
        if v is not None and len(v):
            lines += _verdict_table(v)
        else:
            note = next((x for x in m.notes if x.startswith("Too few")),
                        "Too few flats sold twice.")
            lines += [plain_words(note), ""]
    return lines


def _district_section(s: Study, fig_dir: Path, rel: str) -> list[str]:
    d = s.districts
    if d is None or d.empty:
        return []
    rec = d[d["pairs_type"] == "recorded"].copy()
    if rec.empty:
        return []
    lines = [f"## By district (setting `{s.setting}`, flats sold twice)", "",
             "Medians per district (no controls). Growth = price growth a year; after costs "
             "= after stamp duty, agents and lawyers; total return = after-cost growth + gross "
             "rent yield; worst fall = the district's own repeat-sales index, peak to low "
             "(blank under 100 flats). Model-valued growth = the median for the "
             "model-valued flats (the cross-check).", ""]
    est = d[d["pairs_type"] == "estimated"].set_index("district")["growth"] \
        if (d["pairs_type"] == "estimated").any() else pd.Series(dtype=float)
    rec = rec.sort_values("growth", ascending=False)
    rows = []
    for _, r in rec.iterrows():
        rows.append({
            "District": district_label(r["district"]), "Flats": _n(r["pairs"]),
            "Years held": f"{r['median_years']:.1f}", "Growth/yr": _pct(r["growth"]),
            "After costs": _pct(r["net_growth"]), "Rent yield": _pct(r["rental_yield"]),
            "Total return": _pct(r["total_return"]),
            "Worst fall": _pct(r["drawdown"], 0),
            "Peak → low": (f"{pd.Timestamp(r['peak']):%Y-%m} → {pd.Timestamp(r['trough']):%Y-%m}"
                           if pd.notna(r["drawdown"]) and r["peak"] is not None else "–"),
            "Model-valued growth/yr": _pct(est.get(r["district"], np.nan)),
        })
    lines += _table(pd.DataFrame(rows)) + [""]
    idx = s.indices.get("recorded", {})
    first = pd.Timestamp("1995-01-01")
    drawn = []
    for group, names in DISTRICT_GROUPS.items():
        series = {}
        for name in names:
            ser = idx.get(f"district:{name}")
            if ser is None or ser.empty:
                continue
            ser = ser[ser.index >= first]
            if ser.empty:
                continue
            series[district_label(name)] = ser / ser.iloc[0] * 100
        if series:
            fname = "district_index_" + group.lower().replace(" ", "_") + ".svg"
            charts.line_chart(series, fig_dir / fname,
                              f"Price index by district, {group} (log scale, "
                              f"{series[next(iter(series))].index[0]:%Y} = 100)",
                              "index", log_scale=True)
            drawn.append((group, fname))
    if drawn:
        lines += ["### Price index per district from 1995", "",
                  "A repeat-sales index from the district's own flats sold twice (first month "
                  "= 100, log scale so equal slopes mean equal growth rates).", ""]
        for group, fname in drawn:
            lines += [f"![{group}]({rel}/{fname})", ""]
    return lines


def _index_section(s: Study, fig_dir: Path, rel: str) -> list[str]:
    lines = ["## Price indices and drawdown", ""]
    idx_rec = s.indices.get("recorded", {})
    if "all" in idx_rec:
        rec_idx = idx_rec["all"]
        series = {"Flats sold twice": rec_idx / rec_idx.iloc[0] * 100}
        rvd_all = _rvd_series(s, "rvd_price_index", "ALL", "ALL")
        if rvd_all is not None:
            r = rvd_all[rvd_all.index >= rec_idx.index[0]]
            if not r.empty:
                series["RVD all classes"] = r / r.iloc[0] * 100
        charts.line_chart(series, fig_dir / "index_recorded.svg",
                          f"Repeat-sales index from flats sold twice vs RVD "
                          f"({rec_idx.index[0]:%Y} = 100, log scale)", "index", log_scale=True)
        lines += [f"![Recorded index]({rel}/index_recorded.svg)", "",
                  "Our own index from every flat sold twice in the setting, against the "
                  "official RVD index over the same months. Agreement is a check on our data.",
                  ""]
    idx_est = s.indices.get("estimated", {})
    if "all" in idx_est:
        # model-valued flats only have data at their purchase months: draw those (normalised
        # to today = 100), not the smoothed join from 2018 to today
        cut = pd.Timestamp(ESTIMATED_INDEX_WINDOW[1])
        e = idx_est["all"]
        series = {"Model-valued flats": (e / e.iloc[-1] * 100)[e.index <= cut]}
        if "first_hand_start" in idx_est:
            f = idx_est["first_hand_start"]
            series["Est., first-hand start"] = (f / f.iloc[-1] * 100)[f.index <= cut]
        rvd_all = _rvd_series(s, "rvd_price_index", "ALL", "ALL")
        if rvd_all is not None:
            series["RVD all classes"] = rvd_all / rvd_all.iloc[-1] * 100
        charts.line_chart(series, fig_dir / "index_long.svg",
                          "Long-run index from model-valued flats (today = 100, log scale)",
                          "index", log_scale=True)
        lines += [f"![Long-run index]({rel}/index_long.svg)", "",
                  "Model-valued flats give the index only at their purchase months (to 2017), "
                  "relative to the model's value today; that they track the official RVD index "
                  "through the 1997-2003 crash and the 2003-2017 rise is a check on the model "
                  "and on these flats.", ""]
    if not s.rvd_long.empty:
        lt = s.rvd_long.copy()
        cls = {"A": "A (under 40 m2)", "B": "B (40-69.9 m2)", "C": "C (70-99.9 m2)",
               "D": "D (100-159.9 m2)", "E": "E (160 m2+)", "ABC": "A-C", "DE": "D-E",
               "ALL": "all"}
        t = pd.DataFrame({"Series": lt["series"], "Size class": lt["class"].map(cls),
                          "Region": lt["region"].str.replace("ALL", "all"),
                          "Period": lt["from"].dt.strftime("%Y-%m") + " to "
                          + lt["to"].dt.strftime("%Y-%m"),
                          "Growth/yr": lt["ann_growth"].map(_pct),
                          "Worst fall": lt["drawdown"].map(lambda x: _pct(x, 0)),
                          "Peak → low": lt["peak"].dt.strftime("%Y-%m") + " → "
                          + lt["trough"].dt.strftime("%Y-%m")})
        lines += ["### Long-run context from RVD (official indices)", "",
                  "Size classes are by saleable area. Larger flats (D, E) grew somewhat faster "
                  "over the long run on these indices; every class fell by about two-thirds "
                  "from 1997 to 2003.", ""] + _table(t) + [""]
        series = {}
        for c in ("A", "C", "E"):
            ser = _rvd_series(s, "rvd_price_index", c, "ALL")
            if ser is not None:
                series[f"Class {c}"] = ser
        if series:
            charts.line_chart(series, fig_dir / "rvd_classes.svg",
                              "RVD price index by size class (log scale)", "index",
                              log_scale=True)
            lines += [f"![RVD classes]({rel}/rvd_classes.svg)", ""]
    if not s.drawdowns.empty:
        d = s.drawdowns[~s.drawdowns["group"].str.startswith("district:")]
        lines += ["### Drawdown by group", ""] + _table(pd.DataFrame({
            "Kind": d["pairs_type"].map(KIND), "Group": d["group"],
            "Flats used": d["pairs"].map(_n),
            "Worst fall": d["drawdown"].map(lambda x: _pct(x, 0)),
            "Peak": pd.to_datetime(d["peak"]).dt.strftime("%Y-%m"),
            "Low": pd.to_datetime(d["trough"]).dt.strftime("%Y-%m")})) + [""]
    return lines


def write(s: Study, studies: dict[str, Study] | None = None, today: date | None = None
          ) -> Path:
    """Write the findings file from the headline study `s` (setting `all` when it was run)
    and, when `studies` holds the other settings of the same run, the attribute x setting
    table, the district tables and the sensitivity section."""
    today = today or date.today()
    reports = get_data_dir() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"findings_{today:%Y%m%d}.md"
    fig_dir = reports / f"findings_{today:%Y%m%d}"
    fig_dir.mkdir(exist_ok=True)
    rel = fig_dir.name

    studies = studies or {s.setting: s}
    if s.setting not in studies:
        studies = {s.setting: s, **studies}
    main = [m for m in studies.values() if not m.sensitivity]
    sens = [m for m in studies.values() if m.sensitivity]

    lines = [f"# Findings: which flat attributes grew faster ({today:%Y-%m-%d})",
             "", "Written by `hkestate report`. Generated, not edited by hand; rerun it after "
             "`hkestate train` as data grows. Not committed to git. Headline setting "
             f"`{s.setting}`: flats {_window_text(s)}."
             + (f" Settings run: {', '.join(f'`{m.setting}`' for m in main)}"
                + (f"; sensitivities: {', '.join(f'`{m.setting}`' for m in sens)}" if sens
                   else "") + "." if len(studies) > 1 else ""), ""]
    lines += _summary(s, main, sens)
    lines += _coverage(s)
    lines += _method(s, main, sens)
    lines += _verdict_sections(s, main, fig_dir, rel)
    lines += _estimated_section(s, fig_dir, rel)
    lines += _sensitivity_section(sens)

    lines += ["## Growth, total return and drawdown by attribute", "",
              "Medians. Growth = price growth a year; after costs = after stamp duty, agents and "
              "lawyers on a buy and a sale; total return = after-cost growth + gross rent yield. "
              "These are raw group medians (no controls): the verdicts above are the "
              "like-for-like comparison.", ""]
    for key in ("recorded", "estimated"):
        t = _groups_table(s, key)
        if t:
            lines += [f"### {TYPE_NAMES[key]}", ""] + t + [""]

    lines += _district_section(s, fig_dir, rel)
    lines += _index_section(s, fig_dir, rel)

    lines += ["## First-hand vs second-hand", "",
              "First-hand = the first sale is the developer's (SRPE, or the project's "
              "first-sale rule). Developer prices include launch discounts and incentives, so "
              "first-hand starts are compared separately.", ""]
    if not s.first_hand.empty:
        fh = s.first_hand
        lines += _table(pd.DataFrame({
            "Kind": fh["pairs_type"].map(KIND), "Start": fh["start"],
            "Count": fh["pairs"].map(_n),
            "Median years held": fh["median_years"].map(lambda x: f"{x:.1f}"),
            "Growth/yr": fh["growth"].map(_pct), "After costs": fh["net_growth"].map(_pct),
            "Total return": fh["total_return"].map(_pct)})) + [""]
    for key in ("recorded", "estimated"):
        v = s.verdicts.get(key)
        if v is not None and "first_hand_start" in v.index:
            r = v.loc["first_hand_start"]
            lines.append(f"- {KIND[key]}: like-for-like effect {_pp(r['coef'])} pp a year "
                         f"(p {r['p']:.3f}), {'robust' if r['robust'] else 'not robust'}.")
    lines.append("")

    lines += ["## Checks", "", "### Centadata / 28Hse '% change' figures", ""]
    gc = s.gain_check
    lines.append(
        f"Where a flat's '% change vs the previous sale' can be compared with a recorded "
        f"previous sale ({_n(gc.get('n'))} cases), the implied previous price matches within "
        f"2% in {_pct(gc.get('within_2pct'), 0)} of cases (median difference "
        f"{_pct(gc.get('median_abs_diff'), 2)}). So the field is reliable for the price. It "
        f"has no date, so {len(s.pairs['gain']):,} such flats cannot be turned into growth "
        "a year and are not used in the verdicts.")
    if not s.gain_signs.empty:
        g = s.gain_signs.dropna(subset=["recorded_coef"])
        lines += ["", "Sign check: the same regression on their total gain (sale year instead "
                  "of purchase year). Holding periods are unknown and differ by attribute "
                  "(e.g. small flats change hands more often), so disagreement is expected "
                  "and this is weak evidence either way.", ""]
        lines += _table(pd.DataFrame({
            "Attribute": [SHORT[a] for a in g.index],
            "Total-gain effect (log)": g["gain_coef"].map(lambda x: f"{x:+.3f}"),
            "Recorded effect (pp/yr)": g["recorded_coef"].map(_pp),
            "Same direction": g["agrees"].map({True: "yes", False: "no"})}))
    lines.append("")
    if not s.model_bias.empty:
        b = s.model_bias
        lines += ["### Model error by attribute (for model-valued flats)", "",
                  "The fair-price model's out-of-time error (median of model / actual - 1) for "
                  "flats with and without each attribute. A difference tilts estimated growth "
                  "by about the difference / holding years.", ""]
        lines += _table(pd.DataFrame({
            "Attribute": [SHORT[a] for a in b.index], "Test sales with it": b["n_with"].map(_n),
            "Error with": b["bias_with"].map(_pct), "Error without": b["bias_without"].map(_pct),
            "Difference": b["difference"].map(lambda x: _pct(x, 1, True))})) + [""]

    for k in ("recorded", "estimated"):
        f = s.frames.get(k)
        if f is not None and "net_ann_growth_first" in f:
            lines.append(f"- Cost sensitivity ({KIND[k]}): after-cost growth is "
                         f"{_pct(f['net_ann_growth'].median())} a year for a buyer who owns "
                         f"another flat, {_pct(f['net_ann_growth_first'].median())} for a "
                         "first-home buyer (lower stamp duty 2013-2024).")
    lines.append("")

    lines += _limits(s)
    if s.notes:
        lines += ["## Notes", ""] + [f"- {n}" for n in s.notes] + [""]
    path.write_text("\n".join(lines), "utf-8")
    return path


def _rvd_series(s: Study, series: str, cls: str, region: str) -> pd.Series | None:
    rvd = s.rvd
    if rvd is None or rvd.empty:
        return None
    r = rvd[(rvd["series"] == series) & (rvd["class"] == cls) & (rvd["region"] == region)
            & (rvd["freq"] == "month")]
    if r.empty:
        return None
    return r.assign(period=pd.to_datetime(r["period"])).set_index("period")["value"].sort_index()


def _limits(s: Study) -> list[str]:
    fc = s.coverage.get("feature_cover", {})
    return [
        "## Limits", "",
        "- **Only flats that were resold are counted as sold twice.** A flat bought and never "
        "sold again contributes nothing, so the results lean towards flats that change hands; "
        "the model-valued flats (last sold before 2018, valued by the model) cover the rest.",
        "- **Short phases hold few flats sold twice after a 3-year hold.** Both sales must be "
        "inside the window, "
        "so a phase under about four years (the 2021-2024 fall, the market since late 2024) "
        "has no verdict of its own; the 1-year-hold sensitivity is the only read on them.",
        "- **Attribute coverage is uneven.** MTR distance is known for "
        f"{_pct(fc.get('MTR distance'), 0)} of clean sales, facing for "
        f"{_pct(fc.get('facing'), 0)}; sea view and facing come from listings only, so those "
        "effects rest on few flats.",
        "- **Model-valued flats depend on the model.** Their end price is a model value "
        "(typical error about 5%, 80% interval about ±11%). The model learns today's price "
        "differences between attributes from recent sales, so an estimated effect reads as "
        "\"today's premium vs the premium paid then\". It also learned partly from the old "
        "sale itself, so this check tends to understate differences (see How it was done).",
        "- **Asking rents** anchor the yields; achieved rents are usually lower.",
        "- **No leverage, no holding costs** (rates, management fees, repairs, vacancy, tax).",
        "- **Costs assume a permanent resident who owns another flat**; a first-home buyer "
        "pays less stamp duty for 2013-2024 purchases (sensitivity above). The short-hold "
        "sensitivity ignores Special Stamp Duty.",
        "- **Like-for-like, not causal.** An effect says flats with the attribute grew faster "
        "than otherwise similar flats; it does not say the attribute caused it.",
        "",
        "## Rerun", "",
        "`hkestate train --from-snapshot`, then `hkestate report --from-snapshot --settings "
        "standard` (5-15 minutes: 4.6 on 2026-10-22 with nothing else running, 14 the day "
        "before; the saved results under `data/reports/growth/<date>/` "
        "feed the dashboard's growth page).",
        "",
    ]
