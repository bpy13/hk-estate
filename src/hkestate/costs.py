"""Round-trip costs and rental yields for the growth study (Task 5.2).

Costs (decisions.md "Growth study"): the buyer is a Hong Kong permanent resident (HKPR) who is
*not* a first-time buyer, i.e. already owns another flat (`buyer="additional"`); a first-home
buyer (`buyer="first"`) is available as a sensitivity. Agent 1% on each side; legal fees 0.3%
once, on the purchase (decisions.md lists it without "each side"); stamp duty at the rules in
force on the purchase date. Buyer's Stamp Duty (BSD) applied only to non-HKPR and company
buyers, so never here. Special Stamp Duty (SSD) applied only to resales within 24 or 36
months; every pair is held at least 3 years, so it never applies either.

Stamp duty sources (all read 2026-10-16):
- [IRD-HIST] IRD, "Rates of Stamp Duty - Sale or Transfer of Immovable Property, From 01
  April 1988 to 25 February 2026", https://www.ird.gov.hk/eng/pdf/sd_pty_rates.pdf (linked
  from the AVD FAQ, Q2). Tables (1)-(9) below; Part B (SSD) and Part C (BSD) dates.
- [IRD-FAQ] IRD, "FAQ on Ad valorem stamp duty (AVD)", https://www.ird.gov.hk/eng/faq/avd.htm,
  Q1: rates effective from 26 February 2026 (Part 1 of Scale 1 and Scale 2, residential;
  6.5% over HK$109,574,470).
- [GIA-2026] Government press release "Tax measures proposed in 2026-27 Budget", 25 Feb
  2026, https://www.info.gov.hk/gia/general/202602/25/P2026022500841.htm: residential over
  HK$100m raised from 4.25% to 6.5% for instruments executed on or after 26 February 2026.
- [SOIPN8] IRD Stamp Office Interpretation and Practice Notes No. 8 (Revised), Nov 2018,
  https://www.ird.gov.hk/eng/pdf/soipn08.pdf, paras 2-3 and 7-8: Scale 1 from 23 February
  2013; Part 1 of Scale 1 (flat 15%) from 5 November 2016 for anyone except an HKPR not owning
  another residential property; Part 2 of Scale 1 = the 2013 Scale 1 table.
- [IRD-DSMM] IRD, "FAQ on Demand-side Management Measures for Residential Properties",
  https://www.ird.gov.hk/eng/faq/dsmm.htm: Part 1 of Scale 1 at 7.5% for agreements from 25
  October 2023 to 27 February 2024; from 28 February 2024 it equals Scale 2 (Stamp Duty
  (Amendment) Ordinance 2024).

Not confirmed (docs/open-items.md): the 2026 amendment ordinance's gazettal date (IRD pages
say 6 March and 29 May 2026); the rate applies from 26 February 2026 either way, and until
gazettal the IRD collected 4.25%. Time-of-day cut-overs (11 am on 28 Feb 2007 and 22 Feb
2023) are treated as whole days from the new table. The pre-1999 rule of rounding the price
up to the next HK$100 is ignored (at most HK$2.75 of duty).

Rental yields: RVD average rent / average price per m2 by size class and region (monthly
from 1999; earlier months by the rent and price indices), anchored to current rental
listings (`anchor_factor`). Gross: rates, management fees, vacancy and tax are not
deducted (decisions.md: total return is gross of holding costs).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

AGENT_PCT = 0.01  # each side
LEGAL_PCT = 0.003  # once, on the purchase

# A table is a list of bands (upper limit or None, kind, a, b): "flat" a = amount;
# "rate" a = share of the whole price; "marginal" a = base amount, b = share of the excess
# over the band's lower limit (marginal relief, so duty never jumps at a threshold).


def _band_table(rows):
    """[(upper, ('flat', amt) | ('rate', r) | ('marginal', base, pct))] -> table with the
    lower limits filled in for marginal bands."""
    out, lower = [], 0
    for upper, spec in rows:
        kind = spec[0]
        if kind == "marginal":
            out.append((upper, "marginal", spec[1], spec[2], lower))
        else:
            out.append((upper, kind, spec[1], None, None))
        lower = upper
    return out


F, R, M = "flat", "rate", "marginal"

# [IRD-HIST] tables (1)-(9), and [IRD-FAQ] for 26 Feb 2026 on. Before 23 Feb 2013 these were
# the only AVD rates; from then on they are "Scale 2" (SOIPN8 para 2: Scale 2 is, in essence,
# the rates in force just before 23 Feb 2013).
GENERAL_TABLES = [
    (date(1988, 4, 1), _band_table([  # (1) 01 Apr 1988 - 31 Mar 1994
        (250_000, (F, 20)), (270_057, (M, 20, 0.10)), (500_000, (R, 0.0075)),
        (544_130, (M, 3_750, 0.10)), (1_000_000, (R, 0.015)), (1_062_520, (M, 15_000, 0.10)),
        (1_500_000, (R, 0.02)), (1_655_180, (M, 30_000, 0.10)), (None, (R, 0.0275))])),
    (date(1994, 4, 1), _band_table([  # (2) 01 Apr 1994 - 31 Mar 1996
        (500_000, (F, 100)), (539_470, (M, 100, 0.10)), (1_000_000, (R, 0.0075)),
        (1_088_250, (M, 7_500, 0.10)), (2_000_000, (R, 0.015)), (2_125_020, (M, 30_000, 0.10)),
        (3_000_000, (R, 0.02)), (3_310_360, (M, 60_000, 0.10)), (None, (R, 0.0275))])),
    (date(1996, 4, 1), _band_table([  # (3) 01 Apr 1996 - 31 Mar 1997
        (750_000, (F, 100)), (809_730, (M, 100, 0.10)), (1_500_000, (R, 0.0075)),
        (1_632_350, (M, 11_250, 0.10)), (2_500_000, (R, 0.015)), (2_656_250, (M, 37_500, 0.10)),
        (3_500_000, (R, 0.02)), (3_862_080, (M, 70_000, 0.10)), (None, (R, 0.0275))])),
    (date(1997, 4, 1), _band_table([  # (4) 01 Apr 1997 - 31 Mar 1999
        (1_000_000, (F, 100)), (1_080_010, (M, 100, 0.10)), (2_000_000, (R, 0.0075)),
        (2_176_480, (M, 15_000, 0.10)), (3_000_000, (R, 0.015)), (3_187_520, (M, 45_000, 0.10)),
        (4_000_000, (R, 0.02)), (4_413_830, (M, 80_000, 0.10)), (None, (R, 0.0275))])),
    (date(1999, 4, 1), _band_table([  # (5) 01 Apr 1999 - 28 Feb 2007 (10:59:59 am)
        (1_000_000, (F, 100)), (1_080_000, (M, 100, 0.10)), (2_000_000, (R, 0.0075)),
        (2_176_470, (M, 15_000, 0.10)), (3_000_000, (R, 0.015)), (3_290_320, (M, 45_000, 0.10)),
        (4_000_000, (R, 0.0225)), (4_428_570, (M, 90_000, 0.10)), (6_000_000, (R, 0.03)),
        (6_720_000, (M, 180_000, 0.10)), (None, (R, 0.0375))])),
    (date(2007, 2, 28), _band_table([  # (6) 11 am 28 Feb 2007 - 31 Mar 2010
        (2_000_000, (F, 100)), (2_351_760, (M, 100, 0.10)), (3_000_000, (R, 0.015)),
        (3_290_320, (M, 45_000, 0.10)), (4_000_000, (R, 0.0225)), (4_428_570, (M, 90_000, 0.10)),
        (6_000_000, (R, 0.03)), (6_720_000, (M, 180_000, 0.10)), (None, (R, 0.0375))])),
    (date(2010, 4, 1), _band_table([  # (7) 01 Apr 2010 - 22 Feb 2023 (10:59:59 am)
        (2_000_000, (F, 100)), (2_351_760, (M, 100, 0.10)), (3_000_000, (R, 0.015)),
        (3_290_320, (M, 45_000, 0.10)), (4_000_000, (R, 0.0225)), (4_428_570, (M, 90_000, 0.10)),
        (6_000_000, (R, 0.03)), (6_720_000, (M, 180_000, 0.10)), (20_000_000, (R, 0.0375)),
        (21_739_120, (M, 750_000, 0.10)), (None, (R, 0.0425))])),
    (date(2023, 2, 22), _band_table([  # (8) 11 am 22 Feb 2023 - 25 Feb 2025
        (3_000_000, (F, 100)), (3_528_240, (M, 100, 0.10)), (4_500_000, (R, 0.015)),
        (4_935_480, (M, 67_500, 0.10)), (6_000_000, (R, 0.0225)), (6_642_860, (M, 135_000, 0.10)),
        (9_000_000, (R, 0.03)), (10_080_000, (M, 270_000, 0.10)), (20_000_000, (R, 0.0375)),
        (21_739_120, (M, 750_000, 0.10)), (None, (R, 0.0425))])),
    (date(2025, 2, 26), _band_table([  # (9) 26 Feb 2025 - 25 Feb 2026
        (4_000_000, (F, 100)), (4_323_780, (M, 100, 0.20)), (4_500_000, (R, 0.015)),
        (4_935_480, (M, 67_500, 0.10)), (6_000_000, (R, 0.0225)), (6_642_860, (M, 135_000, 0.10)),
        (9_000_000, (R, 0.03)), (10_080_000, (M, 270_000, 0.10)), (20_000_000, (R, 0.0375)),
        (21_739_120, (M, 750_000, 0.10)), (None, (R, 0.0425))])),
    (date(2026, 2, 26), _band_table([  # [IRD-FAQ] Q1, residential, from 26 Feb 2026
        (4_000_000, (F, 100)), (4_323_780, (M, 100, 0.20)), (4_500_000, (R, 0.015)),
        (4_935_480, (M, 67_500, 0.10)), (6_000_000, (R, 0.0225)), (6_642_860, (M, 135_000, 0.10)),
        (9_000_000, (R, 0.03)), (10_080_000, (M, 270_000, 0.10)), (20_000_000, (R, 0.0375)),
        (21_739_120, (M, 750_000, 0.10)), (100_000_000, (R, 0.0425)),
        (109_574_470, (M, 4_250_000, 0.30)), (None, (R, 0.065))])),
]

# [SOIPN8] para 7: Scale 1 (from 23 Feb 2013; "Part 2 of Scale 1" from 5 Nov 2016).
SCALE1_FROM = date(2013, 2, 23)
SCALE1_PART2 = (SCALE1_FROM, _band_table([
    (2_000_000, (R, 0.015)), (2_176_470, (M, 30_000, 0.20)), (3_000_000, (R, 0.03)),
    (3_290_330, (M, 90_000, 0.20)), (4_000_000, (R, 0.045)), (4_428_580, (M, 180_000, 0.20)),
    (6_000_000, (R, 0.06)), (6_720_000, (M, 360_000, 0.20)), (20_000_000, (R, 0.075)),
    (21_739_130, (M, 1_500_000, 0.20)), (None, (R, 0.085))]))
# [SOIPN8] para 3, 8: Part 1 of Scale 1, flat 15%, for residential property bought by anyone
# but an HKPR owning no other residential property; [IRD-DSMM] 7.5% from 25 Oct 2023; from
# 28 Feb 2024 Part 1 of Scale 1 equals Scale 2.
PART1_FLAT = [(date(2016, 11, 5), 0.15), (date(2023, 10, 25), 0.075)]
PART1_ENDS = date(2024, 2, 28)

BUYERS = ("additional", "first")


def apply_table(table, price: float) -> float:
    for upper, kind, a, b, lower in table:
        if upper is None or price <= upper:
            if kind == "flat":
                return float(a)
            if kind == "rate":
                return a * price
            return a + b * (price - lower)
    raise AssertionError("table has no open top band")


def _general_table(day: date):
    chosen = GENERAL_TABLES[0][1]
    for start, table in GENERAL_TABLES:
        if day >= start:
            chosen = table
    return chosen


def stamp_duty(price: float, day, buyer: str = "additional") -> float:
    """Ad valorem stamp duty on a residential purchase at `price` on `day` (agreement date),
    rounded up to the dollar. `buyer`: 'additional' (an HKPR who owns another flat, the
    study's default) or 'first' (an HKPR owning no other flat)."""
    if buyer not in BUYERS:
        raise ValueError(f"buyer must be one of {BUYERS}")
    day = pd.Timestamp(day).date()
    if buyer == "additional" and SCALE1_FROM <= day < PART1_ENDS:
        flat = [r for start, r in PART1_FLAT if day >= start]
        duty = flat[-1] * price if flat else apply_table(SCALE1_PART2[1], price)
    else:
        duty = apply_table(_general_table(day), price)
    return float(np.ceil(duty - 1e-9))


def round_trip(start_price: float, start_date, end_price: float,
               buyer: str = "additional") -> dict:
    duty = stamp_duty(start_price, start_date, buyer)
    buy = duty + (AGENT_PCT + LEGAL_PCT) * start_price
    return {"stamp_duty": duty, "buy_cost": buy, "sell_cost": AGENT_PCT * end_price}


def add_net_growth(pairs: pd.DataFrame, buyer: str = "additional") -> pd.DataFrame:
    """Annualised growth after round-trip costs: (end - selling costs) / (start + buying
    costs), over the holding years. Adds `buy_cost`, `sell_cost`, `net_ann_growth`."""
    p = pairs.copy()
    if p.empty:
        return p.assign(buy_cost=[], sell_cost=[], net_ann_growth=[], cost_pct=[])
    rt = [round_trip(s, d, e, buyer) for s, d, e in
          zip(p["start_price"], p["start_date"], p["end_price"], strict=True)]
    p["buy_cost"] = [r["buy_cost"] for r in rt]
    p["sell_cost"] = [r["sell_cost"] for r in rt]
    p["cost_pct"] = (p["buy_cost"] + p["sell_cost"]) / p["start_price"]
    net = (p["end_price"] - p["sell_cost"]) / (p["start_price"] + p["buy_cost"])
    p["net_ann_growth"] = np.power(net.astype(float), 1 / p["years"].astype(float)) - 1
    return p


# ---- rental yields --------------------------------------------------------------------------

# RVD regions: Hong Kong Island, Kowloon, New Territories (18 districts)
_HK = {"中西區", "灣仔區", "東區", "南區"}
_KLN = {"油尖旺區", "深水埗區", "九龍城區", "黃大仙區", "觀塘區"}


def region_of(district) -> str | None:
    if not isinstance(district, str) or not district:
        return None
    if district in _HK:
        return "HK"
    if district in _KLN:
        return "KLN"
    return "NT"


def rvd_yields(rvd: pd.DataFrame) -> pd.Series:
    """Gross yield (12 x average monthly rent / average price, per m2) by (month, class,
    region). Months before RVD's averages start (1999) are carried back with the class's rent
    and price indices (relative change since the first average month)."""
    r = rvd[(rvd["series"] == "rvd_avg_rent_psm") & (rvd["freq"] == "month")]
    p = rvd[(rvd["series"] == "rvd_avg_price_psm") & (rvd["freq"] == "month")]
    key = ["period", "class", "region"]
    m = r[key + ["value"]].merge(p[key + ["value"]], on=key, suffixes=("_rent", "_price"))
    m["period"] = pd.to_datetime(m["period"])
    m["y"] = 12 * m["value_rent"] / m["value_price"]
    y = m.set_index(key)["y"].dropna()
    if y.empty:
        return y
    ri = rvd[(rvd["series"] == "rvd_rent_index") & (rvd["region"] == "ALL")]
    pi = rvd[(rvd["series"] == "rvd_price_index") & (rvd["region"] == "ALL")]
    if not ri.empty and not pi.empty:
        idx = ri[["period", "class", "value"]].merge(pi[["period", "class", "value"]],
                                                     on=["period", "class"],
                                                     suffixes=("_r", "_p"))
        idx["period"] = pd.to_datetime(idx["period"])
        idx["ratio"] = idx["value_r"] / idx["value_p"]
        first = y.index.get_level_values("period").min()
        extra = []
        for (cls, region), s in y.groupby(level=["class", "region"]):
            ci = idx[idx["class"] == cls].set_index("period")["ratio"]
            if first not in ci.index:
                continue
            base = s.xs(first, level="period").iloc[0] if (first, cls, region) in s.index \
                else None
            if base is None:
                continue
            early = ci[ci.index < first]
            extra.append(pd.Series((base * early / ci[first]).to_numpy(),
                                   index=pd.MultiIndex.from_arrays(
                                       [early.index, [cls] * len(early), [region] * len(early)],
                                       names=key)))
        if extra:
            y = pd.concat([y, *extra]).sort_index()
    return y


def _yield_lookup(y: pd.Series) -> dict:
    """{(class, region): monthly series, forward-filled}."""
    out = {}
    for (cls, region), s in y.groupby(level=["class", "region"]):
        s = s.droplevel(["class", "region"]).sort_index()
        months = pd.date_range(s.index.min(), s.index.max(), freq="MS")
        out[(cls, region)] = s.reindex(months).ffill()
    return out


def pair_yields(pairs: pd.DataFrame, y: pd.Series, anchor: float = 1.0) -> pd.Series:
    """Average gross yield over each pair's holding period, for its size class and region,
    times the listing anchor. Missing class/region: the region's class B, then NaN."""
    look = _yield_lookup(y)
    out = []
    for s, e, cls, dist in zip(pairs["start_date"], pairs["end_date"], pairs["rvd_class"],
                               pairs["district"], strict=True):
        region = region_of(dist) or "NT"
        ser = look.get((cls, region)) if cls in ("A", "B", "C", "D", "E") else None
        ser = ser if ser is not None else look.get(("B", region))
        if ser is None or pd.isna(s) or pd.isna(e):
            out.append(np.nan)
            continue
        lo = pd.Timestamp(s).to_period("M").to_timestamp()
        hi = pd.Timestamp(e).to_period("M").to_timestamp()
        part = ser[(ser.index >= lo) & (ser.index <= hi)]
        if part.empty:  # outside RVD's range: nearest month
            part = ser.iloc[[0]] if hi < ser.index.min() else ser.iloc[[-1]]
        out.append(float(part.mean()) * anchor)
    return pd.Series(out, index=pairs.index, dtype=float)


ANCHOR_MIN_BUILDINGS = 20
ANCHOR_LIMITS = (0.5, 2.0)


def anchor_factor(rents: pd.DataFrame, sales: pd.DataFrame, y: pd.Series) -> dict:
    """How current rental listings compare with RVD's yield. `rents`: building_id, rent_psf
    (monthly asking rent per saleable sq ft); `sales`: building_id, sale_psf (recent deals),
    rvd_class, region. Per building: listing yield = 12 x median rent / median sale price per
    sq ft; the factor is the median of listing yield / RVD's latest yield for the building's
    class and region, kept within 0.5-2. Fewer than 20 buildings: factor 1 (RVD only)."""
    rent_b = rents.groupby("building_id")["rent_psf"].median()
    sale_b = sales.groupby("building_id").agg(sale_psf=("sale_psf", "median"),
                                              rvd_class=("rvd_class", "first"),
                                              region=("region", "first"))
    b = sale_b.join(rent_b, how="inner").dropna(subset=["rent_psf", "sale_psf"])
    b["listing_yield"] = 12 * b["rent_psf"] / b["sale_psf"]
    look = _yield_lookup(y) if not y.empty else {}

    def latest(cls, region):
        s = look.get((cls, region))
        s = s if s is not None else look.get(("B", region))
        return float(s.iloc[-1]) if s is not None and len(s) else np.nan

    b["rvd_yield"] = [latest(c, r) for c, r in zip(b["rvd_class"], b["region"], strict=True)]
    b = b.dropna(subset=["rvd_yield"])
    med_listing = float(b["listing_yield"].median()) if len(b) else np.nan
    med_rvd = float(b["rvd_yield"].median()) if len(b) else np.nan
    if len(b) < ANCHOR_MIN_BUILDINGS:
        return {"factor": 1.0, "basis": "rvd_only", "buildings": int(len(b)),
                "listing_median_yield": med_listing, "rvd_median_yield": med_rvd}
    f = float(np.clip((b["listing_yield"] / b["rvd_yield"]).median(), *ANCHOR_LIMITS))
    return {"factor": f, "basis": "listings", "buildings": int(len(b)),
            "listing_median_yield": med_listing, "rvd_median_yield": med_rvd}

def add_total_return(pairs: pd.DataFrame, y: pd.Series, anchor: float = 1.0) -> pd.DataFrame:
    """Total return a year = growth after round-trip costs + the average gross rental yield
    over the holding period (rent not reinvested; no leverage; gross of holding costs)."""
    p = pairs.copy()
    p["rental_yield"] = pair_yields(p, y, anchor) if not p.empty else []
    p["total_return"] = p["net_ann_growth"] + p["rental_yield"]
    return p
