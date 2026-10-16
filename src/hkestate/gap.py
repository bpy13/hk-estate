"""Asking-vs-deal gap (Task 4.3): how far below the asking price flats actually sell.

A pair is a sale listing and a clean deal on the same unit, the deal agreed between the
listing's first sighting and 90 days after its last. The unit is matched on building + flat
+ exact floor when the listing gives one; most listings give only a band (高層/中層/低層),
so otherwise building + flat + floor band + saleable area within 2% (the cross-site
duplicate rule). Listings without a flat or band can't be paired. Each deal pairs with the
nearest listing in time, each listing with one deal.

Per district: with 30+ pairs, the median gap; otherwise the 5% default, marked `assumed`
(decisions.md "Fair price"). Listings started on 2026-10-16, so for now every district is
`assumed`; the measured gaps take over as listings and deals accumulate.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hkestate.normalise import parse_floor

DEFAULT_GAP = 0.05
MIN_PAIRS = 30
WINDOW_DAYS = 90
AREA_TOL = 0.02
_BAND_WORDS = {"低層": "low", "中層": "mid", "高層": "high"}


def _band_of_rel(rel) -> str | None:
    if rel is None or pd.isna(rel):
        return None
    return "low" if rel < 1 / 3 else ("mid" if rel < 2 / 3 else "high")


def _listing_unit(row) -> tuple[str | None, str | None]:
    """(exact floor label, band) a listing states."""
    text = row.get("floor_text")
    fl = parse_floor(text) if isinstance(text, str) and text else None
    if fl and fl.num is not None and fl.kind in ("number", "ground"):
        return fl.label, None
    band = _BAND_WORDS.get(fl.label) if fl and fl.kind == "band" else None
    band = band or (row.get("floor_band") if row.get("floor_band") in ("low", "mid", "high")
                    else None)
    return None, band


def pairs(listings: pd.DataFrame, deals: pd.DataFrame) -> pd.DataFrame:
    """Listing-deal pairs on the same unit (see module docstring), with the gap
    1 - deal price / asking price."""
    cols = ["source", "listing_id", "source_id", "district", "asking", "deal_price", "gap",
            "days_after_last_seen", "match"]
    li = listings[(listings.get("kind", "sale") == "sale") & listings["building_id"].notna()
                  & listings["flat"].notna() & (listings["price_hkd"] > 0)].copy()
    dl = deals[deals["exclude_reason"].isna() & deals["building_id"].notna()
               & deals["flat"].notna()].copy()
    if li.empty or dl.empty:
        return pd.DataFrame(columns=cols)
    units = li.apply(_listing_unit, axis=1, result_type="expand")
    li["floor_exact"], li["band"] = units[0], units[1]
    li = li[li["floor_exact"].notna() | li["band"].notna()]
    dl["band"] = dl["rel_floor"].map(_band_of_rel)
    m = li.merge(dl, on=["building_id", "flat"], suffixes=("", "_deal"))
    if m.empty:
        return pd.DataFrame(columns=cols)
    exact = m["floor_exact"].notna() & (m["floor_exact"] == m["floor_label"])
    banded = (m["floor_exact"].isna() & (m["band"] == m["band_deal"])
              & ((m["area_sqft"] / m["saleable_sqft"] - 1).abs() <= AREA_TOL))
    first = pd.to_datetime(m["first_seen"])
    last = pd.to_datetime(m["last_seen"])
    when = m["agreement_date"]
    in_time = (when >= first) & (when <= last + pd.Timedelta(days=WINDOW_DAYS))
    m = m[(exact | banded) & in_time].copy()
    if m.empty:
        return pd.DataFrame(columns=cols)
    m["match"] = np.where(m["floor_exact"].notna(), "exact_floor", "band_area")
    m["days_after_last_seen"] = (m["agreement_date"] - pd.to_datetime(m["last_seen"])).dt.days
    m["dist"] = m["days_after_last_seen"].abs()
    # each deal with its nearest listing, then each listing with one deal
    m = m.sort_values("dist").drop_duplicates(["source_deal", "source_id"])
    m = m.drop_duplicates(["source", "listing_id"])
    m["asking"] = m["price_hkd"]
    m["deal_price"] = m["price_hkd_deal"]
    m["gap"] = 1 - m["deal_price"] / m["asking"]
    m["district"] = m["district"].fillna(m.get("district_deal"))
    return m[cols].reset_index(drop=True)


def district_gaps(p: pd.DataFrame, default: float = DEFAULT_GAP,
                  min_pairs: int = MIN_PAIRS) -> pd.DataFrame:
    """Per district: pairs, measured median gap, and the gap used (`basis` measured or
    assumed)."""
    if p.empty:
        return pd.DataFrame(columns=["district", "n_pairs", "median_gap", "gap", "basis"])
    g = p.dropna(subset=["district"]).groupby("district")["gap"].agg(["size", "median"])
    g = g.rename(columns={"size": "n_pairs", "median": "median_gap"}).reset_index()
    measured = g["n_pairs"] >= min_pairs
    g["gap"] = np.where(measured, g["median_gap"], default)
    g["basis"] = np.where(measured, "measured", "assumed")
    return g


def gap_for(gaps: pd.DataFrame, district, default: float = DEFAULT_GAP) -> tuple[float, str]:
    if gaps.empty or district is None or (isinstance(district, float) and np.isnan(district)):
        return default, "assumed"
    row = gaps[gaps["district"] == district]
    if row.empty:
        return default, "assumed"
    return float(row.iloc[0]["gap"]), str(row.iloc[0]["basis"])
