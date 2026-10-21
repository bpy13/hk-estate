"""Repeat-sales pairs for the growth study (Task 5.1).

A pair is two sales of the same flat: bought at `start_price` on `start_date`, sold (or
valued) at `end_price` on `end_date`. Four kinds, always kept apart (`pair_type`):

- `real`: two recorded sales (28Hse, Centadata, SRPE, Ricacorp current deals) of the same unit,
  consecutive among the clean deals, held at least 3 years (decisions.md "Growth study").
- `ricacorp`: a recent resale whose previous deal comes from Ricacorp's "days held" and
  "% gain" (date and price derived, accurate to about 1%; sources.md).
- `gain`: a Centadata or 28Hse deal with its "% change vs the previous deal": the previous
  price is known but not its date, so it cannot be annualised. Used only as a check
  (`gain_check`, and the sign of attribute effects on the total gain).
- `estimated`: a flat whose latest sale was before 2018, against the fair-price model's value
  of it today. Lower confidence: the end price is a model estimate (error carried in
  `ann_sd_model`).

"Clean" is the shared definition in `features.deal_features` (`exclude_reason` is None):
not a duplicate, house, parking, bulk, outlier, price-check, non-market or derived row.
A developer (first-hand) sale may start a pair and is tagged `first_hand_start`; it must
pass the other checks too, except the bulk rule (a launch sells many flats on one day).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_HOLD_YEARS = 3.0
DAYS_PER_YEAR = 365.25
FIRST_HAND_REASONS = {"first_hand", "first_hand_rule"}
MAX_AREA_CHANGE = 0.10  # the two deals' areas differ by more: not the same flat as recorded
ESTIMATE_BEFORE = pd.Timestamp("2018-01-01")  # full histories start in 2018 (28Hse)
MIN_START = pd.Timestamp("1993-01-01")  # the RVD index (and the model) start here

# Unit attributes carried onto each pair (from the end deal; for estimated pairs, the start)
ATTRS = ["building_id", "district", "estate_key", "estate_name", "building_name", "is_pilot",
         "rel_floor", "floor_num", "top_floor", "facing", "sea_view", "special_unit", "mtr_m",
         "estate_units", "built_year", "rvd_class", "rooms", "lat", "lon"]

PAIR_COLUMNS = ["pair_type", "unit_id", "start_date", "end_date", "start_price", "end_price",
                "start_source", "end_source", "years", "log_growth", "ann_growth",
                "first_hand_start", "area_sqft", "start_psf"]


def _bool(s: pd.Series) -> pd.Series:
    return s.fillna(False).astype(bool)


def eligible(deals: pd.DataFrame) -> pd.Series:
    """Deals that may take part in a pair: clean, or a developer sale that passes the other
    checks (no parking, not under HK$500k, not non-market, not an outlier, has an area)."""
    reason = deals["exclude_reason"]
    clean = reason.isna()
    fh = reason.isin(FIRST_HAND_REASONS)
    fh_ok = (fh & ~_bool(deals["parking"]) & ~_bool(deals["excl_low_price"])
             & ~_bool(deals["excl_non_market"]) & ~_bool(deals["excl_outlier"])
             & (pd.to_numeric(deals["area_sqft"], errors="coerce") > 0))
    has = deals["unit_id"].notna() & deals["agreement_date"].notna() & (
        deals["agreement_date"] >= MIN_START)
    return (clean | fh_ok) & has


def _attrs(df: pd.DataFrame) -> pd.DataFrame:
    return df[[c for c in ATTRS if c in df.columns]]


def _finish(p: pd.DataFrame) -> pd.DataFrame:
    p["years"] = (p["end_date"] - p["start_date"]).dt.days / DAYS_PER_YEAR
    p["log_growth"] = np.log(p["end_price"].astype(float) / p["start_price"].astype(float))
    p["ann_growth"] = np.exp(p["log_growth"] / p["years"]) - 1
    p["ann_log_growth"] = p["log_growth"] / p["years"]
    return p


def real_pairs(deals: pd.DataFrame, min_years: float = MIN_HOLD_YEARS) -> pd.DataFrame:
    """Consecutive eligible sales of each unit held at least `min_years`."""
    d = deals[eligible(deals)].copy()
    d["fh"] = d["exclude_reason"].isin(FIRST_HAND_REASONS)
    # the same sale from two sources on one day counts once (the clean copy first)
    d = d.sort_values(["unit_id", "agreement_date", "fh"]).drop_duplicates(
        ["unit_id", "agreement_date"])
    g = d.groupby("unit_id")
    prev = pd.DataFrame({
        "start_date": g["agreement_date"].shift(), "start_price": g["price_hkd"].shift(),
        "start_source": g["source"].shift(), "start_area": g["area_sqft"].shift(),
        "start_psf": g["psf_hkd"].shift(), "first_hand_start": g["fh"].shift()})
    p = pd.concat([d, prev], axis=1)
    p = p[p["start_date"].notna() & ~p["fh"]]  # a developer sale can only start a pair
    out = pd.DataFrame({
        "pair_type": "real", "unit_id": p["unit_id"], "start_date": p["start_date"],
        "end_date": p["agreement_date"], "start_price": p["start_price"],
        "end_price": p["price_hkd"], "start_source": p["start_source"],
        "end_source": p["source"], "first_hand_start": _bool(p["first_hand_start"]),
        "area_sqft": p["area_sqft"], "start_psf": p["start_psf"]})
    area_a = pd.to_numeric(p["start_area"], errors="coerce")
    area_b = pd.to_numeric(p["area_sqft"], errors="coerce")
    same_flat = ~((area_a - area_b).abs() > MAX_AREA_CHANGE * area_b)
    out = pd.concat([out[same_flat], _attrs(p[same_flat])], axis=1)
    out = _finish(out)
    return out[out["years"] >= min_years].reset_index(drop=True)


def ricacorp_pairs(deal_pairs: pd.DataFrame, deals: pd.DataFrame,
                   min_years: float = MIN_HOLD_YEARS,
                   real: pd.DataFrame | None = None) -> pd.DataFrame:
    """Ricacorp previous deals (derived from days held and % gain) against the clean current
    deal they belong to. Pairs whose previous deal is a stored sale are real pairs already.
    The 1% price match can miss on a derived price, so when `real` is given, pairs ending on
    the same unit within 90 days of a real pair's end are dropped (the same sale twice)."""
    if deal_pairs is None or deal_pairs.empty:
        return pd.DataFrame(columns=PAIR_COLUMNS)
    dp = deal_pairs[_bool(deal_pairs["prev_derived"])].copy()
    cur = deals[deals["exclude_reason"].isna()].drop_duplicates(["source", "source_id"])
    cur = cur.set_index(["source", "source_id"])
    keys = pd.MultiIndex.from_arrays([dp["cur_source"], dp["cur_id"]])
    dp = dp[keys.isin(cur.index)]
    if dp.empty:
        return pd.DataFrame(columns=PAIR_COLUMNS)
    c = cur.loc[pd.MultiIndex.from_arrays([dp["cur_source"], dp["cur_id"]])].reset_index()
    dp = dp.reset_index(drop=True)
    built = pd.to_numeric(c["built_year"], errors="coerce")
    prev_date = pd.to_datetime(dp["prev_date"])
    out = pd.DataFrame({
        "pair_type": "ricacorp", "unit_id": c["unit_id"], "start_date": prev_date,
        "end_date": c["agreement_date"], "start_price": dp["prev_price_hkd"].astype(float),
        "end_price": c["price_hkd"].astype(float), "start_source": "ricacorp",
        "end_source": c["source"],
        # a previous deal within a year of completion is most likely the developer's sale
        "first_hand_start": (prev_date.dt.year <= built + 1).fillna(False),
        "area_sqft": c["area_sqft"],
        "start_psf": dp["prev_price_hkd"].astype(float) / c["area_sqft"]})
    out = pd.concat([out, _attrs(c)], axis=1)
    out = out.drop_duplicates(["unit_id", "start_date", "end_date"])
    out = _finish(out)
    out = out[(out["years"] >= min_years) & (out["start_date"] >= MIN_START)].reset_index(
        drop=True)
    if real is not None and not real.empty and not out.empty:
        out = out[~_matches_end(out, real)].reset_index(drop=True)
    return out


def gain_pairs(deals: pd.DataFrame, exclude: pd.DataFrame | None = None) -> pd.DataFrame:
    """Deals with a '% change vs the previous deal' (Centadata, 28Hse): previous price known,
    date unknown. A duplicate copy still carries its gain (the clean copy may lack it).
    Pairs whose end is already the end of a real pair are left out (`exclude`)."""
    if "gain_pct" not in deals.columns:
        return pd.DataFrame(columns=["unit_id", "end_date", "start_price", "end_price",
                                     "log_gain"])
    ok = deals["exclude_reason"].isna() | deals["exclude_reason"].eq("duplicate")
    g = deals[ok & deals["gain_pct"].notna() & deals["unit_id"].notna()
              & (deals["gain_pct"] > -95)].copy()
    g = g.sort_values(["unit_id", "agreement_date", "exclude_reason"], na_position="first")
    g = g.drop_duplicates(["unit_id", "agreement_date"])
    out = pd.DataFrame({
        "pair_type": "gain", "unit_id": g["unit_id"], "start_date": pd.NaT,
        "end_date": g["agreement_date"],
        "start_price": g["price_hkd"].astype(float) / (1 + g["gain_pct"] / 100.0),
        "end_price": g["price_hkd"].astype(float), "end_source": g["source"],
        "area_sqft": g["area_sqft"]})
    out["log_gain"] = np.log(out["end_price"] / out["start_price"])
    out = pd.concat([out, _attrs(g)], axis=1).reset_index(drop=True)
    if exclude is not None and not exclude.empty:
        out = out[~_matches_end(out, exclude)].reset_index(drop=True)
    return out


def undated_gain_pairs(deals: pd.DataFrame, recorded: pd.DataFrame) -> pd.DataFrame:
    """Gain pairs whose start date is still unknown. A deal whose previous sale is stored
    (any hold, however short) has a dated start now: held 3+ years it is a real pair, shorter
    it is dropped like any short hold, but it must not stay behind as an undated gain pair.
    `recorded` are the real and Ricacorp pairs already built."""
    dated = real_pairs(deals, min_years=0.0)
    frames = [x for x in (recorded, dated) if x is not None and not x.empty]
    return gain_pairs(deals, exclude=pd.concat(frames, ignore_index=True) if frames else None)


def in_window(p: pd.DataFrame, start=None, end=None) -> pd.Series:
    """True where a pair falls inside the study window: bought on or after `start` and sold
    (or valued) on or before `end`; `None` means no limit on that side (decisions.md
    "History: all data, adjustable window"). A pair with no start date fails a `start`
    limit: it cannot be shown to be inside."""
    ok = pd.Series(True, index=p.index)
    if p.empty:
        return ok
    if start is not None:
        ok &= pd.to_datetime(p["start_date"]) >= pd.Timestamp(start)
    if end is not None:
        ok &= pd.to_datetime(p["end_date"]) <= pd.Timestamp(end)
    return ok


def _matches_end(g: pd.DataFrame, real: pd.DataFrame, days: int = 90) -> pd.Series:
    """True where a real pair ends on the same unit within `days` of the gain pair's end."""
    r = real[["unit_id", "end_date"]].assign(end_date=lambda x: pd.to_datetime(x["end_date"]))
    m = g.reset_index().merge(r, on="unit_id", suffixes=("", "_r"))
    hit = (pd.to_datetime(m["end_date"]) - m["end_date_r"]).dt.days.abs() <= days
    idx = set(m.loc[hit, "index"])
    return pd.Series([i in idx for i in g.index], index=g.index)


def gain_check(gain: pd.DataFrame, real: pd.DataFrame, days: int = 90) -> dict:
    """How well a '% change' previous price agrees with the recorded previous sale, where a
    real pair ends on the same deal: median and 90th percentile absolute log difference."""
    m = gain.merge(real[["unit_id", "end_date", "start_price"]], on="unit_id",
                   suffixes=("", "_real"))
    m = m[(m["end_date"] - m["end_date_real"]).dt.days.abs() <= days]
    if m.empty:
        return {"n": 0, "median_abs_diff": np.nan, "p90_abs_diff": np.nan,
                "within_2pct": np.nan}
    diff = np.abs(np.log(m["start_price"] / m["start_price_real"].astype(float)))
    return {"n": int(len(m)), "median_abs_diff": float(np.median(diff)),
            "p90_abs_diff": float(np.quantile(diff, 0.9)),
            "within_2pct": float((diff <= 0.02).mean())}


def estimate_candidates(deals: pd.DataFrame, before: pd.Timestamp = ESTIMATE_BEFORE
                        ) -> pd.DataFrame:
    """Flats whose latest sale (any kind except a second copy of the same sale) was an
    eligible sale before `before`: the start of an estimated pair. Rows are the start deals,
    with every feature column, so the fair-price model can value the flat today."""
    rows = deals[deals["unit_id"].notna() & deals["agreement_date"].notna()
                 & ~deals["exclude_reason"].eq("duplicate")]
    last = rows["agreement_date"].groupby(rows["unit_id"]).transform("max")
    latest = rows[rows["agreement_date"].eq(last)]
    # any other kind of sale on the latest day (e.g. a price-check row) disqualifies the flat
    n_kinds = latest.groupby("unit_id")["source_id"].transform("size")
    ok = eligible(latest) & (n_kinds == 1) & (latest["agreement_date"] < before)
    c = latest[ok].copy()
    c["first_hand_start"] = c["exclude_reason"].isin(FIRST_HAND_REASONS)
    return c.reset_index(drop=True)


def estimated_pairs(candidates: pd.DataFrame, value_now, value_date: pd.Timestamp,
                    sigma) -> pd.DataFrame:
    """Estimated pairs: the start deal against the model's value today (`value_now`, HK$),
    with the model's error as a standard deviation in log price (`sigma`), carried into
    the annualised growth as `ann_sd_model`."""
    c = candidates
    out = pd.DataFrame({
        "pair_type": "estimated", "unit_id": c["unit_id"], "start_date": c["agreement_date"],
        "end_date": pd.Timestamp(value_date), "start_price": c["price_hkd"].astype(float),
        "end_price": np.asarray(value_now, dtype=float), "start_source": c["source"],
        "end_source": "model", "first_hand_start": _bool(c["first_hand_start"]),
        "area_sqft": c["area_sqft"], "start_psf": c["psf_hkd"]})
    out = pd.concat([out, _attrs(c)], axis=1)
    out = _finish(out)
    out["sd_model"] = np.asarray(sigma, dtype=float)
    out["ann_sd_model"] = out["sd_model"] / out["years"]
    return out.reset_index(drop=True)
