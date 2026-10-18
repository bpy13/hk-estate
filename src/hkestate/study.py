"""The growth study end to end (Tasks 5.1-5.3), from a database snapshot to the numbers the
findings report shows. Re-runnable: `hkestate report` rebuilds everything from the latest
snapshot, so the report follows the data as the Phase 3 loads finish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from hkestate import costs, features, growth, model, pairs

Z80 = 1.2816  # an 80% interval's half-width is 1.2816 standard deviations (normal)
RENT_PSF_RANGE = (5.0, 200.0)  # monthly asking rent per sq ft; outside = not a real rent
ANCHOR_SALES_MONTHS = 12
INDEX_MIN_PAIRS = growth.MIN_INDEX_PAIRS
ESTIMATED_INDEX_WINDOW = ("1995-01-01", "2017-12-31")  # where estimated pairs have starts
RECORDED_INDEX_WINDOW = ("2018-01-01", None)  # where recorded pairs are dense (28Hse 2018 on)
INDEX_ATTRS = ["size_small", "size_large", "floor_high", "floor_low", "mtr_near", "mtr_far",
               "big_estate", "first_hand_start"]


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
    li = raw["listings"]
    r = li[(li["kind"] == "rent") & li["building_id"].notna()
           & (pd.to_numeric(li["saleable_sqft"], errors="coerce") > 0)]
    rents = pd.DataFrame({"building_id": r["building_id"],
                          "rent_psf": r["price_hkd"] / r["saleable_sqft"]})
    rents = rents[rents["rent_psf"].between(*RENT_PSF_RANGE)]
    since = deals["agreement_date"].max() - pd.DateOffset(months=ANCHOR_SALES_MONTHS)
    s = deals[deals["exclude_reason"].isna() & (deals["agreement_date"] > since)]
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
    by_source = d.groupby("source").agg(deals=("source_id", "size"),
                                        first=("agreement_date", "min"),
                                        last=("agreement_date", "max"))
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
        "quarantined": raw["meta"].get("quarantined_rows"),
        "running": running,
        "feature_cover": {
            "MTR distance": float(clean["mtr_m"].notna().mean()),
            "floor position": float(clean["rel_floor"].notna().mean()),
            "facing": float((clean["facing"] != "unknown").mean()),
            "building age": float(clean["built_year"].notna().mean()),
        },
    }


# ---- the whole study ----------------------------------------------------------------------


def _index_set(frame: pd.DataFrame, window=None) -> tuple[dict, list]:
    """Indices for all pairs and each attribute level with enough pairs; drawdown rows."""
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
    for name, part in groups.items():
        idx = growth.rs_index(part)
        out[name] = idx
        lo, hi = window if window else (None, None)
        dd = growth.drawdown(idx, lo, hi)
        rows.append({"group": name, "pairs": len(part), **dd})
    return out, rows


def run(raw: dict, m: model.FairPrice | None = None, test_predictions: pd.DataFrame | None = None,
        log=print) -> Study:
    notes = []
    deals = features.deal_features(raw)
    log(f"deals {len(deals)}, clean {deals['exclude_reason'].isna().sum()}")

    real = pairs.real_pairs(deals)
    rica = pairs.ricacorp_pairs(raw.get("deal_pairs"), deals, real=real)
    recorded = pd.concat([x for x in (real, rica) if not x.empty] or [real], ignore_index=True)
    gain_all = pairs.gain_pairs(deals)
    chk = pairs.gain_check(gain_all, real)
    gain = pairs.undated_gain_pairs(deals, recorded)

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
    by_type = {"real": real, "ricacorp": rica, "recorded": recorded, "estimated": estimated}
    by_type = {k: _with_costs(v, y, anchor["factor"]) if not y.empty else v
               for k, v in by_type.items()}
    by_type["gain"] = gain

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
        if len(f) >= 200:
            verdicts[k] = growth.verdicts(f)

    bias = model_bias(test_predictions, deals) if m is not None else pd.DataFrame()
    if "estimated" in verdicts and not bias.empty:
        v = verdicts["estimated"]
        yrs = float(frames["estimated"]["years"].median())
        v["model_tilt"] = bias["difference"].reindex(v.index) / yrs
        # an estimated-pair effect counts only if it is bigger than the model's own tilt
        v["robust"] = v["robust"] & (v["coef"].abs() > v["model_tilt"].abs().fillna(0))

    # % change pairs: sign check of attribute effects on the total gain (no dates)
    gain_signs = pd.DataFrame()
    if len(gain) >= 200 and "recorded" in verdicts:
        g = gain.assign(start_date=gain["end_date"], first_hand_start=False,
                        start_psf=gain["end_price"] / gain["area_sqft"],
                        ann_log_growth=gain["log_gain"])
        gf = growth.attribute_frame(g)
        est = growth.fit(gf)
        rv = verdicts["recorded"]
        gain_signs = pd.DataFrame({"gain_coef": est["coef"], "gain_p": est["p"]})
        gain_signs["recorded_coef"] = rv["coef"].reindex(gain_signs.index)
        gain_signs["agrees"] = np.sign(gain_signs["gain_coef"]) == np.sign(
            gain_signs["recorded_coef"])

    indices, dd_rows = {}, []
    if "recorded" in frames:
        indices["recorded"], rows = _index_set(frames["recorded"], RECORDED_INDEX_WINDOW)
        dd_rows += [{"pairs_type": "recorded", **r} for r in rows]
    if "estimated" in frames:
        indices["estimated"], rows = _index_set(frames["estimated"], ESTIMATED_INDEX_WINDOW)
        dd_rows += [{"pairs_type": "estimated", **r} for r in rows]

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

    return Study(
        pairs=by_type, frames=frames, verdicts=verdicts, gain_check=chk,
        gain_signs=gain_signs, model_bias=bias, indices=indices,
        drawdowns=pd.DataFrame(dd_rows), rvd_long=growth.rvd_long_run(rvd) if not rvd.empty
        else pd.DataFrame(), first_hand=pd.DataFrame(fh_rows), summaries=summaries,
        anchor=anchor, coverage=coverage(raw, deals), notes=notes, rvd=rvd)


def today() -> date:
    return date.today()
