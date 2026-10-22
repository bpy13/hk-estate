"""The growth study end to end (Tasks 5.1-5.3), from a database snapshot to the numbers the
findings report shows. Re-runnable: `hkestate report` rebuilds everything from the latest
snapshot.

Two steps, so several date windows can be run from one snapshot (plan 2026-10-21, Phase 4):
`prepare` builds every pair once (the slow part: pairs, costs, yields, model values);
`analyse` takes a window (`start`, `end`) and runs the regressions, checks, indices and
tables on the pairs whose two sales both fall inside it. `run` does both for one window;
`run_settings` runs the preset list (`STANDARD`) and saves each setting's results.

Saved layout (the dashboard's growth page, plan Phase 5, reads it):
`data/reports/growth/<YYYYMMDD>/<setting>/` with four CSV files, plus
`data/reports/growth/<YYYYMMDD>/settings.json`.

- `effects.csv`: one row per attribute and pair type (`recorded`, `real`, `estimated`), from
  `growth.verdicts`. Columns: `setting`; `pairs_type`; `attribute` (column name, e.g.
  `size_small`); `label` (plain English); `coef` (effect on annual log growth; x100 =
  percentage points a year); `se`; `p`; `n_with` (pairs with the attribute); `n` (pairs in
  the regression); `time_a`, `time_a_p`, `time_b`, `time_b_p` (the effect and p in each half
  of the time split); `district_a`, `district_a_p`, `district_b`, `district_b_p` (the
  district split); `time_holds`, `district_holds` (same sign in both halves); `significant`
  (p < 0.05); `robust` (significant and both splits hold; for estimated pairs also bigger
  than the model's tilt); `model_tilt` (estimated pairs only: the model's error difference
  by attribute, per holding year; blank otherwise); `verdict` (`robust`, `significant, not
  robust` or `not significant`). Empty (header only) when the setting has too few pairs.
- `districts.csv`: one row per district and pair type. Columns: `setting`; `pairs_type`;
  `district` (Chinese name, `unknown` where the building has none); `pairs`; `median_years`
  (holding period); `growth` (median annual price growth); `net_growth` (after round-trip
  costs); `rental_yield`; `total_return` (net growth + yield); `drawdown` (worst
  peak-to-trough fall of the district's repeat-sales index within the window; blank under
  100 pairs); `peak`, `trough` (months of that fall, YYYY-MM-DD).
- `index.csv`: monthly repeat-sales indices, long form. Columns: `setting`; `pairs_type`;
  `group` (`all`, an attribute such as `size_small`, its complement `not size_small`, or
  `district:<name>`); `month` (YYYY-MM-DD, first of the month); `level` (first month = 100).
  Estimated pairs give a level only at their purchase months (their end is always today).
- `returns.csv`: raw group medians (no controls), one row per group and pair type. Columns:
  `setting`; `pairs_type`; `group` (`all`, each attribute and its `not ...` complement,
  `first-hand`, `second-hand`); `pairs`; `median_years`; `growth`; `net_growth`;
  `net_growth_first` (a first-home buyer's stamp duty); `rental_yield`; `total_return`;
  `drawdown`, `peak`, `trough` (from the group's index; blank where there is none).
- `settings.json`: `date`, `snapshot_at`, `order` (setting names in run order), `settings`
  (per name: `label`, `start`, `end`, `min_hold_years` (3 for the main settings),
  `sensitivity` (true for a labelled side-check such as the 1-year-hold runs; never mixed
  into the main verdicts), `pairs` by type, `enough` (true when a regression could be run
  on recorded pairs), `note` (why not, or null), `seconds`, `checks` (the time and district
  split used per pair type)), `prepare_seconds`, `total_seconds`.

District names in `districts.csv` are Chinese; `DISTRICT_EN` maps them to English.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from hkestate import costs, features, growth, model, pairs
from hkestate.config import get_data_dir

Z80 = 1.2816  # an 80% interval's half-width is 1.2816 standard deviations (normal)
RENT_PSF_RANGE = (5.0, 200.0)  # monthly asking rent per sq ft; outside = not a real rent
ANCHOR_SALES_MONTHS = 12
INDEX_MIN_PAIRS = growth.MIN_INDEX_PAIRS
MIN_PAIRS = 200  # fewer recorded pairs than this: no regression, the setting says so
ESTIMATED_INDEX_WINDOW = ("1995-01-01", "2017-12-31")  # where estimated pairs have starts
INDEX_ATTRS = ["size_small", "size_large", "floor_high", "floor_low", "mtr_near", "mtr_far",
               "big_estate", "first_hand_start"]
VALUE_COLS = ["ann_growth", "net_ann_growth", "net_ann_growth_first", "rental_yield",
              "total_return"]


SHORT_HOLD_YEARS = 1.0  # the sensitivity runs' minimum hold (decisions.md, 2026-10-21)

# Chinese district names (as stored) -> English, kept alongside in reports and the dashboard
DISTRICT_EN = {
    "中西區": "Central and Western", "灣仔區": "Wan Chai", "東區": "Eastern", "南區": "Southern",
    "油尖旺區": "Yau Tsim Mong", "深水埗區": "Sham Shui Po", "九龍城區": "Kowloon City",
    "黃大仙區": "Wong Tai Sin", "觀塘區": "Kwun Tong", "葵青區": "Kwai Tsing",
    "荃灣區": "Tsuen Wan", "屯門區": "Tuen Mun", "元朗區": "Yuen Long", "北區": "North",
    "大埔區": "Tai Po", "沙田區": "Sha Tin", "西貢區": "Sai Kung", "離島區": "Islands",
}
# RVD's three regions, for grouping district charts (NT is split in two so a chart holds
# at most five lines)
DISTRICT_GROUPS = {
    "Hong Kong Island": ["中西區", "灣仔區", "東區", "南區"],
    "Kowloon": ["油尖旺區", "深水埗區", "九龍城區", "黃大仙區", "觀塘區"],
    "New Territories West": ["葵青區", "荃灣區", "屯門區", "元朗區", "離島區"],
    "New Territories East": ["北區", "大埔區", "沙田區", "西貢區"],
}


def district_label(name) -> str:
    """'東區' -> 'Eastern (東區)'; a name without a translation (e.g. 'unknown') as is."""
    en = DISTRICT_EN.get(name)
    return f"{en} ({name})" if en else str(name)


@dataclass(frozen=True)
class Setting:
    """A date window to run the study in: a pair counts when bought on or after `start` and
    sold (or valued) on or before `end` (None = no limit), held at least `min_hold_years`.
    A `sensitivity` is a labelled side-check (the 1-year-hold runs) shown in its own
    section, never in the main verdict table."""
    name: str
    start: str | None
    end: str | None
    label: str
    min_hold_years: float = pairs.MIN_HOLD_YEARS
    sensitivity: bool = False


# The plan's preset list (plan 2026-10-21 Task 7, from the owner's "all data in everything,
# start and end adjustable, several meaningful settings"). Phase dates follow the RVD price
# index and our own repeat-sales index: peak October 1997, low July 2003, the 2008 financial
# crisis (Lehman, September 2008), peak September 2021, low October 2024. The 3-year minimum
# hold still applies, so a phase shorter than about 4 years holds few or no pairs; such a
# setting reports "too few pairs" rather than a verdict. The two most recent phases hold
# almost none (1 and 0 on 2026-10-21), so each also runs as a clearly labelled sensitivity
# with a 1-year minimum hold (decisions.md "Recent phases get a short-hold sensitivity").
STANDARD = [
    Setting("all", None, None, "All data (every pair, 1995 on)"),
    Setting("since-2000", "2000-01-01", None, "Bought from 2000 on"),
    Setting("crash-1997-2003", "1997-10-01", "2003-07-31",
            "Bought and sold in the 1997-2003 crash (Oct 1997 peak to Jul 2003 low)"),
    Setting("recovery-2003-2008", "2003-08-01", "2008-09-30",
            "Bought and sold in the 2003-2008 recovery (to the financial crisis)"),
    Setting("boom-2008-2021", "2008-10-01", "2021-09-30",
            "Bought and sold in the 2008-2021 boom (to the Sep 2021 peak)"),
    Setting("fall-2021-2024", "2021-10-01", "2024-10-31",
            "Bought and sold in the 2021-2024 fall (Sep 2021 peak to Oct 2024 low)"),
    Setting("recent-2024-now", "2024-11-01", None, "Bought and sold since the Oct 2024 low"),
    Setting("bought-after-crash", "2003-07-01", None,
            "Bought from July 2003 on (does the crash drive the all-data result?)"),
    Setting("fall-2021-2024-1y", "2021-10-01", "2024-10-31",
            "Sensitivity: the 2021-2024 fall with a 1-year minimum hold",
            min_hold_years=SHORT_HOLD_YEARS, sensitivity=True),
    Setting("recent-2024-now-1y", "2024-11-01", None,
            "Sensitivity: since the Oct 2024 low with a 1-year minimum hold",
            min_hold_years=SHORT_HOLD_YEARS, sensitivity=True),
]


def settings_named(spec: str) -> list[Setting]:
    """`standard` (the preset list) or comma-separated setting names."""
    if spec == "standard":
        return list(STANDARD)
    by_name = {s.name: s for s in STANDARD}
    out = []
    for name in [n.strip() for n in spec.split(",") if n.strip()]:
        if name not in by_name:
            raise ValueError(f"unknown setting {name!r}; known: standard, "
                             f"{', '.join(by_name)}")
        out.append(by_name[name])
    return out


@dataclass
class Base:
    """Everything built once per snapshot, before any window is applied."""
    deals: pd.DataFrame
    pairs: dict[str, pd.DataFrame]  # real, ricacorp, recorded, estimated (with costs), gain
    short: dict[str, pd.DataFrame]  # real, ricacorp held SHORT_HOLD_YEARS to 3 years, no costs
    y: pd.Series
    anchor: dict
    bias: pd.DataFrame
    gain_check: dict
    coverage: dict
    rvd: pd.DataFrame
    rvd_long: pd.DataFrame
    notes: list[str]


@dataclass
class Study:
    pairs: dict[str, pd.DataFrame]  # by type: real, ricacorp, recorded, estimated, gain
    frames: dict[str, pd.DataFrame]  # attribute frames used in the regressions
    verdicts: dict[str, pd.DataFrame]
    gain_check: dict
    gain_signs: pd.DataFrame
    model_bias: pd.DataFrame
    indices: dict[str, dict[str, pd.Series]]
    drawdowns: pd.DataFrame
    rvd_long: pd.DataFrame
    first_hand: pd.DataFrame
    summaries: dict[str, pd.DataFrame]
    anchor: dict
    coverage: dict
    notes: list[str] = field(default_factory=list)
    rvd: pd.DataFrame = field(default_factory=pd.DataFrame)  # raw RVD series, for charts
    setting: str = "all"
    window: tuple = (None, None)  # (start, end) as given; None = no limit
    districts: pd.DataFrame = field(default_factory=pd.DataFrame)
    enough: bool = True  # enough recorded pairs for a regression
    seconds: float = 0.0
    label: str = ""
    min_hold_years: float = pairs.MIN_HOLD_YEARS
    sensitivity: bool = False


# ---- estimated pairs: the model's value of each flat today -----------------------------------


def value_now(cands: pd.DataFrame, m: model.FairPrice) -> tuple[np.ndarray, np.ndarray,
                                                                 pd.Timestamp]:
    """(value HK$, model error as a log-price standard deviation, value date) per candidate.
    Valued like a listing: at the model's valuation month, at the RVD index of its reference
    month, with the building's age then."""
    c = cands.copy()
    c["month_idx"] = m.meta["valuation_month_idx"]
    ref = pd.Timestamp(m.meta["index_reference_month"])
    c["age_years"] = ref.year - pd.to_numeric(c["built_year"], errors="coerce")
    adj = model.predict_adj(m, c)
    value = np.exp(adj) * c["area_sqft"].to_numpy(dtype=float)
    half = model.group_of(c).map(lambda g: model.q_for(m.meta["q"], g)).to_numpy(dtype=float)
    return value, half / Z80, ref + pd.Timedelta(days=14)


def model_bias(test_predictions: pd.DataFrame, deals: pd.DataFrame) -> pd.DataFrame:
    """How the model's out-of-time error differs by attribute: median signed error (model /
    actual - 1) for flats with and without each attribute. A difference means estimated pairs
    are tilted for that attribute, by about the difference / holding years a year."""
    if test_predictions is None or test_predictions.empty:
        return pd.DataFrame()
    tp = test_predictions[["source", "source_id", "pred_log_psf", "log_psf"]].merge(
        deals, on=["source", "source_id"], how="inner", suffixes=("", "_d"))
    if tp.empty:
        return pd.DataFrame()
    tp = tp.assign(start_date=tp["agreement_date"], end_date=tp["agreement_date"],
                   start_psf=tp["psf_hkd"], first_hand_start=False, ann_log_growth=0.0)
    d = growth.attribute_frame(tp)
    err = np.exp(d["pred_log_psf"] - d["log_psf"]) - 1
    rows = []
    for a in growth.ATTRIBUTES:
        if a not in d.columns or d[a].sum() < 20 or (d[a] == 0).sum() < 20:
            continue
        w, wo = err[d[a] == 1].median(), err[d[a] == 0].median()
        rows.append({"attribute": a, "n_with": int(d[a].sum()), "bias_with": w,
                     "bias_without": wo, "difference": w - wo})
    return pd.DataFrame(rows).set_index("attribute") if rows else pd.DataFrame()


# ---- rents ---------------------------------------------------------------------------------------


def listing_anchor(raw: dict, deals: pd.DataFrame, y: pd.Series) -> dict:
    """Current asking rents against the last 12 months of clean sales, per building
    (`costs.anchor_factor`)."""
    li = raw["listings"]
    r = li[(li["kind"] == "rent") & li["building_id"].notna()
           & (pd.to_numeric(li["saleable_sqft"], errors="coerce") > 0)]
    rents = pd.DataFrame({"building_id": r["building_id"],
                          "rent_psf": r["price_hkd"] / r["saleable_sqft"]})
    rents = rents[rents["rent_psf"].between(*RENT_PSF_RANGE)]
    # The window ends at the latest clean deal on or before the snapshot date, not at the
    # latest date of any stored row: one SRPE row dated 2055 (a register typo) emptied the
    # window and the anchor silently fell back to RVD alone (pre-release review 2026-10-21).
    clean = deals[deals["exclude_reason"].isna()
                  & (deals["agreement_date"] <= features.snapshot_cutoff(raw))]
    since = clean["agreement_date"].max() - pd.DateOffset(months=ANCHOR_SALES_MONTHS)
    s = clean[clean["agreement_date"] > since]
    sales = pd.DataFrame({"building_id": s["building_id"], "sale_psf": s["psf_hkd"],
                          "rvd_class": s["rvd_class"],
                          "region": s["district"].map(costs.region_of)})
    out = costs.anchor_factor(rents, sales, y)
    out["rent_listings"] = int(len(rents))
    return out


def _with_costs(p: pd.DataFrame, y: pd.Series, anchor: float) -> pd.DataFrame:
    if p.empty:
        return p
    p = costs.add_net_growth(p, "additional")
    first = costs.add_net_growth(p[["start_price", "end_price", "start_date", "years"]],
                                 "first")
    p["net_ann_growth_first"] = first["net_ann_growth"].to_numpy()
    return costs.add_total_return(p, y, anchor)


# ---- coverage ------------------------------------------------------------------------------------


def coverage(raw: dict, deals: pd.DataFrame) -> dict:
    d = deals
    # first/last sale per source over dates on or before the snapshot date: a register typo
    # (an SRPE row dated 2055) must not show as the source's latest sale
    dated = d[d["agreement_date"] <= features.snapshot_cutoff(raw)]
    by_source = d.groupby("source").agg(deals=("source_id", "size"))
    by_source[["first", "last"]] = dated.groupby("source")["agreement_date"].agg(["min", "max"])
    clean = d[d["exclude_reason"].isna()]
    years = clean.groupby(clean["agreement_date"].dt.year).size()
    runs = raw.get("run_log", pd.DataFrame())
    running = []
    if not runs.empty:
        r = runs[runs["status"] == "running"]
        running = [f"{c} (since {str(s)[:16]})" for c, s in zip(r["command"], r["started_at"],
                                                                strict=True)]
    return {
        "snapshot_at": raw["meta"].get("extracted_at"),
        "deals": len(d), "clean": int(len(clean)),
        "pilot_clean": int(clean["is_pilot"].sum()),
        "buildings_clean": int(clean["building_id"].nunique()),
        "estates_clean": int(clean["estate_key"].nunique()),
        "by_source": by_source, "clean_by_year": years,
        "exclusions": d["exclude_reason"].fillna("(clean)").value_counts(),
        "running": running,
        "feature_cover": {
            "MTR distance": float(clean["mtr_m"].notna().mean()),
            "floor position": float(clean["rel_floor"].notna().mean()),
            "facing": float((clean["facing"] != "unknown").mean()),
            "building age": float(clean["built_year"].notna().mean()),
        },
    }


# ---- step 1: everything that does not depend on the window ------------------------------------


def prepare(raw: dict, m: model.FairPrice | None = None,
            test_predictions: pd.DataFrame | None = None, log=print) -> Base:
    """Build every pair type from the snapshot, with costs and yields, once."""
    notes = []
    deals = features.deal_features(raw)
    log(f"deals {len(deals)}, clean {deals['exclude_reason'].isna().sum()}")

    # every consecutive-sale pair once (any hold); the 3-year rule splits it into the main
    # pairs and the short holds kept for the 1-year sensitivity runs
    dated = pairs.real_pairs(deals, min_years=0.0)
    real = dated[dated["years"] >= pairs.MIN_HOLD_YEARS].reset_index(drop=True)
    short_real = dated[(dated["years"] >= SHORT_HOLD_YEARS)
                       & (dated["years"] < pairs.MIN_HOLD_YEARS)].reset_index(drop=True)
    rica_all = pairs.ricacorp_pairs(raw.get("deal_pairs"), deals, min_years=SHORT_HOLD_YEARS,
                                    real=dated)
    rica = rica_all[rica_all["years"] >= pairs.MIN_HOLD_YEARS].reset_index(drop=True)
    short_rica = rica_all[rica_all["years"] < pairs.MIN_HOLD_YEARS].reset_index(drop=True)
    recorded = pd.concat([x for x in (real, rica) if not x.empty] or [real], ignore_index=True)
    gain_all = pairs.gain_pairs(deals)
    chk = pairs.gain_check(gain_all, real)
    gain = pairs.undated_gain_pairs(deals, recorded, dated=dated)

    estimated = pd.DataFrame()
    if m is not None:
        cands = pairs.estimate_candidates(deals)
        cands = cands[pd.to_numeric(cands["area_sqft"], errors="coerce") > 0]
        # only buildings the model was trained on: a building loaded since the last
        # `hkestate train` has no building effect, which would bias its estimated growth
        known = cands["building_id"].astype(str).isin(set(m.categories["building_id"]))
        if (~known).any():
            notes.append(f"{int((~known).sum()):,} estimated-pair candidates in buildings the "
                         "model has not seen yet were left out; rerun `hkestate train` first.")
        cands = cands[known]
        if not cands.empty:
            v, sd, when = value_now(cands, m)
            estimated = pairs.estimated_pairs(cands, v, when, sd)
            estimated = estimated[estimated["years"] >= pairs.MIN_HOLD_YEARS]
    else:
        notes.append("No trained model: estimated pairs skipped (run `hkestate train`).")
    log(f"pairs: real {len(real)}, ricacorp {len(rica)}, gain {len(gain)}, "
        f"estimated {len(estimated)}")

    rvd = raw.get("rvd", pd.DataFrame())
    y = costs.rvd_yields(rvd) if not rvd.empty else pd.Series(dtype=float)
    anchor = listing_anchor(raw, deals, y) if not y.empty else {"factor": 1.0,
                                                               "basis": "none"}
    # costs are per row, so they are computed once on the recorded set (real + Ricacorp)
    # and split back, not on each of the three overlapping frames
    if not y.empty:
        recorded = _with_costs(recorded, y, anchor["factor"])
        estimated = _with_costs(estimated, y, anchor["factor"])
    by_type = {"real": _of_type(recorded, "real", real),
               "ricacorp": _of_type(recorded, "ricacorp", rica),
               "recorded": recorded, "estimated": estimated, "gain": gain}
    log("costs and yields added")
    bias = model_bias(test_predictions, deals) if m is not None else pd.DataFrame()
    return Base(deals=deals, pairs=by_type, short={"real": short_real, "ricacorp": short_rica},
                y=y, anchor=anchor, bias=bias, gain_check=chk,
                coverage=coverage(raw, deals), rvd=rvd,
                rvd_long=growth.rvd_long_run(rvd) if not rvd.empty else pd.DataFrame(),
                notes=notes)


def _of_type(recorded: pd.DataFrame, pair_type: str, fallback: pd.DataFrame) -> pd.DataFrame:
    """The rows of one pair type out of the recorded set (with its cost columns); the frame
    as built when the recorded set is empty."""
    if recorded.empty or "pair_type" not in recorded.columns:
        return fallback
    return recorded[recorded["pair_type"] == pair_type].reset_index(drop=True)


# ---- step 2: one window -----------------------------------------------------------------------


def _index_set(frame: pd.DataFrame, window=None) -> tuple[dict, list]:
    """Indices for all pairs, each attribute level and each district with enough pairs;
    drawdown rows within `window`."""
    out, rows = {}, []
    if len(frame) < INDEX_MIN_PAIRS:
        return out, rows
    groups = {"all": frame}
    for a in INDEX_ATTRS:
        if a in frame.columns:
            for lvl, name in ((1, a), (0, f"not {a}")):
                part = frame[frame[a] == lvl]
                if len(part) >= INDEX_MIN_PAIRS:
                    groups[name] = part
    for dist, part in frame.groupby("district"):
        if len(part) >= INDEX_MIN_PAIRS:
            groups[f"district:{dist}"] = part
    lo, hi = window if window else (None, None)
    for name, part in groups.items():
        idx = growth.rs_index(part)
        out[name] = idx
        dd = growth.drawdown(idx, lo, hi)
        rows.append({"group": name, "pairs": len(part), **dd})
    return out, rows


def _concat(*frames: pd.DataFrame) -> pd.DataFrame:
    """Stack pair frames, leaving out empty ones (an empty frame's object-typed date columns
    would turn the dates of the others into objects)."""
    parts = [f for f in frames if not f.empty]
    return pd.concat(parts, ignore_index=True) if parts else frames[0]


def _clip_window(inner: tuple, outer: tuple) -> tuple:
    """The later start and the earlier end of two windows (None = open)."""
    starts = [pd.Timestamp(x) for x in (inner[0], outer[0]) if x is not None]
    ends = [pd.Timestamp(x) for x in (inner[1], outer[1]) if x is not None]
    return (max(starts) if starts else None, min(ends) if ends else None)


def _medians(part: pd.DataFrame) -> dict:
    out = {"pairs": int(len(part)), "median_years": float(part["years"].median())}
    for c, name in zip(VALUE_COLS, ("growth", "net_growth", "net_growth_first", "rental_yield",
                                    "total_return"), strict=True):
        out[name] = float(part[c].median()) if c in part.columns and len(part) else np.nan
    return out


def _district_rows(frames: dict, drawdowns: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for k in ("recorded", "estimated"):
        f = frames.get(k)
        if f is None or f.empty:
            continue
        dd = drawdowns[drawdowns["pairs_type"] == k].set_index("group") if not drawdowns.empty \
            else pd.DataFrame()
        for dist, part in f.groupby("district"):
            row = {"pairs_type": k, "district": dist, **_medians(part)}
            g = f"district:{dist}"
            if g in dd.index:
                row.update(drawdown=dd.loc[g, "drawdown"], peak=dd.loc[g, "peak"],
                           trough=dd.loc[g, "trough"])
            else:
                row.update(drawdown=np.nan, peak=None, trough=None)
            rows.append(row)
    return pd.DataFrame(rows)


def analyse(base: Base, start=None, end=None, name: str = "all", log=print,
            min_hold_years: float = pairs.MIN_HOLD_YEARS, label: str = "",
            sensitivity: bool = False) -> Study:
    """The study on the pairs inside [start, end] (both sales inside; None = open).
    `min_hold_years` below the 3-year rule adds the short holds (a sensitivity run: their
    costs and yields are computed here, for the window's pairs only)."""
    t0 = time.perf_counter()
    window = (start, end)
    by_type = {k: v[pairs.in_window(v, start, end)].reset_index(drop=True)
               for k, v in base.pairs.items() if k != "gain"}
    notes = list(base.notes)
    if min_hold_years < pairs.MIN_HOLD_YEARS:
        extra = {}
        for k, v in base.short.items():
            v = v[pairs.in_window(v, start, end) & (v["years"] >= min_hold_years)]
            extra[k] = _with_costs(v.reset_index(drop=True), base.y, base.anchor["factor"]) \
                if not base.y.empty else v.reset_index(drop=True)
            by_type[k] = _concat(by_type[k], extra[k])
        by_type["recorded"] = _concat(by_type["real"], by_type["ricacorp"])
        n_short = sum(len(v) for v in extra.values())
        notes.append(f"Sensitivity: minimum hold {min_hold_years:g} years instead of 3, which "
                     f"adds {n_short:,} recorded pairs held {min_hold_years:g}-3 years. "
                     "After-cost figures ignore Special Stamp Duty (10-20% on resales within "
                     "36 months until 28 Feb 2024); trust the gross growth and the verdicts, "
                     "not the after-cost numbers.")
    # gain pairs have no start date: only their (sale) end can be placed in the window
    gain = base.pairs["gain"]
    by_type["gain"] = gain[pairs.in_window(gain, None, end)].reset_index(drop=True)
    counts = {k: len(v) for k, v in by_type.items()}
    log(f"[{name}] window {start or 'start'} to {end or 'end'}, hold {min_hold_years:g}+ "
        f"years: pairs {counts}")

    frames, verdicts, summaries = {}, {}, {}
    for k in ("real", "ricacorp", "recorded", "estimated"):
        p = by_type[k]
        if p.empty:
            continue
        f = growth.attribute_frame(p)
        frames[k] = f
        summaries[k] = f[["ann_growth", "net_ann_growth", "net_ann_growth_first",
                          "rental_yield", "total_return", "years"]].describe(
            percentiles=[0.25, 0.5, 0.75]).T if "net_ann_growth" in f else pd.DataFrame()
    enough = len(frames.get("recorded", ())) >= MIN_PAIRS
    if not enough:
        notes.append(f"Too few recorded pairs in this window ({counts['recorded']:,}; a "
                     f"regression needs {MIN_PAIRS}): no verdicts. Pairs are held "
                     f"{min_hold_years:g}+ years, so a short phase holds few.")
    for k in ("recorded", "real", "estimated"):
        f = frames.get(k)
        if f is None or len(f) < MIN_PAIRS:
            continue
        if k == "real" and by_type["ricacorp"].empty and "recorded" in verdicts:
            verdicts[k] = verdicts["recorded"].copy()  # the same pairs: no second regression
            verdicts[k].attrs = dict(verdicts["recorded"].attrs)
            continue
        verdicts[k] = growth.verdicts(f)
        log(f"[{name}] verdicts on {k}: {len(f):,} pairs, "
            f"{int(verdicts[k]['robust'].sum()) if len(verdicts[k]) else 0} robust")

    bias = base.bias
    if "estimated" in verdicts and not bias.empty and len(verdicts["estimated"]):
        v = verdicts["estimated"]
        yrs = float(frames["estimated"]["years"].median())
        v["model_tilt"] = bias["difference"].reindex(v.index) / yrs
        # an estimated-pair effect counts only if it is bigger than the model's own tilt
        v["robust"] = v["robust"] & (v["coef"].abs() > v["model_tilt"].abs().fillna(0))

    # % change pairs: sign check of attribute effects on the total gain (no dates)
    gain_signs = pd.DataFrame()
    g = by_type["gain"]
    if len(g) >= MIN_PAIRS and "recorded" in verdicts and len(verdicts["recorded"]):
        g = g.assign(start_date=g["end_date"], first_hand_start=False,
                     start_psf=g["end_price"] / g["area_sqft"], ann_log_growth=g["log_gain"])
        gf = growth.attribute_frame(g)
        est = growth.fit(gf)
        rv = verdicts["recorded"]
        gain_signs = pd.DataFrame({"gain_coef": est["coef"], "gain_p": est["p"]})
        gain_signs["recorded_coef"] = rv["coef"].reindex(gain_signs.index)
        gain_signs["agrees"] = np.sign(gain_signs["gain_coef"]) == np.sign(
            gain_signs["recorded_coef"])

    indices, dd_rows = {}, []
    if "recorded" in frames:
        indices["recorded"], rows = _index_set(frames["recorded"], window)
        dd_rows += [{"pairs_type": "recorded", **r} for r in rows]
    if "estimated" in frames:
        indices["estimated"], rows = _index_set(frames["estimated"],
                                                _clip_window(ESTIMATED_INDEX_WINDOW, window))
        dd_rows += [{"pairs_type": "estimated", **r} for r in rows]
    drawdowns = pd.DataFrame(dd_rows)

    fh_rows = []
    for k in ("recorded", "estimated"):
        if k in frames:
            f = frames[k]
            for lvl, part in f.groupby("first_hand_start"):
                fh_rows.append({"pairs_type": k, "start": "first-hand" if lvl else "second-hand",
                                "pairs": len(part), "median_years": part["years"].median(),
                                "growth": part["ann_growth"].median(),
                                "net_growth": part.get("net_ann_growth",
                                                       pd.Series(dtype=float)).median(),
                                "total_return": part.get("total_return",
                                                         pd.Series(dtype=float)).median()})

    s = Study(
        pairs=by_type, frames=frames, verdicts=verdicts, gain_check=base.gain_check,
        gain_signs=gain_signs, model_bias=bias, indices=indices, drawdowns=drawdowns,
        rvd_long=base.rvd_long, first_hand=pd.DataFrame(fh_rows), summaries=summaries,
        anchor=base.anchor, coverage=base.coverage, notes=notes, rvd=base.rvd,
        setting=name, window=window, districts=_district_rows(frames, drawdowns),
        enough=enough, label=label, min_hold_years=min_hold_years, sensitivity=sensitivity)
    s.seconds = time.perf_counter() - t0
    log(f"[{name}] done in {s.seconds:.0f} s")
    return s


def run(raw: dict, m: model.FairPrice | None = None, test_predictions: pd.DataFrame | None = None,
        log=print, start=None, end=None) -> Study:
    """The whole study for one window (the default: all data)."""
    return analyse(prepare(raw, m, test_predictions, log=log), start, end, log=log)


# ---- saved results per setting ----------------------------------------------------------------


def effects_table(s: Study) -> pd.DataFrame:
    cols = ["coef", "se", "p", "n_with", "n", "time_a", "time_a_p", "time_b", "time_b_p",
            "district_a", "district_a_p", "district_b", "district_b_p", "time_holds",
            "district_holds", "significant", "robust", "model_tilt"]
    rows = []
    for k in ("recorded", "real", "estimated"):
        v = s.verdicts.get(k)
        if v is None or v.empty:
            continue
        t = v.reindex(columns=cols)
        t.insert(0, "label", v["label"])
        t.insert(0, "attribute", v.index)
        t.insert(0, "pairs_type", k)
        t.insert(0, "setting", s.setting)
        t["verdict"] = np.where(v["robust"], "robust", np.where(
            v["significant"], "significant, not robust", "not significant"))
        rows.append(t.reset_index(drop=True))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["setting", "pairs_type", "attribute", "label", *cols, "verdict"])


def returns_table(s: Study) -> pd.DataFrame:
    rows = []
    for k in ("recorded", "estimated"):
        f = s.frames.get(k)
        if f is None or f.empty:
            continue
        dd = s.drawdowns[s.drawdowns["pairs_type"] == k].set_index("group") \
            if not s.drawdowns.empty else pd.DataFrame()
        groups = [("all", f)]
        for a in growth.ATTRIBUTES:
            if a in f.columns:
                groups += [(a, f[f[a] == 1]), (f"not {a}", f[f[a] == 0])]
        groups += [("first-hand", f[f["first_hand_start"] == 1]),
                   ("second-hand", f[f["first_hand_start"] == 0])]
        for g, part in groups:
            if part.empty:
                continue
            row = {"setting": s.setting, "pairs_type": k, "group": g, **_medians(part)}
            if g in dd.index:
                row.update(drawdown=dd.loc[g, "drawdown"], peak=dd.loc[g, "peak"],
                           trough=dd.loc[g, "trough"])
            else:
                row.update(drawdown=np.nan, peak=None, trough=None)
            rows.append(row)
    return pd.DataFrame(rows, columns=["setting", "pairs_type", "group", "pairs", "median_years",
                                       "growth", "net_growth", "net_growth_first",
                                       "rental_yield", "total_return", "drawdown", "peak",
                                       "trough"])


def index_table(s: Study) -> pd.DataFrame:
    parts = []
    for k, groups in s.indices.items():
        for g, idx in groups.items():
            parts.append(pd.DataFrame({"setting": s.setting, "pairs_type": k, "group": g,
                                       "month": idx.index.strftime("%Y-%m-%d"),
                                       "level": idx.to_numpy()}))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=["setting", "pairs_type", "group", "month", "level"])


def districts_table(s: Study) -> pd.DataFrame:
    cols = ["setting", "pairs_type", "district", "pairs", "median_years", "growth", "net_growth",
            "rental_yield", "total_return", "drawdown", "peak", "trough"]
    if s.districts.empty:
        return pd.DataFrame(columns=cols)
    return s.districts.assign(setting=s.setting).reindex(columns=cols)


def _dates(df: pd.DataFrame) -> pd.DataFrame:
    for c in ("peak", "trough"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c]).dt.strftime("%Y-%m-%d")
    return df


def save_setting(s: Study, out_dir: Path) -> Path:
    """Write the four CSV files of one setting under `out_dir/<setting>/`."""
    d = out_dir / s.setting
    d.mkdir(parents=True, exist_ok=True)
    effects_table(s).to_csv(d / "effects.csv", index=False, encoding="utf-8")
    _dates(districts_table(s)).to_csv(d / "districts.csv", index=False, encoding="utf-8")
    index_table(s).to_csv(d / "index.csv", index=False, encoding="utf-8")
    _dates(returns_table(s)).to_csv(d / "returns.csv", index=False, encoding="utf-8")
    return d


def growth_dir(today: date | None = None) -> Path:
    today = today or date.today()
    return get_data_dir() / "reports" / "growth" / f"{today:%Y%m%d}"


def setting_meta(s: Study, setting: Setting) -> dict:
    """The settings.json entry for one run."""
    note = next((n for n in s.notes if n.startswith("Too few")), None)
    return {"label": setting.label, "start": setting.start, "end": setting.end,
            "min_hold_years": setting.min_hold_years, "sensitivity": setting.sensitivity,
            "pairs": {k: int(len(v)) for k, v in s.pairs.items()}, "enough": s.enough,
            "note": note, "seconds": round(s.seconds, 1),
            "checks": {k: dict(v.attrs.get("checks", {})) for k, v in s.verdicts.items()}}


def run_settings(raw: dict, m: model.FairPrice | None, test_predictions: pd.DataFrame | None,
                 settings: list[Setting], log=print, today: date | None = None,
                 out_dir: Path | None = None) -> dict[str, Study]:
    """Prepare once, then run and save each setting; returns the studies by name.

    A second run on the same day merges into that day's settings.json: settings not run
    again keep their entry and their place in `order`, while `date`, `snapshot_at` and
    `prepare_seconds` are overwritten by this run's. So after a run on a newer snapshot the
    file's `snapshot_at` describes only the settings run last; rerun `standard` to make every
    entry come from one snapshot."""
    t0 = time.perf_counter()
    out_dir = out_dir or growth_dir(today)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = prepare(raw, m, test_predictions, log=log)
    prep_s = time.perf_counter() - t0
    log(f"prepared all pairs in {prep_s:.0f} s")
    meta_path = out_dir / "settings.json"
    meta = json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else {}
    meta.update({"date": f"{today or date.today():%Y-%m-%d}",
                 "snapshot_at": base.coverage.get("snapshot_at"),
                 "prepare_seconds": round(prep_s, 1)})
    meta.setdefault("settings", {})
    meta["order"] = [x for x in meta.get("order", []) if x not in {s.name for s in settings}]
    studies = {}
    for st in settings:
        s = analyse(base, st.start, st.end, name=st.name, log=log,
                    min_hold_years=st.min_hold_years, label=st.label,
                    sensitivity=st.sensitivity)
        studies[st.name] = s
        save_setting(s, out_dir)
        meta["settings"][st.name] = setting_meta(s, st)
        meta["order"].append(st.name)
        meta["total_seconds"] = round(time.perf_counter() - t0, 1)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str),
                             "utf-8")
    log(f"settings saved under {out_dir} ({meta['total_seconds']:.0f} s in all)")
    return studies
