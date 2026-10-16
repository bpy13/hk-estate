"""Ranked listings (Task 4.4): every active sale listing valued by the fair-price model and
sorted by its discount to fair price. No cut-off (decisions.md: "all is relative").

discount = 1 - expected deal price / fair price, where the expected deal price is the asking
price less the district's usual asking-vs-deal gap (gap.py; 5% `assumed` until a district
has 30 listing-deal pairs). Positive = the listing should sell below the model's value.

Shown alongside, never changing the order: the 80% interval, confidence (`low` with fewer
than 10 comparable deals in the same building in the last 24 months), keyword flags, and
the top 3 SHAP reasons. Listings on several sites (same `dup_group`) are ranked once, at the
lowest asking price, with the others in `also_listed`.

Outputs: `data/reports/ranked_listings_<date>.csv` (UTF-8 with BOM so Excel shows Chinese)
and `data/model/ranked_latest.parquet` (with SHAP values, for the dashboard).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from hkestate import features, gap, model
from hkestate.config import get_data_dir

LOW_CONFIDENCE_COMPS = 10
COMPS_MONTHS = 24


def discount(asking, fair, gap_pct):
    """1 - expected deal price / fair price (positive = below fair price)."""
    return 1 - np.asarray(asking, dtype=float) * (1 - np.asarray(gap_pct, dtype=float)) / (
        np.asarray(fair, dtype=float))


@dataclass
class Ranked:
    table: pd.DataFrame  # one row per valued listing, best discount first
    gaps: pd.DataFrame  # per district
    counts: dict


def _comps(deals: pd.DataFrame, today: date) -> pd.DataFrame:
    since = pd.Timestamp(today) - pd.DateOffset(months=COMPS_MONTHS)
    c = deals[deals["exclude_reason"].isna() & (deals["agreement_date"] > since)]
    return c.groupby("building_id").agg(comps_24m=("source_id", "size"),
                                        comps_median_psf=("psf_hkd", "median"))


def _display_values(row) -> dict:
    vals = {f: row.get(f) for f in model.FEATURES}
    vals["building_id"] = row.get("building_name") or row.get("building_id")
    vals["estate_key"] = row.get("estate_name") or row.get("estate_key")
    return vals


def _floor_text(row) -> str:
    if row["floor_basis"] == "exact":
        return f"{row['floor_num']:.0f}/F"
    if row["floor_basis"] == "band":
        return f"{row['floor_band'] if row['floor_band'] != 'unknown' else 'band'} band"
    return "not given"


def rank(raw: dict, m: model.FairPrice, today: date | None = None) -> Ranked:
    today = today or date.today()
    deals = features.deal_features(raw)
    listings = features.listing_features(raw, today)
    not_valued = listings["value_note"].value_counts().to_dict()
    li = listings[listings["value_note"].isna()].copy()
    # value at the last well-populated month (model.valuation_month), not the current one
    li["month_idx"] = m.meta.get("valuation_month_idx", li["month_idx"])

    # asking-vs-deal gap per district, from every sale listing seen (active or delisted)
    b = features.building_table(raw)
    sale = raw["listings"][raw["listings"]["kind"] == "sale"].join(b[["district"]],
                                                                   on="building_id")
    gaps = gap.district_gaps(gap.pairs(sale, deals))

    # value: index-adjusted prediction, moved by any index change since the model's
    # reference month (zero unless RVD has published a newer month since training)
    adj = model.predict_adj(m, li)
    ref = pd.Timestamp(m.meta["index_reference_month"])
    when = pd.Series([pd.Timestamp(today)] * len(li))
    logd = (np.log(model.deflator(raw["index"], li["rvd_class"], when))
            - np.log(model.deflator(raw["index"], li["rvd_class"], pd.Series([ref] * len(li)))))
    li["fair_psf"] = np.exp(adj + logd)
    li["fair_price_hkd"] = li["fair_psf"] * li["area_sqft"]
    li["group"] = model.group_of(li)
    half = li["group"].map(lambda g: model.q_for(m.meta["q"], g))
    li["fair_low_hkd"] = li["fair_price_hkd"] * np.exp(-half)
    li["fair_high_hkd"] = li["fair_price_hkd"] * np.exp(half)

    gb = [gap.gap_for(gaps, d) for d in li["district"]]
    li["gap_pct"] = [g for g, _ in gb]
    li["gap_basis"] = [basis for _, basis in gb]
    li["asking_hkd"] = li["price_hkd"]
    li["asking_psf"] = li["price_hkd"] / li["area_sqft"]
    li["expected_deal_hkd"] = li["asking_hkd"] * (1 - li["gap_pct"])
    li["discount_pct"] = discount(li["asking_hkd"], li["fair_price_hkd"], li["gap_pct"])

    comps = _comps(deals, today)
    li["comps_24m"] = li["building_id"].map(comps["comps_24m"]).fillna(0).astype(int)
    li["comps_median_psf"] = li["building_id"].map(comps["comps_median_psf"])
    li["confidence"] = np.where(li["comps_24m"] < LOW_CONFIDENCE_COMPS, "low", "normal")

    shap = model.predict_shap(m, li)
    shap.index = li.index
    li = pd.concat([li, shap], axis=1)
    reasons = [model.reasons(li.loc[i, shap.columns], _display_values(li.loc[i]))
               for i in li.index]
    for k in range(3):
        li[f"reason_{k + 1}"] = [r[k] if len(r) > k else None for r in reasons]
    li["floor"] = li.apply(_floor_text, axis=1)

    # one row per cross-site duplicate group, at the lowest asking price
    li["key"] = li["source"] + ":" + li["listing_id"]
    li["dup_group"] = li["dup_group"].fillna(li["key"])
    li = li.sort_values(["dup_group", "asking_hkd", "key"])
    others = li.groupby("dup_group")["key"].agg(list)
    li = li.drop_duplicates("dup_group", keep="first").copy()
    li["also_listed"] = [", ".join(k for k in others[g] if k != key) or None
                         for g, key in zip(li["dup_group"], li["key"], strict=True)]

    li = li.sort_values("discount_pct", ascending=False).reset_index(drop=True)
    li.insert(0, "rank", np.arange(1, len(li) + 1))
    counts = {"active_sale_listings": len(listings), "ranked": len(li),
              "duplicates_merged": int(listings["value_note"].isna().sum()) - len(li),
              "not_valued": {str(k): int(v) for k, v in not_valued.items()},
              "gap_pairs": int(gaps["n_pairs"].sum()) if not gaps.empty else 0}
    return Ranked(li, gaps, counts)


CSV_COLUMNS = [
    "rank", "discount_pct", "source", "listing_id", "url", "estate_name", "building_name",
    "district", "floor", "flat", "area_sqft", "rooms", "facing", "age_years", "asking_hkd",
    "asking_psf", "gap_pct", "gap_basis", "expected_deal_hkd", "fair_price_hkd", "fair_low_hkd",
    "fair_high_hkd", "fair_psf", "confidence", "comps_24m", "comps_median_psf", "flags",
    "reason_1", "reason_2", "reason_3", "group", "floor_basis", "first_seen", "also_listed",
]


def write(r: Ranked, today: date | None = None) -> dict[str, Path]:
    today = today or date.today()
    reports = get_data_dir() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    csv_path = reports / f"ranked_listings_{today:%Y%m%d}.csv"
    out = r.table[CSV_COLUMNS].copy()
    for c in ("asking_hkd", "expected_deal_hkd", "fair_price_hkd", "fair_low_hkd",
              "fair_high_hkd"):
        out[c] = out[c].round(-3)
    for c in ("asking_psf", "fair_psf", "comps_median_psf"):
        out[c] = out[c].round(0)
    for c in ("discount_pct", "gap_pct"):
        out[c] = out[c].round(4)
    out.to_csv(csv_path, index=False, encoding="utf-8-sig")
    gap_path = reports / f"asking_gap_{today:%Y%m%d}.csv"
    r.gaps.to_csv(gap_path, index=False, encoding="utf-8-sig")
    mdir = model.model_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    parquet_path = mdir / "ranked_latest.parquet"
    r.table.assign(ranked_on=str(today)).to_parquet(parquet_path, index=False)
    return {"csv": csv_path, "gaps": gap_path, "parquet": parquet_path}
