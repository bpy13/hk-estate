"""Market today: one table per district and size class of where prices stand and which way
they are moving (plan Task 20; shown on the dashboard by Task 22).

Two views of "change", on purpose:
- the RVD price index for the size class (official, territory-wide, a month or two old);
- our own clean resales: median price per sq ft in a recent window against the window before
  (no index, so it works per district, but thin cells are noisy: the deal counts are given so
  the page can grey them).

Deals reach the register weeks after they are signed, so the newest months are incomplete.
The 3-month change therefore ends `RECENT_GAP_DAYS` (6 weeks) before the latest deal date;
the 12-month change and the 12-month median end at the latest deal date.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from hkestate import features

RECENT_GAP_DAYS = 42  # recent deals are still being registered; the 3-month change skips them
TERRITORY = "Territory"
SIZE_CLASSES = ["A", "B", "C", "D", "E"]
ALL = "ALL"
PREMIUM_MONTHS = 6  # asking price is compared with the deals of the last 6 months
# the dashboard greys a cell whose own-sales change rests on fewer deals than this
THIN_DEALS = 10

COLUMNS = [
    "district", "size_class", "median_psf_12m", "n_sales_12m",
    "idx_chg_3m", "idx_chg_12m", "own_chg_3m", "own_chg_12m", "n_own_3m", "n_own_12m",
    "sale_listings", "rent_listings", "median_ask_psf", "median_deal_psf_6m", "ask_premium",
]


def _clean_deals(raw: dict, deals: pd.DataFrame | None) -> pd.DataFrame:
    d = features.deal_features(raw) if deals is None else deals
    d = d[d["exclude_reason"].isna()]
    d = d.assign(agreement_date=pd.to_datetime(d["agreement_date"]),
                 psf_hkd=pd.to_numeric(d["psf_hkd"], errors="coerce"))
    return d.dropna(subset=["agreement_date", "psf_hkd"])[
        ["district", "rvd_class", "agreement_date", "psf_hkd"]]


def _active_listings(raw: dict, listings: pd.DataFrame | None) -> pd.DataFrame:
    """Active listings with district, size class, kind and (for sales) asking price/sq ft."""
    if listings is not None:
        return listings
    li = raw["listings"]
    li = li[li["status"] == "active"]
    b = features.building_table(raw)
    out = pd.DataFrame({
        "kind": li["kind"].to_numpy(),
        "district": li["building_id"].map(b["district"]).to_numpy(),
        "area_sqft": pd.to_numeric(li["saleable_sqft"], errors="coerce").to_numpy(),
        "price_hkd": pd.to_numeric(li["price_hkd"], errors="coerce").to_numpy(),
        "house": (li["floor_text"].eq("洋房")).to_numpy(),
    })
    out["rvd_class"] = out["area_sqft"].map(features.rvd_class)
    return out


def _asking_psf(li: pd.DataFrame) -> pd.Series:
    """Asking price per sq ft of sale listings we can value (area known, price sane)."""
    ok = ((li["kind"] == "sale") & (li["area_sqft"] > 0)
          & (li["price_hkd"] >= features.db.MIN_PRICE_HKD))
    if "house" in li:
        ok &= ~li["house"].fillna(False).astype(bool)
    return (li["price_hkd"] / li["area_sqft"]).where(ok)


def _index_change(index_df: pd.DataFrame, cls: str, months: int) -> float:
    """% change of the RVD price index for a size class, latest month vs `months` earlier."""
    if index_df is None or index_df.empty:
        return np.nan
    w = features.index_wide(index_df)
    if cls not in w or w[cls].dropna().empty:
        return np.nan
    s = w[cls].dropna()
    last = s.index[-1]
    prev = last - pd.DateOffset(months=months)
    if prev not in s.index:
        return np.nan
    return float(s[last] / s[prev] - 1)


def data_to(raw: dict, deals: pd.DataFrame | None = None) -> pd.Timestamp:
    """The latest clean-deal date: the market figures run to this day."""
    return _clean_deals(raw, deals)["agreement_date"].max().normalize()


def _window(d: pd.DataFrame, end: pd.Timestamp, months: int, back: int = 0) -> pd.Series:
    """psf of deals in the `months`-month window that ends `back` months before `end`."""
    hi = end - pd.DateOffset(months=back)
    lo = hi - pd.DateOffset(months=months)
    return d.loc[(d["agreement_date"] > lo) & (d["agreement_date"] <= hi), "psf_hkd"]


def _cell(d: pd.DataFrame, li: pd.DataFrame, index_df, cls: str, end: pd.Timestamp) -> dict:
    """Figures for one cell: deals `d` and active listings `li` already cut to it."""
    stable = end - pd.Timedelta(days=RECENT_GAP_DAYS)
    last12, prev12 = _window(d, end, 12), _window(d, end, 12, 12)
    last3, prev3 = _window(d, stable, 3), _window(d, stable, 3, 3)
    deals6 = _window(d, end, PREMIUM_MONTHS)

    def chg(now, before):
        return float(now.median() / before.median() - 1) if len(now) and len(before) else np.nan

    ask = _asking_psf(li).dropna()
    med_ask = float(ask.median()) if len(ask) else np.nan
    med6 = float(deals6.median()) if len(deals6) else np.nan
    return {
        "median_psf_12m": float(last12.median()) if len(last12) else np.nan,
        "n_sales_12m": int(len(last12)),
        "idx_chg_3m": _index_change(index_df, cls, 3),
        "idx_chg_12m": _index_change(index_df, cls, 12),
        "own_chg_3m": chg(last3, prev3),
        "own_chg_12m": chg(last12, prev12),
        "n_own_3m": int(min(len(last3), len(prev3))),
        "n_own_12m": int(min(len(last12), len(prev12))),
        "sale_listings": int((li["kind"] == "sale").sum()),
        "rent_listings": int((li["kind"] == "rent").sum()),
        "median_ask_psf": med_ask,
        "median_deal_psf_6m": med6,
        "ask_premium": med_ask / med6 - 1 if med_ask == med_ask and med6 == med6 else np.nan,
    }


def summary(raw: dict, today: date | None = None, deals: pd.DataFrame | None = None,
            listings: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per district x size class (A-E, and ALL for every size), plus the territory
    (district 'Territory', one row per class and ALL). Sorted with the territory first.

    The figures run to the latest deal date (`df.attrs["data_to"]`); `today` only caps it (a
    deal dated after `today` is a source error). Percent changes are fractions (0.05 = +5%).
    `deals` / `listings` are for tests and callers that already hold the feature tables.
    """
    d = _clean_deals(raw, deals)
    if today is not None:
        d = d[d["agreement_date"] <= pd.Timestamp(today)]
    end = d["agreement_date"].max().normalize()
    li = _active_listings(raw, listings)
    index_df = raw.get("index")

    rows = []
    districts = [None] + sorted(set(d["district"].dropna()) | set(li["district"].dropna()))
    for dist in districts:
        dd = d if dist is None else d[d["district"] == dist]
        ll = li if dist is None else li[li["district"] == dist]
        for cls in [ALL, *SIZE_CLASSES]:
            dc = dd if cls == ALL else dd[dd["rvd_class"] == cls]
            lc = ll if cls == ALL else ll[ll["rvd_class"] == cls]
            rows.append({"district": TERRITORY if dist is None else dist, "size_class": cls,
                         **_cell(dc, lc, index_df, cls, end)})
    out = pd.DataFrame(rows, columns=COLUMNS)
    out.attrs["data_to"] = end
    out.attrs["three_month_change_ends"] = end - pd.Timedelta(days=RECENT_GAP_DAYS)
    return out


def trend(raw: dict, district: str | None = None, months: int = 36,
          deals: pd.DataFrame | None = None) -> pd.DataFrame:
    """Monthly series for a chart: median price per sq ft of clean resales (and the deal
    count) per month, for one district or the territory, over the last `months` months to the
    latest deal date. The last month is usually incomplete (late registrations): the
    `partial` column marks it. `rvd_index` is the all-sizes RVD index rebased to 100 at the
    first month shown, for comparison."""
    d = _clean_deals(raw, deals)
    end = d["agreement_date"].max().normalize()
    if district is not None:
        d = d[d["district"] == district]
    start = (end - pd.DateOffset(months=months)).to_period("M").to_timestamp() + pd.DateOffset(
        months=1)
    d = d[d["agreement_date"] >= start]
    month = d["agreement_date"].dt.to_period("M").dt.to_timestamp()
    g = d.groupby(month)["psf_hkd"].agg(median_psf="median", n_sales="size")
    all_months = pd.date_range(start, end.to_period("M").to_timestamp(), freq="MS")
    out = g.reindex(all_months)
    out["n_sales"] = out["n_sales"].fillna(0).astype(int)
    out.index.name = "month"
    out["partial"] = out.index >= (end - pd.Timedelta(days=RECENT_GAP_DAYS)).to_period(
        "M").to_timestamp()
    idx = raw.get("index")
    if idx is not None and not idx.empty:
        w = features.index_wide(idx)
        if ALL in w:
            s = w[ALL].reindex(all_months).dropna()
            out["rvd_index"] = (s / s.iloc[0] * 100) if len(s) else np.nan
    return out.reset_index()
