"""Monthly rent estimate for a flat (Task 15 of the shortlist plan).

Order of evidence, first level with enough data wins:
  1. building: 5+ active rent listings in the same building
  2. estate: 5+ in the same estate (a big estate's phases count together)
  3. district: 5+ in the same district
  4. RVD: the average rent per m2 for the flat's size class and region (no listings enough)

Levels 1-3: median asking rent per saleable sq ft x the flat's area. Each listing's rent per
sq ft is moved to the target month with the RVD rent index of its size group (ABC / DE) before
the median, so an old listing is not compared with today's market.

Asking versus achieved. The listing anchor in `costs.anchor_factor` is
(listing yield) / (RVD yield): above 1 means asking rents are higher than RVD's average
rents relative to prices. `costs.pair_yields` multiplies RVD yields by it to bring them to
the listing level. This module follows the same direction: every estimate is at the
*listing (asking) level*. Listing-based levels are already there; the RVD level is RVD
average rent x anchor. `achieved_from_asking` goes the other way (asking / anchor), for
callers who want what RVD would call the average rent. Anchor defaults to 1 (RVD only).

Listings with impossible values are dropped before any median: rent per sq ft outside
HK$10-150 a month, or saleable area below 150 or above 5,000 sq ft (mostly entry errors: a
price typed as a rent, an area in m2, a whole-floor or car-park listing).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from . import costs, features

MIN_LISTINGS = 5
PSF_BAND = (10.0, 150.0)  # HK$ a month per saleable sq ft
AREA_BAND = (150.0, 5000.0)  # saleable sq ft


def _index_group(cls: str) -> str:
    """RVD publishes its rent index for ABC, DE and ALL (not per class)."""
    if cls in ("A", "B", "C"):
        return "ABC"
    if cls in ("D", "E"):
        return "DE"
    return "ALL"


def _month(d) -> pd.Timestamp:
    return pd.Timestamp(d).to_period("M").to_timestamp()


def clean_rent_listings(listings: pd.DataFrame) -> pd.DataFrame:
    """Active rent listings with a plausible rent, area and rent per sq ft."""
    r = listings[(listings["kind"] == "rent") & (listings["status"] == "active")].copy()
    r["rent"] = pd.to_numeric(r["price_hkd"], errors="coerce").astype(float)
    r["area"] = pd.to_numeric(r["saleable_sqft"], errors="coerce").astype(float)
    r["psf"] = r["rent"] / r["area"]
    ok = (r["area"].between(*AREA_BAND) & r["psf"].between(*PSF_BAND))
    return r[ok]


@dataclass
class RentModel:
    listings: pd.DataFrame  # cleaned rent listings: building_id, estate_key, district, ...
    building: pd.DataFrame  # index building_id; estate_key, district
    rent_index: dict  # {"ABC"|"DE"|"ALL": monthly Series, forward-filled}
    avg_rent: dict  # {(class, region): monthly RVD average rent per m2, forward-filled}
    anchor: float = 1.0
    _groups: dict = field(default_factory=dict, repr=False)
    _cache: dict = field(default_factory=dict, repr=False)

    # ---- building ------------------------------------------------------------------------
    @classmethod
    def from_snapshot(cls, raw: dict, anchor: float = 1.0) -> RentModel:
        b = features.building_table(raw)[["estate_key", "district"]]
        li = clean_rent_listings(raw["listings"])
        li = li.join(b, on="building_id")
        li["month"] = pd.to_datetime(li["last_seen"]).dt.to_period("M").dt.to_timestamp()
        return cls.from_frames(li, b, raw["rvd"], anchor)

    @classmethod
    def from_frames(cls, listings: pd.DataFrame, building: pd.DataFrame, rvd: pd.DataFrame,
                    anchor: float = 1.0) -> RentModel:
        """`listings` as made by `clean_rent_listings` plus estate_key, district and month."""
        ri = rvd[rvd["series"] == "rvd_rent_index"]
        rent_index = {}
        for g, s in ri.groupby("class"):
            s = s.assign(period=pd.to_datetime(s["period"])).set_index("period")["value"]
            s = s.sort_index()
            rent_index[g] = s.reindex(pd.date_range(s.index.min(), s.index.max(),
                                                    freq="MS")).ffill()
        ar = rvd[(rvd["series"] == "rvd_avg_rent_psm") & (rvd["freq"] == "month")]
        avg = {}
        for (c, r), s in ar.groupby(["class", "region"]):
            s = s.assign(period=pd.to_datetime(s["period"])).set_index("period")["value"]
            s = s.sort_index()
            avg[(c, r)] = s.reindex(pd.date_range(s.index.min(), s.index.max(),
                                                  freq="MS")).ffill()
        m = cls(listings=listings.copy(), building=building, rent_index=rent_index,
                avg_rent=avg, anchor=float(anchor))
        m._prepare()
        return m

    def _idx(self, group: str, month: pd.Timestamp) -> float:
        """Rent index of a group at a month; months outside the series take the nearest."""
        s = self.rent_index.get(group)
        if s is None:
            s = self.rent_index.get("ALL")
        if s is None or s.empty:
            return np.nan
        return float(s.iloc[min(max(s.index.searchsorted(month), 0), len(s) - 1)]
                     if month not in s.index else s.loc[month])

    def _prepare(self) -> None:
        li = self.listings
        li["group"] = [_index_group(features.rvd_class(a)) for a in li["area"]]
        li["idx_then"] = [self._idx(g, _month(m)) for g, m in
                          zip(li["group"], li["month"], strict=True)]
        self._cache = {}
        self._groups = {}
        for level, col in (("building", "building_id"), ("estate", "estate_key"),
                           ("district", "district")):
            self._groups[level] = {k: g for k, g in li.dropna(subset=[col]).groupby(col)}

    # ---- estimate ------------------------------------------------------------------------
    def _median_psf(self, rows: pd.DataFrame, month: pd.Timestamp) -> float:
        """Median asking rent per sq ft of `rows`, each moved to `month` by the rent index."""
        key = (id(rows), month)
        if key not in self._cache:
            now = rows["group"].map({g: self._idx(g, month) for g in rows["group"].unique()})
            adj = rows["psf"] * now / rows["idx_then"]
            self._cache[key] = float(np.nanmedian(adj)) if len(adj) else np.nan
        return self._cache[key]

    def estimate(self, building_id: str | None, area_sqft: float, district: str | None = None,
                 size_class: str | None = None, on: date | None = None
                 ) -> tuple[float, str]:
        """(monthly rent in HK$ at the listing level, basis) with basis one of 'building',
        'estate', 'district', 'rvd'; (nan, 'none') when nothing applies."""
        month = _month(on if on is not None else date.today())
        row = self.building.loc[building_id] if building_id in self.building.index else None
        keys = {"building": building_id,
                "estate": None if row is None else row["estate_key"],
                "district": district if district else (None if row is None
                                                       else row["district"])}
        if district is None and keys["district"] is not None and pd.isna(keys["district"]):
            keys["district"] = None
        for level in ("building", "estate", "district"):
            k = keys[level]
            if k is None or (isinstance(k, float) and np.isnan(k)):
                continue
            rows = self._groups[level].get(k)
            if rows is not None and len(rows) >= MIN_LISTINGS:
                psf = self._median_psf(rows, month)
                if np.isfinite(psf):
                    return psf * float(area_sqft), level
        rent = self._rvd_rent(area_sqft, keys["district"], size_class, month)
        return (rent, "rvd") if np.isfinite(rent) else (np.nan, "none")

    def _rvd_rent(self, area_sqft, district, size_class, month) -> float:
        cls = size_class if size_class in list("ABCDE") else features.rvd_class(area_sqft)
        region = costs.region_of(district) or "NT"
        s = self.avg_rent.get((cls, region))
        if s is None:
            s = self.avg_rent.get(("B", region))
        if s is None or s.empty:
            return np.nan
        last = s.index.max()
        base = float(s.loc[min(month, last)]) if month >= s.index.min() else float(s.iloc[0])
        if month > last:  # the average lags the index: carry it forward with the rent index
            g = _index_group(cls)
            base *= self._idx(g, month) / self._idx(g, last)
        psm_to_psf = 1 / features.SQFT_PER_SQM
        return base * psm_to_psf * float(area_sqft) * self.anchor

    def achieved_from_asking(self, asking_rent: float) -> float:
        """Asking rent brought down to the RVD average-rent level (the inverse of the anchor
        `costs.pair_yields` applies to RVD yields)."""
        return asking_rent / self.anchor
