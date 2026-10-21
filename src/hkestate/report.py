"""Findings report (Task 5.4): `hkestate report` writes data/reports/findings_<date>.md with
charts in data/reports/findings_<date>/. Generated from `study.run`; never committed (the repo
is public and the report holds the data).

Order: a plain-language summary first (the owner relays it to non-technical people), then
coverage, method, verdicts, tables and charts, first-hand vs second-hand, checks, limits.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from hkestate import charts
from hkestate.config import get_data_dir
from hkestate.study import ESTIMATED_INDEX_WINDOW, Study

TYPE_NAMES = {
    "real": "Real pairs (two recorded sales of the same flat)",
    "ricacorp": "Ricacorp pairs (previous sale from Ricacorp's days-held and % gain)",
    "recorded": "Recorded pairs (real + Ricacorp combined)",
    "estimated": "Estimated pairs (sale before 2018 vs the model's value today)",
}


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


def _summary(s: Study) -> list[str]:
    cov = s.coverage
    rec = s.frames.get("recorded")
    est = s.frames.get("estimated")
    lines = ["## Summary in plain language", ""]
    lines.append(
        "**This is a preliminary report.** The territory-wide data load is still running "
        "(about 9 days for the 28Hse history), so most recorded resale pairs come from the five "
        "pilot estates. The numbers will change when the load finishes; rerun `hkestate report` "
        "then. Verdicts close to the line can flip as data is added (while the load runs, a "
        "rerun an hour later can differ), so treat single verdicts as provisional and look "
        "for the ones that hold in both kinds of pairs.")
    lines.append("")
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
                f"- **Worst fall in our own resale pairs (this window):** prices fell "
                f"{_pct(r['drawdown'], 0)} from {r['peak']:%B %Y} to {r['trough']:%B %Y}.")
    for key, who in (("recorded", "flats sold twice since about 2018 (mostly 2018-2026)"),
                     ("estimated", "flats last sold before 2018, valued today by our model")):
        v = s.verdicts.get(key)
        if v is None or v.empty:
            continue
        rob = v[v["robust"]]
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
    both = []
    if "recorded" in s.verdicts and "estimated" in s.verdicts:
        a, b = s.verdicts["recorded"], s.verdicts["estimated"]
        common = a.index.intersection(b.index)
        both = [x for x in common if a.loc[x, "robust"] and b.loc[x, "robust"]
                and np.sign(a.loc[x, "coef"]) == np.sign(b.loc[x, "coef"])]
        if both:
            lines.append(f"- **Strongest signal (robust in both the recent and the long-run "
                         f"pairs, same direction):** {', '.join(SHORT[x] for x in both)}.")
    fh = s.first_hand
    if not fh.empty:
        for k in ("recorded", "estimated"):
            part = fh[fh["pairs_type"] == k].set_index("start")
            if {"first-hand", "second-hand"} <= set(part.index):
                lines.append(
                    f"- **First-hand vs second-hand ({k} pairs):** flats bought from the "
                    f"developer grew {_pct(part.loc['first-hand', 'growth'])} a year (median) "
                    f"vs {_pct(part.loc['second-hand', 'growth'])} for resales.")
    for k, f in (("recorded", rec), ("estimated", est)):
        if f is not None and "total_return" in f:
            lines.append(
                f"- **Return to an owner ({k} pairs, median):** price growth "
                f"{_pct(f['ann_growth'].median())} a year; after buying and selling costs "
                f"(stamp duty for a buyer who owns another flat, agents, lawyers) "
                f"{_pct(f['net_ann_growth'].median())}; plus rent of about "
                f"{_pct(f['rental_yield'].median())} a year gives a total of "
                f"{_pct(f['total_return'].median())} a year (before rates, management fees "
                f"and tax; no mortgage).")
    lines.append(
        f"- **How much data:** {_n(cov['clean'])} clean sales; "
        f"{_n(len(s.pairs['real']))} real resale pairs, {_n(len(s.pairs['ricacorp']))} Ricacorp "
        f"pairs, {_n(len(s.pairs['estimated']))} estimated pairs. Recorded pairs are "
        f"{_pct(rec['is_pilot'].mean(), 0) if rec is not None and 'is_pilot' in rec else '–'} "
        "from the pilot estates.")
    lines += ["", "Words used below: a *pair* is two prices for the same flat (bought, then sold "
              "or valued). *Robust* means the effect is statistically clear (p < 0.05) and points "
              "the same way when the pairs are split by time and, separately, by district. "
              "*Drawdown* is the largest fall from a peak to a later low.", ""]
    return lines


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
        {"Pair type": TYPE_NAMES.get(k, k), "Pairs": _n(len(v)),
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


def _method(s: Study) -> list[str]:
    a = s.anchor
    checks = {k: v.attrs.get("checks", {}) for k, v in s.verdicts.items()}
    lines = [
        "## How it was done", "",
        "- **Pairs.** Two sales of the same flat (building + floor + flat), consecutive among "
        "clean sales, held at least 3 years. Clean: not a duplicate report, parking-space "
        "sale, house, bulk sale, outlier, part-share-like price (under half or over double the "
        "building's index-adjusted median), non-market transfer, or wrongly attached LOHAS Park "
        "row. A developer sale may start a pair and is tagged first-hand.",
        "- **Regression.** Annual growth (log) on the attributes, controlling for district, "
        "building age at purchase (and its square), purchase year, sale year and purchase "
        "price per sq ft. Errors are clustered by building. Effects are in percentage points "
        "of growth a year.",
        "- **Out-of-sample checks.** The plan's time split is pairs ending by 2018 vs later. "
        "Recorded pairs almost all end after 2020 (28Hse's free history starts in 2018) and "
        "estimated pairs all end today, so the time split is by purchase year at the median "
        "instead (decisions.md). Districts are dealt into two halves of similar size. "
        "Robust = p < 0.05 on all pairs and the same sign in both halves of both splits.",
    ]
    for k, c in checks.items():
        if c:
            lines.append(f"  - {k}: time split = {c.get('time')}; district split = "
                         f"{c.get('district')}.")
    lines += [
        "- **Estimated pairs.** The fair-price model (Phase 4) values each flat last sold "
        "before 2018 as of the latest RVD index month. Model error (80% interval about ±11%) "
        "is in each pair's growth; clustering by building covers errors shared by a "
        "building. Where the model's out-of-time error differs by attribute, an estimated "
        "effect smaller than that tilt is not called robust.",
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
        "- **Drawdown.** A monthly repeat-sales index per group (log price changes explained "
        "by month effects, lightly smoothed), worst peak-to-trough fall. Recorded pairs: from "
        f"2018; estimated pairs: {ESTIMATED_INDEX_WINDOW[0][:4]}-{ESTIMATED_INDEX_WINDOW[1][:4]} "
        "(their purchase dates; their end is always today).",
        "",
    ]
    return lines


def _verdict_table(v: pd.DataFrame) -> list[str]:
    rows = []
    for _a, r in v.iterrows():
        lo, hi = r["coef"] - 1.96 * r["se"], r["coef"] + 1.96 * r["se"]
        rows.append({
            "Attribute": r["label"], "Pairs with it": _n(r["n_with"]),
            "Effect (pp/yr)": _pp(r["coef"]), "95% range": f"{_pp(lo)} to {_pp(hi)}",
            "p": "<0.001" if r["p"] < 0.001 else f"{r['p']:.3f}",
            "Time halves": f"{_pp(r['time_a'])} / {_pp(r['time_b'])}",
            "District halves": f"{_pp(r['district_a'])} / {_pp(r['district_b'])}",
            **({"Model tilt": _pp(r["model_tilt"])} if "model_tilt" in v.columns else {}),
            "Verdict": "**robust**" if r["robust"] else (
                "significant, not robust" if r["significant"] else "not significant"),
        })
    return _table(pd.DataFrame(rows))


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
                "Attribute": SHORT[a], "Has it": name, "Pairs": _n(len(part)),
                "Growth/yr": _pct(part["ann_growth"].median()),
                "After costs": _pct(part["net_ann_growth"].median()),
                "Rent yield": _pct(part["rental_yield"].median()),
                "Total return": _pct(part["total_return"].median()),
                "Drawdown": _pct(dd["drawdown"].get(g), 0) if not dd.empty else "–",
            })
    return _table(pd.DataFrame(rows))


def write(s: Study, today: date | None = None) -> Path:
    today = today or date.today()
    reports = get_data_dir() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"findings_{today:%Y%m%d}.md"
    fig_dir = reports / f"findings_{today:%Y%m%d}"
    fig_dir.mkdir(exist_ok=True)
    rel = fig_dir.name

    start, end = s.window
    window = (f"pairs bought from {start or 'the earliest data'} and sold by "
              f"{end or 'the latest data'}")
    lines = [f"# Findings: which flat attributes grew faster ({today:%Y-%m-%d}, PRELIMINARY)",
             "", "Written by `hkestate report`. Generated, not edited by hand; rerun it as data "
             f"grows. Not committed to git. Setting `{s.setting}`: {window}.", ""]
    lines += _summary(s)
    lines += _coverage(s)
    lines += _method(s)

    lines += ["## Verdicts by attribute", ""]
    chart_rows = []
    for key in ("recorded", "real", "estimated"):
        v = s.verdicts.get(key)
        if v is None or v.empty:
            continue
        n = len(s.frames[key])
        lines += [f"### {TYPE_NAMES[key]}", "", f"{n:,} pairs.", ""]
        if key == "real":
            lines += ["(Almost the same pairs as the recorded set, which adds the "
                      f"{len(s.pairs['ricacorp'])} Ricacorp pairs; shown for completeness.)",
                      ""]
        lines += _verdict_table(v) + ["", "Time / district halves: the estimate in each "
                                      "half (– = too few pairs there to estimate, which "
                                      "counts as not holding).", ""]
        if key in ("recorded", "estimated"):
            for a, r in v.iterrows():
                chart_rows.append({"label": SHORT[a], "group": key,
                                   "est": 100 * r["coef"], "lo": 100 * (r["coef"] - 1.96 * r["se"]),
                                   "hi": 100 * (r["coef"] + 1.96 * r["se"])})
    if len(s.pairs["ricacorp"]) and "ricacorp" not in s.verdicts:
        lines += [f"Ricacorp pairs alone ({len(s.pairs['ricacorp'])}) are too few for a "
                  "regression (needs 200); they are in the recorded set.", ""]
    if chart_rows:
        cr = pd.DataFrame(chart_rows)
        groups = [g for g in ("recorded", "estimated") if g in set(cr["group"])]
        charts.dot_ci_chart(cr, fig_dir / "effects.svg",
                            "Attribute effects on annual growth", groups)
        lines += [f"![Attribute effects]({rel}/effects.svg)", ""]

    lines += ["## Growth, total return and drawdown by attribute", "",
              "Medians. Growth = price growth a year; after costs = after stamp duty, agents and "
              "lawyers on a buy and a sale; total return = after-cost growth + gross rent yield. "
              "These are raw group medians (no controls): the verdicts above are the "
              "like-for-like comparison.", ""]
    for key in ("recorded", "estimated"):
        t = _groups_table(s, key)
        if t:
            lines += [f"### {TYPE_NAMES[key]}", ""] + t + [""]

    lines += ["## Price indices and drawdown", ""]
    idx_rec = s.indices.get("recorded", {})
    if "all" in idx_rec:
        rec_idx = idx_rec["all"]
        rec_idx = rec_idx[rec_idx.index >= "2018-01-01"]
        series = {"Recorded pairs": rec_idx / rec_idx.iloc[0] * 100}
        rvd_all = _rvd_series(s, "rvd_price_index", "ALL", "ALL")
        if rvd_all is not None:
            r = rvd_all[rvd_all.index >= rec_idx.index[0]]
            series["RVD all classes"] = r / r.iloc[0] * 100
        charts.line_chart(series, fig_dir / "index_recent.svg",
                          "Repeat-sales index from 2018 vs RVD (start = 100)", "index")
        lines += [f"![Recent index]({rel}/index_recent.svg)", ""]
    idx_est = s.indices.get("estimated", {})
    if "all" in idx_est:
        # estimated pairs only have data at their purchase months: draw those (normalised
        # to today = 100), not the smoothed join from 2018 to today
        cut = pd.Timestamp(ESTIMATED_INDEX_WINDOW[1])
        e = idx_est["all"]
        series = {"Estimated pairs": (e / e.iloc[-1] * 100)[e.index <= cut]}
        if "first_hand_start" in idx_est:
            f = idx_est["first_hand_start"]
            series["Est., first-hand start"] = (f / f.iloc[-1] * 100)[f.index <= cut]
        rvd_all = _rvd_series(s, "rvd_price_index", "ALL", "ALL")
        if rvd_all is not None:
            series["RVD all classes"] = rvd_all / rvd_all.iloc[-1] * 100
        charts.line_chart(series, fig_dir / "index_long.svg",
                          "Long-run index (today = 100, log scale)", "index", log_scale=True)
        lines += [f"![Long-run index]({rel}/index_long.svg)", "",
                  "Estimated pairs give the index only at their purchase months (to 2017), "
                  "relative to the model's value today; that they track the official RVD index "
                  "through the 1997-2003 crash and the 2003-2017 rise is a check on the model "
                  "and the pairs.", ""]
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
        d = s.drawdowns
        lines += ["### Drawdown by group", ""] + _table(pd.DataFrame({
            "Pairs": d["pairs_type"], "Group": d["group"], "Pairs used": d["pairs"].map(_n),
            "Worst fall": d["drawdown"].map(lambda x: _pct(x, 0)),
            "Peak": pd.to_datetime(d["peak"]).dt.strftime("%Y-%m"),
            "Low": pd.to_datetime(d["trough"]).dt.strftime("%Y-%m")})) + [""]

    lines += ["## First-hand vs second-hand", "",
              "First-hand = the pair starts with the developer's sale (SRPE, or the project's "
              "first-sale rule). Developer prices include launch discounts and incentives, so "
              "first-hand starts are compared separately.", ""]
    if not s.first_hand.empty:
        fh = s.first_hand
        lines += _table(pd.DataFrame({
            "Pairs": fh["pairs_type"], "Start": fh["start"], "Count": fh["pairs"].map(_n),
            "Median years held": fh["median_years"].map(lambda x: f"{x:.1f}"),
            "Growth/yr": fh["growth"].map(_pct), "After costs": fh["net_growth"].map(_pct),
            "Total return": fh["total_return"].map(_pct)})) + [""]
    for key in ("recorded", "estimated"):
        v = s.verdicts.get(key)
        if v is not None and "first_hand_start" in v.index:
            r = v.loc["first_hand_start"]
            lines.append(f"- {key}: like-for-like effect {_pp(r['coef'])} pp a year "
                         f"(p {r['p']:.3f}), {'robust' if r['robust'] else 'not robust'}.")
    lines.append("")

    lines += ["## Checks", "", "### Centadata / 28Hse '% change' pairs", ""]
    gc = s.gain_check
    lines.append(
        f"Where a flat's '% change vs the previous sale' can be compared with a recorded "
        f"previous sale ({_n(gc.get('n'))} cases), the implied previous price matches within "
        f"2% in {_pct(gc.get('within_2pct'), 0)} of cases (median difference "
        f"{_pct(gc.get('median_abs_diff'), 2)}). So the field is reliable for the price. It "
        f"has no date, so {len(s.pairs['gain']):,} such pairs cannot be turned into growth "
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
        lines += ["### Model error by attribute (for estimated pairs)", "",
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
            lines.append(f"- Cost sensitivity ({k}): after-cost growth is "
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
    return [
        "## Limits", "",
        "- **Preliminary and pilot-heavy.** Recorded pairs come mostly from five pilot estates "
        "(Taikoo Shing, Mei Foo, City One, LOHAS Park, Goldwin Heights); LOHAS Park's young "
        "flats dominate the first-hand starts. The territory backfill adds full 2018-on "
        "histories for every estate.",
        "- **Short recorded window.** Free full histories start in 2018, so recorded pairs "
        "cover 2018-2026 only, a falling then recovering market. Effects in one cycle may not "
        "hold in another; the estimated pairs (1995-2017 purchases) are the long-run check.",
        "- **Estimated pairs depend on the model.** Their end price is a model value (typical "
        "error about 5%, 80% interval about ±11%). The model learns today's price differences "
        "between attributes from recent sales, so an estimated effect reads as \"today's "
        "premium vs the premium paid then\".",
        "- **Few flats have facing, sea view or rooms**: these come from listings only.",
        "- **Asking rents** anchor the yields; achieved rents are usually lower.",
        "- **No leverage, no holding costs** (rates, management fees, repairs, vacancy, tax).",
        "- **Costs assume a permanent resident who owns another flat**; a first-home buyer "
        "pays less stamp duty for 2013-2024 purchases (sensitivity above).",
        "",
        "## What changes when the backfill completes", "",
        "- Recorded pairs grow from the pilot estates to the whole territory (every resale since "
        "2018 held 3+ years), so the district split becomes a real test across many districts.",
        "- The model is retrained on more data (`hkestate train`), changing the estimated "
        "pairs' end values and the model-error check.",
        "- Rent anchoring improves as territory rental listings accumulate.",
        "- Rerun: `hkestate train`, then `hkestate report`.",
        "",
    ]
