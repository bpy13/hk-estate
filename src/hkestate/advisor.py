"""Decision helper (plan 2026-10-23, Tasks 18-19): from a person's profile to ranked estates
and listings, each with a 0-100 score per goal, an overall weighted score and a one-line
reason per goal; plus the money questions, buy-vs-rent (`buy_vs_rent`) and sell-vs-let
(`sell_vs_let`, `advise_owner`).

Steps, in order:
1. Candidates: for `buy`, the ranked sale listings (rank.py) with no `exclude_reason` (a data
   check failed), the same flat listed twice at different prices kept once at the cheaper
   price; for `rent`, the active rent listings with their rent estimate (rent.py).
2. Hard limits (each row is kept or counted out, never scored around): budget, size, rooms,
   commute, school net. A limit on a value the data does not have (no coordinates, so no
   commute; no net) counts the row out unless `Profile.allow_unknown`.
3. Scores per goal (docs/decisions.md "Decision helper scoring"), NaN when the data is
   missing; the row's reason says "unknown" and the overall score counts it as 0, so missing
   data never reads as good.
4. Overall = weighted average of the goal scores by the profile's weights. Estates are ranked
   by the mean overall score of their listings; listings follow in estate order.
5. Buy: each listing gets a "vs renting a similar flat" line over the profile's horizon, at
   the district's growth (low / mid / high from the growth study).

Data comes in through `AdvisorData` (`load_data` reads the snapshot, the ranked parquet, the
growth study's CSVs, the fair-price model and the MTR files; tests build it from small
frames). The live database is never opened here.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from hkestate import commute, costs, features, growth, model, mortgage, rent
from hkestate.config import get_data_dir

GOALS = ("investment", "near_place", "school", "space")
INTENTS = ("buy", "rent", "sell", "let")
GOAL_LABELS = {"investment": "Investment", "near_place": "Near a place",
               "school": "Primary school net", "space": "Space"}

# ---- scoring constants (one line each in docs/decisions.md) ----------------------------------
GROWTH_FULL_PCT = 8.0          # district price growth of 8%/yr (or more) scores 100
ATTR_PP_TO_POINTS = 25.0       # attribute effects: 50 + 25 points per percentage point a year
YIELD_FULL_PCT = 6.0           # a 6% rent yield scores 100 (3% = 50)
DISCOUNT_TO_POINTS = 500.0     # 50 + 500 x discount: 10% below fair price = 100, 10% above = 0
COMMUTE_BEST_MIN = 15.0        # 15 minutes or less scores 100
COMMUTE_WORST_MIN = 60.0       # 60 minutes or more scores 0
DEFAULT_MIN_SQFT = 400.0       # size reference when the profile gives no minimum
SIZE_FULL_MULTIPLE = 2.0       # twice the minimum size scores 100 (the minimum itself 50)
AGE_POINTS_PER_YEAR = 2.0      # building age: 100 - 2 a year (50 years old = 0)
SPACE_SIZE_SHARE = 0.75        # space = 0.75 size + 0.25 age
ROBUST_ONLY = True             # attribute effects: only the study's robust verdicts count
DISTRICT_YIELD_PULL = 0.5      # a district-level rent estimate: yield pulled halfway to the
#                                district's median yield (growth study); building and estate
#                                estimates count in full, an RVD-average estimate not at all
SAME_FLAT_TOLERANCE = 0.02     # same building, floor and flat, area within 2%: one flat

# ---- buy-vs-rent and sell-vs-let assumptions (Task 19; docs/decisions.md) -------------------
# Rates: 5% of the rateable value, taken here as the flat's annual rent (Rating and
# Valuation Department, "Rates", rvd.gov.hk; the progressive 8% / 12% steps above a rateable
# value of HK$550,000 from 2024-25 are not modelled). Government rent: 3% of the rateable
# value (Lands Department, Government Rent (Assessment and Collection) Ordinance, Cap. 515;
# older Hong Kong Island and Kowloon leases pay a nominal rent instead, not modelled).
RATES_PCT = 0.05
GOV_RENT_PCT = 0.03
MGMT_FEE_SHARE = 0.10          # management fee as a share of rent: a guess (HK$3-5 a sq ft a
#                                month is usual, about a tenth of the rent)
DEPOSIT_RETURN = 0.03          # what the deposit earns elsewhere a year: a guess (HKD time
#                                deposits and bonds paid about 2-3.5% in 2025-26)
PROPERTY_TAX = 0.15            # IRD property tax: 15% of the net assessable value = rent
PROPERTY_TAX_ALLOWANCE = 0.20  # received less rates paid, less a 20% statutory allowance
TENANCY_YEARS = 2              # a tenancy runs two years (common practice; a guess)
LETTING_FEE_MONTHS = 0.5       # the landlord pays half a month's rent per new tenancy (usual)
VACANCY_MONTHS = 1.0           # empty for a month at each change of tenant (a guess)
GROWTH_SPREAD_GUESS = 0.03     # low/high = mid -/+ 3 pp when the study has no index to use
GROWTH_MID_GUESS = 0.03        # mid when the study has nothing for the district (long-run
#                                Hong Kong flat prices grew about 3%/yr; a guess)
GROWTH_CASES = ("low", "mid", "high")


@dataclass
class Profile:
    """What the person wants. Money is HK$; `budget_hkd` is the highest price (buy) or the
    highest monthly rent (rent). `stations` are MTR station codes (ADM = Admiralty).
    `income_hkd` is monthly; when given, a buy budget is also tested with HKMA mortgage rules
    (mortgage.affordable). `allow_unknown` keeps rows a hard limit cannot judge."""
    intent: str = "buy"
    weights: dict = field(default_factory=lambda: dict.fromkeys(GOALS, 1.0))
    budget_hkd: float | None = None
    min_sqft: float | None = None
    min_rooms: int | None = None
    stations: tuple[str, ...] = ()
    max_commute_min: float | None = None
    school_net: str | None = None
    years: int = 10
    buyer: str = "first"
    income_hkd: float | None = None
    rate: float = 0.035
    mortgage_years: int = 30
    allow_unknown: bool = False

    def __post_init__(self):
        if self.intent not in INTENTS:
            raise ValueError(f"intent must be one of {INTENTS}, got {self.intent!r}")
        unknown = set(self.weights) - set(GOALS)
        if unknown:
            raise ValueError(f"unknown goals {sorted(unknown)}; goals are {GOALS}")
        self.weights = {g: float(self.weights.get(g, 0.0)) for g in GOALS}
        if any(w < 0 for w in self.weights.values()):
            raise ValueError("goal weights must be 0 or more")
        if not any(self.weights.values()):
            self.weights = dict.fromkeys(GOALS, 1.0)
        self.stations = tuple(self.stations)


@dataclass
class AdvisorData:
    """Everything `recommend` reads. Frames use the snapshot's column names."""
    sale: pd.DataFrame                 # rank.py's table (one row per valued sale listing)
    rent_listings: pd.DataFrame        # active rent listings with building columns attached
    buildings: pd.DataFrame            # index building_id: lat, lon, mtr_m, district, ...
    district_growth: pd.DataFrame      # index district: growth, rental_yield, total_return
    effects: pd.DataFrame              # index attribute: coef, verdict (recorded pairs)
    stations: list[tuple[str, float, float]] = field(default_factory=list)  # (code, lat, lon)
    station_names: dict[str, str] = field(default_factory=dict)  # code -> English name
    graph: dict = field(default_factory=dict)  # commute.Graph
    rent_model: rent.RentModel | None = None
    today: date = field(default_factory=date.today)
    growth_setting: str = ""
    district_index: pd.DataFrame = field(default_factory=pd.DataFrame)  # district, month, level
    price_index: pd.DataFrame = field(default_factory=pd.DataFrame)     # snapshot's RVD index
    fair_model: model.FairPrice | None = None                            # for `fair_value`


@dataclass
class Result:
    estates: pd.DataFrame     # one row per estate, best first
    listings: pd.DataFrame    # one row per listing, in estate order then best first
    left_out: dict            # rows counted out by each hard limit
    notes: list[str]
    weights: dict


# ---- loading ---------------------------------------------------------------------------------


def read_stations(csv_text: str) -> tuple[list[tuple[str, float, float]], dict[str, str]]:
    """Stations with coordinates from CSV text with columns code, name_en, lat, lon (the
    `mtr_station` table's columns; an override for the snapshot's stations frame)."""
    return station_rows(pd.DataFrame(list(csv.DictReader(io.StringIO(csv_text.lstrip("﻿"))))))


def station_rows(stations: pd.DataFrame) -> tuple[list[tuple[str, float, float]],
                                                  dict[str, str]]:
    """(code, lat, lon) for every station with coordinates, and code -> English name, from
    the snapshot's `stations` frame (code, name_zh, name_en, lat, lon)."""
    rows, names = [], {}
    if stations.empty or "code" not in stations:
        return rows, names
    for r in stations.to_dict("records"):
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (np.isfinite(lat) and np.isfinite(lon)):
            continue
        rows.append((r["code"], lat, lon))
        name = r.get("name_en")
        names[r["code"]] = name if isinstance(name, str) and name else r["code"]
    return rows, names


def growth_tables(growth_dir: Path, setting: str = "all",
                  pairs_type: str = "recorded") -> tuple[pd.DataFrame, pd.DataFrame]:
    """(district growth, attribute effects) of one growth-study setting, from its CSVs."""
    d = growth_dir / setting
    districts = pd.read_csv(d / "districts.csv", encoding="utf-8")
    districts = districts[districts["pairs_type"] == pairs_type].set_index("district")
    effects = pd.read_csv(d / "effects.csv", encoding="utf-8")
    effects = effects[effects["pairs_type"] == pairs_type].set_index("attribute")
    return districts, effects


def growth_index(growth_dir: Path, setting: str = "all",
                 pairs_type: str = "recorded") -> pd.DataFrame:
    """The growth study's monthly repeat-sales index per district (plus `all`), long form:
    district, month, level. Empty when the setting has no index file."""
    path = growth_dir / setting / "index.csv"
    if not path.exists():
        return pd.DataFrame(columns=["district", "month", "level"])
    idx = pd.read_csv(path, encoding="utf-8")
    if idx.empty:
        return pd.DataFrame(columns=["district", "month", "level"])
    idx = idx[(idx["pairs_type"] == pairs_type)
              & (idx["group"].str.startswith("district:") | (idx["group"] == "all"))].copy()
    idx["district"] = idx["group"].str.replace("district:", "", regex=False)
    idx["month"] = pd.to_datetime(idx["month"])
    return idx[["district", "month", "level"]].reset_index(drop=True)


def rent_candidates(raw: dict, buildings: pd.DataFrame) -> pd.DataFrame:
    """Active rent listings with a plausible rent and area, plus building columns."""
    li = rent.clean_rent_listings(raw["listings"])
    cols = ["district", "lat", "lon", "mtr_m", "built_year", "estate_key", "estate_units",
            "estate_name", "building_name"]
    if "school_net" in buildings:
        cols.append("school_net")
    li = li.drop(columns=[c for c in cols if c in li]).join(buildings[cols], on="building_id")
    li["area_sqft"] = li["area"]
    li["rooms"] = pd.to_numeric(li["rooms"], errors="coerce")
    li["age_years"] = pd.to_numeric(li["age_years"], errors="coerce")
    li["key"] = li["source"] + ":" + li["listing_id"]
    return li


def load_data(snapshot_dir: Path | None = None, ranked_path: Path | None = None,
              growth_dir: Path | None = None, setting: str = "all",
              stations_csv: Path | None = None, mtr_csv: Path | None = None,
              school_nets: pd.Series | None = None, today: date | None = None,
              model_dir: Path | None = None) -> AdvisorData:
    """Read the saved files (never the live database). School nets and MTR stations come
    from the snapshot (`buildings.school_net`, `stations.parquet`); `school_nets` (a
    building_id -> net Series) and `stations_csv` (code, name_en, lat, lon) override them.
    `mtr_csv` is the MTR open-data lines file (commute graph); without it there is no
    commute. The fair-price model is loaded from `model_dir` when its files are there."""
    data_dir = get_data_dir()
    raw = features.load_snapshot(snapshot_dir)
    buildings = features.building_table(raw)
    if "school_net" not in buildings:
        buildings["school_net"] = None
    if school_nets is not None:
        buildings["school_net"] = buildings.index.map(school_nets)
    ranked_path = ranked_path or features.model_dir() / "ranked_latest.parquet"
    sale = pd.read_parquet(ranked_path) if Path(ranked_path).exists() else pd.DataFrame()
    if not sale.empty:
        sale["school_net"] = sale["building_id"].map(buildings["school_net"])
    if growth_dir is None:
        root = data_dir / "reports" / "growth"
        dated = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        growth_dir = dated[-1] if dated else None
    if growth_dir is not None and (Path(growth_dir) / setting).is_dir():
        districts, effects = growth_tables(Path(growth_dir), setting)
        index = growth_index(Path(growth_dir), setting)
    else:
        districts, effects, index = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    if stations_csv is not None and Path(stations_csv).exists():
        stations, names = read_stations(Path(stations_csv).read_text("utf-8"))
    else:
        stations, names = station_rows(raw.get("stations", pd.DataFrame()))
    graph = {}
    if mtr_csv is not None and Path(mtr_csv).exists():
        graph = commute.build_graph(Path(mtr_csv).read_text("utf-8"),
                                    {c: (la, lo) for c, la, lo in stations})
    rent_model = rent.RentModel.from_snapshot(raw) if not raw["rvd"].empty else None
    model_dir = model_dir or features.model_dir()
    fair = model.load(model_dir) if (Path(model_dir) / "fair_price.txt").exists() else None
    return AdvisorData(sale=sale, rent_listings=rent_candidates(raw, buildings),
                       buildings=buildings, district_growth=districts, effects=effects,
                       stations=stations, station_names=names, graph=graph,
                       rent_model=rent_model, today=today or date.today(),
                       growth_setting=setting if not districts.empty else "",
                       district_index=index, price_index=raw["index"], fair_model=fair)


# ---- candidates ------------------------------------------------------------------------------

_KEEP = ["key", "url", "source", "listing_id", "building_id", "estate_key", "estate_name",
         "building_name", "district", "area_sqft", "rooms", "age_years", "lat", "lon", "mtr_m",
         "rel_floor", "estate_units", "special_unit", "facing", "sea_view", "school_net",
         "floor", "flat"]


def _candidates(profile: Profile, data: AdvisorData) -> tuple[pd.DataFrame, list[str]]:
    notes = []
    if profile.intent == "buy":
        c = data.sale.copy()
        if c.empty:
            return c, ["no ranked sale listings"]
        bad = c["exclude_reason"].notna()
        notes.append(f"{int(bad.sum())} sale listings with a data check reason left out")
        c = c[~bad].copy()
        c["price_hkd"] = c["asking_hkd"].astype(float)
        c["fair_hkd"] = c["fair_price_hkd"].astype(float)
        c, merged = _keep_cheaper_of_same_flat(c)
        if merged:
            notes.append(f"{merged} listings of a flat already listed cheaper merged into it")
    else:
        c = data.rent_listings.copy()
        if c.empty:
            return c, ["no active rent listings"]
        c["price_hkd"] = c["rent"].astype(float)
        for col in ("discount_pct", "confidence", "fair_hkd"):
            c[col] = np.nan
    for col in _KEEP:
        if col not in c:
            c[col] = np.nan
    c["estate_key"] = c["estate_key"].where(c["estate_key"].notna(), c["building_id"])
    c["estate_name"] = c["estate_name"].where(c["estate_name"].notna(), c["building_name"])
    c = c.reset_index(drop=True)
    c["rent_est"], c["rent_basis"] = _rent_estimates(c, data)
    c["commute_min"], c["commute_text"] = _commutes(c, profile, data)
    return c, notes


def _keep_cheaper_of_same_flat(c: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """rank.py merges the same flat listed twice only when the prices are within 2%; two
    listings of one flat (same building, floor and flat, area within 2%) at prices further
    apart both reach here. Keep the cheaper, name the others in `also_listed`, and return how
    many were merged. A blank floor or flat never matches (it could be another flat)."""
    if c.empty or "floor" not in c or "flat" not in c:
        return c, 0
    if "also_listed" not in c:
        c["also_listed"] = None
    c = c.sort_values("price_hkd", kind="stable").reset_index(drop=True)  # unique labels
    floor = c["floor"].astype(object)
    flat = c["flat"].astype(object)
    known = (floor.notna() & ~floor.isin(["", "not given", "unknown"])
             & flat.notna() & ~flat.isin(["", "unknown"]) & c["building_id"].notna())
    drop, extra = [], {}
    for _, rows in c[known].groupby(["building_id", "floor", "flat"], sort=False):
        if len(rows) < 2:
            continue
        kept = rows.index[0]
        area = float(rows["area_sqft"].iloc[0])
        same = rows.iloc[1:]
        same = same[(same["area_sqft"] - area).abs() <= SAME_FLAT_TOLERANCE * area]
        if same.empty:
            continue
        drop += list(same.index)
        extra[kept] = list(same["key"])
    for kept, keys in extra.items():
        old = c.at[kept, "also_listed"]
        c.at[kept, "also_listed"] = ", ".join(([old] if isinstance(old, str) and old else [])
                                              + keys)
    return c.drop(index=drop), len(drop)


def _rent_estimates(c: pd.DataFrame, data: AdvisorData) -> tuple[pd.Series, pd.Series]:
    """Monthly asking-level rent estimate per listing (one call per building and area)."""
    if data.rent_model is None:
        return (pd.Series(np.nan, index=c.index), pd.Series("none", index=c.index))
    cache: dict[tuple, tuple[float, str]] = {}
    est, basis = [], []
    for b, area, district in zip(c["building_id"], c["area_sqft"], c["district"], strict=True):
        k = (b, area, district)
        if k not in cache:
            if not area > 0:
                cache[k] = (np.nan, "none")
            else:
                cache[k] = data.rent_model.estimate(
                    b if isinstance(b, str) else None, float(area),
                    district if isinstance(district, str) else None, on=data.today)
        e, bs = cache[k]
        est.append(e)
        basis.append(bs)
    return pd.Series(est, index=c.index, dtype=float), pd.Series(basis, index=c.index)


def _commutes(c: pd.DataFrame, profile: Profile, data: AdvisorData
              ) -> tuple[pd.Series, pd.Series]:
    """Average door-to-station minutes over the profile's stations (NaN = unknown) and the
    text for the reason, e.g. '32 min to Admiralty, 41 to Central'."""
    minutes = pd.Series(np.nan, index=c.index)
    text = pd.Series("", index=c.index, dtype=object)
    if not profile.stations:
        return minutes, text
    if not data.stations or not data.graph:
        return minutes, pd.Series("commute unknown (no station data)", index=c.index)
    known = {code for code, _, _ in data.stations}
    missing = [s for s in profile.stations if s not in known]
    if missing:
        raise ValueError(f"unknown MTR station codes {missing}")
    cache: dict[tuple[float, float], tuple[float, str]] = {}
    for i, (lat, lon) in enumerate(zip(c["lat"], c["lon"], strict=True)):
        if not (pd.notna(lat) and pd.notna(lon)):
            text.iloc[i] = "commute unknown (no coordinates)"
            continue
        k = (float(lat), float(lon))
        if k not in cache:
            legs = []
            for s in profile.stations:
                m = commute.minutes(k[0], k[1], s, data.stations, data.graph)
                legs.append((s, m))
            if any(m is None for _, m in legs):
                cache[k] = (np.nan, "commute unknown (no route)")
            else:
                avg = float(np.mean([m for _, m in legs]))
                parts = [f"{m:.0f} min to {data.station_names.get(s, s)}" if n == 0
                         else f"{m:.0f} to {data.station_names.get(s, s)}"
                         for n, (s, m) in enumerate(legs)]
                cache[k] = (avg, ", ".join(parts))
        minutes.iloc[i], text.iloc[i] = cache[k]
    return minutes, text


# ---- hard limits -----------------------------------------------------------------------------


def _within_budget(c: pd.DataFrame, profile: Profile) -> pd.Series:
    ok = pd.Series(True, index=c.index)
    if profile.budget_hkd is not None:
        ok &= c["price_hkd"] <= profile.budget_hkd
    if profile.intent == "buy" and profile.income_hkd:
        ok &= pd.Series([mortgage.affordable(p, profile.income_hkd, profile.rate,
                                             profile.mortgage_years, profile.buyer).passes
                         for p in c["price_hkd"]], index=c.index)
    return ok


def _apply_limits(c: pd.DataFrame, profile: Profile) -> tuple[pd.DataFrame, dict]:
    """Rows that pass every hard limit, and how many each limit counted out (in order, so a
    row counts once). An unknown value fails a limit unless the profile allows it."""
    allow = profile.allow_unknown

    def judge(value: pd.Series, passes: pd.Series) -> pd.Series:
        return passes.where(value.notna(), allow)

    limits = {"budget": _within_budget(c, profile)}
    if profile.min_sqft is not None:
        limits["size"] = judge(c["area_sqft"], c["area_sqft"] >= profile.min_sqft)
    if profile.min_rooms is not None:
        limits["rooms"] = judge(c["rooms"], c["rooms"] >= profile.min_rooms)
    if profile.max_commute_min is not None and profile.stations:
        limits["commute"] = judge(c["commute_min"], c["commute_min"] <= profile.max_commute_min)
    if profile.school_net is not None:
        net = c["school_net"].astype(object)
        limits["school_net"] = judge(net, net == str(profile.school_net))
    keep = pd.Series(True, index=c.index)
    left_out = {}
    for name, ok in limits.items():
        ok = ok.fillna(False).astype(bool)
        left_out[name] = int((keep & ~ok).sum())
        keep &= ok
    return c[keep].copy(), left_out


# ---- scores ----------------------------------------------------------------------------------


def _clip(x) -> float:
    return float(np.clip(x, 0.0, 100.0))


def _attribute_pp(row, effects: pd.DataFrame) -> tuple[float, list[str]]:
    """Percentage points a year from the growth study's attribute effects that apply to this
    flat (robust verdicts only), and the attributes named."""
    if effects.empty:
        return 0.0, []
    area = row["area_sqft"]
    rel, mtr = row.get("rel_floor"), row.get("mtr_m")
    units = row.get("estate_units")
    flags = {
        "size_small": area < growth.SQFT_A,
        "size_large": growth.SQFT_B <= area < growth.SQFT_C,
        "size_xl": area >= growth.SQFT_C,
        "floor_low": pd.notna(rel) and rel < 1 / 3,
        "floor_high": pd.notna(rel) and rel > 2 / 3,
        "mtr_near": pd.notna(mtr) and mtr < growth.NEAR_MTR,
        "mtr_far": pd.notna(mtr) and mtr > growth.FAR_MTR,
        "big_estate": pd.notna(units) and units >= growth.BIG_ESTATE,
        "special_unit": bool(row.get("special_unit")) if pd.notna(row.get("special_unit"))
        else False,
        "facing_south": row.get("facing") in growth.SOUTH,
        "sea_view": bool(row.get("sea_view")) if pd.notna(row.get("sea_view")) else False,
    }
    total, named = 0.0, []
    for attr, applies in flags.items():
        if not applies or attr not in effects.index:
            continue
        e = effects.loc[attr]
        if ROBUST_ONLY and e["verdict"] != "robust":
            continue
        pp = float(e["coef"]) * 100
        total += pp
        named.append(f"{attr.replace('_', ' ')} {pp:+.1f}")
    return total, named


def _yield_part(row, district, data: AdvisorData) -> tuple[float, str]:
    """The rent-yield part of the investment score, by how much the rent estimate can be
    trusted: a building or estate estimate counts in full; a district estimate is pulled
    halfway to the district's median yield (the growth study's `rental_yield`), or counts
    half when the study has none, and never above that median; an RVD-average estimate (no listings near the flat at all)
    scores unknown. The reason names the basis: 'rent yield 3.4% (estate rents)'."""
    est, basis = row["rent_est"], row["rent_basis"]
    if not (pd.notna(est) and est > 0 and row["price_hkd"] > 0):
        return 0.0, "rent yield unknown"
    if basis == "rvd":
        return 0.0, "rent yield unknown (no rent listings near this flat)"
    y = est * 12 / row["price_hkd"]
    if basis in ("building", "estate"):
        return _clip(y * 100 / YIELD_FULL_PCT * 100), f"rent yield {y * 100:.1f}% ({basis} rents)"
    med = data.district_growth["rental_yield"].get(district) if isinstance(district, str) \
        and "rental_yield" in data.district_growth else None
    if med is not None and pd.notna(med):
        med = float(med)
        pulled = (1 - DISTRICT_YIELD_PULL) * y + DISTRICT_YIELD_PULL * med
        if pulled > med:  # a guess from district rents never earns more than a typical flat
            return (_clip(med * 100 / YIELD_FULL_PCT * 100),
                    f"rent yield {y * 100:.1f}% (district rents; scored as {med * 100:.1f}%, "
                    f"capped at the district median {med * 100:.1f}%)")
        return (_clip(pulled * 100 / YIELD_FULL_PCT * 100),
                f"rent yield {y * 100:.1f}% (district rents; scored as {pulled * 100:.1f}%, "
                f"halfway to the district median {med * 100:.1f}%)")
    return (_clip(y * 100 / YIELD_FULL_PCT * 100) * DISTRICT_YIELD_PULL,
            f"rent yield {y * 100:.1f}% (district rents; counts half)")


def _score_investment(c: pd.DataFrame, profile: Profile, data: AdvisorData
                      ) -> tuple[pd.Series, pd.Series]:
    scores, whys = [], []
    for _, row in c.iterrows():
        if profile.intent == "rent":
            est = row["rent_est"]
            if not pd.notna(est) or not est > 0:
                scores.append(np.nan)
                whys.append("value unknown (no rent estimate)")
                continue
            fit = 1 - row["price_hkd"] / est
            scores.append(_clip(50 + DISCOUNT_TO_POINTS * fit))
            whys.append(f"asking HK${row['price_hkd']:,.0f} vs estimate HK${est:,.0f} "
                        f"({abs(fit) * 100:.0f}% {'below' if fit >= 0 else 'above'}, "
                        f"{row['rent_basis']} level)")
            continue
        subs, parts = [], []
        d = row["district"]
        g = data.district_growth["growth"].get(d) if isinstance(d, str) \
            and "growth" in data.district_growth else None
        if g is not None and pd.notna(g):
            subs.append(_clip(g * 100 / GROWTH_FULL_PCT * 100))
            parts.append(f"district grew {g * 100:.1f}%/yr")
        else:
            subs.append(0.0)
            parts.append("district growth unknown")
        pp, named = _attribute_pp(row, data.effects)
        subs.append(_clip(50 + ATTR_PP_TO_POINTS * pp))
        parts.append(f"attributes {pp:+.1f} pp/yr" + (f" ({', '.join(named)})" if named
                                                      else ""))
        y_score, y_text = _yield_part(row, d, data)
        subs.append(y_score)
        parts.append(y_text)
        disc = row["discount_pct"]
        if pd.notna(disc) and row.get("confidence") == "normal":
            subs.append(_clip(50 + DISCOUNT_TO_POINTS * disc))
            parts.append(f"{abs(disc) * 100:.0f}% {'below' if disc >= 0 else 'above'} fair "
                         "price")
        else:
            subs.append(0.0)
            parts.append("discount unknown (low confidence)" if pd.notna(disc)
                         else "discount unknown")
        scores.append(float(np.mean(subs)))
        whys.append("; ".join(parts))
    return pd.Series(scores, index=c.index, dtype=float), pd.Series(whys, index=c.index)


def _score_near_place(c: pd.DataFrame, profile: Profile) -> tuple[pd.Series, pd.Series]:
    if not profile.stations:
        return (pd.Series(np.nan, index=c.index),
                pd.Series("no place chosen", index=c.index))
    span = COMMUTE_WORST_MIN - COMMUTE_BEST_MIN
    score = ((COMMUTE_WORST_MIN - c["commute_min"]) / span * 100).clip(0, 100)
    return score, c["commute_text"]


def _score_school(c: pd.DataFrame, profile: Profile) -> tuple[pd.Series, pd.Series]:
    if profile.school_net is None:
        return (pd.Series(np.nan, index=c.index), pd.Series("no net chosen", index=c.index))
    net = c["school_net"].astype(object)
    want = str(profile.school_net)
    score = pd.Series(np.where(net == want, 100.0, 0.0), index=c.index).where(net.notna())
    why = [f"net {n}" if n == want else f"net {n}, not {want}" if isinstance(n, str)
           else "school net unknown" for n in net]
    return score, pd.Series(why, index=c.index)


def _score_space(c: pd.DataFrame, profile: Profile) -> tuple[pd.Series, pd.Series]:
    ref = profile.min_sqft or DEFAULT_MIN_SQFT
    size = (c["area_sqft"] / (SIZE_FULL_MULTIPLE * ref) * 100).clip(0, 100)
    age = c["age_years"].astype(float)
    age_score = (100 - AGE_POINTS_PER_YEAR * age).clip(0, 100)
    score = SPACE_SIZE_SHARE * size + (1 - SPACE_SIZE_SHARE) * age_score.fillna(0)
    score = score.where(c["area_sqft"].notna() & (c["area_sqft"] > 0))
    why = []
    for a, r, y in zip(c["area_sqft"], c["rooms"], age, strict=True):
        if not (pd.notna(a) and a > 0):
            why.append("size unknown")
            continue
        s = f"{a:,.0f} sq ft ({a / ref:.1f}x your minimum)"
        if pd.notna(r):
            s += f", {int(r)} rooms"
        s += f", {y:.0f} years old" if pd.notna(y) else ", age unknown"
        why.append(s)
    return score, pd.Series(why, index=c.index)


def score(c: pd.DataFrame, profile: Profile, data: AdvisorData) -> pd.DataFrame:
    """Adds score_<goal>, why_<goal> (every goal), `overall` and `reason` (the goals the
    profile weights). An unknown goal score (NaN) counts as 0 in `overall`."""
    c = c.copy()
    c["score_investment"], c["why_investment"] = _score_investment(c, profile, data)
    c["score_near_place"], c["why_near_place"] = _score_near_place(c, profile)
    c["score_school"], c["why_school"] = _score_school(c, profile)
    c["score_space"], c["why_space"] = _score_space(c, profile)
    total = sum(profile.weights.values())
    c["overall"] = sum(c[f"score_{g}"].fillna(0) * w for g, w in profile.weights.items()
                       if w > 0) / total
    used = [g for g in GOALS if profile.weights[g] > 0]
    c["reason"] = ["; ".join(str(row[f"why_{g}"]) for g in used) for _, row in c.iterrows()]
    return c


# ---- buy vs rent, sell vs let (Task 19) ------------------------------------------------------


@dataclass(frozen=True)
class GrowthRange:
    """Price growth a year, three cases. `basis` says where the numbers come from."""
    low: float
    mid: float
    high: float
    basis: str = "given"

    def as_dict(self) -> dict[str, float]:
        return {"low": self.low, "mid": self.mid, "high": self.high}


def _as_range(growth) -> GrowthRange:
    if isinstance(growth, GrowthRange):
        return growth
    if isinstance(growth, (tuple, list)) and len(growth) == 3:
        return GrowthRange(float(growth[0]), float(growth[1]), float(growth[2]))
    g = float(growth)
    return GrowthRange(g, g, g)


def window_growth(index: pd.Series, years: int) -> pd.Series:
    """Annualised growth over every `years`-long window of a monthly index (level by month),
    one value per start month that has an end month `years` later."""
    s = index.dropna().sort_index()
    if s.empty or years <= 0:
        return pd.Series(dtype=float)
    end = s.index + pd.DateOffset(years=years)
    later = s.reindex(end).to_numpy(dtype=float)
    out = (later / s.to_numpy(dtype=float)) ** (1 / years) - 1
    return pd.Series(out, index=s.index).dropna()


def growth_range(district, years: int, data: AdvisorData) -> GrowthRange:
    """The district's long-run growth from the growth study (median of flats sold twice,
    `districts.csv`), with low and high cases: the median shifted by the 25th and 75th
    percentile spread of `years`-long outcomes in the district's repeat-sales index
    (`index.csv`; the territory index when the district has none). Without the study, a
    stated guess."""
    g = data.district_growth["growth"].get(district) if isinstance(district, str) \
        and "growth" in data.district_growth else None
    mid = float(g) if g is not None and pd.notna(g) else None
    basis = f"{district} median, growth study" if mid is not None else None
    idx = data.district_index
    spread = None
    if not idx.empty and "district" in idx:
        for name in ((district, "all") if isinstance(district, str) else ("all",)):
            s = idx[idx["district"] == name].set_index("month")["level"]
            w = window_growth(s, years)
            if len(w) >= 12:
                q25, q50, q75 = (float(w.quantile(p)) for p in (0.25, 0.5, 0.75))
                if mid is None:
                    mid, basis = q50, f"{years}-year windows of the {name} index"
                spread = (q25 - q50, q75 - q50)
                basis += f"; range from {years}-year windows of the {name} index"
                break
    if mid is None:
        mid, basis = GROWTH_MID_GUESS, "a guess (no growth study for this district)"
    if spread is None:
        spread = (-GROWTH_SPREAD_GUESS, GROWTH_SPREAD_GUESS)
        basis += "; range a guess of +/-3 pp"
    return GrowthRange(mid + spread[0], mid, mid + spread[1], basis)


def mortgage_interest(loan: float, annual_rate: float, mortgage_years: float,
                      years: float) -> dict:
    """Interest paid in the first `years` of an annuity mortgage, closed form: what was paid
    less the principal repaid (loan less the balance left). Returns payment, interest,
    balance."""
    n_all = round(mortgage_years * 12)
    n = min(round(years * 12), n_all)
    pmt = mortgage.monthly_payment(loan, annual_rate, mortgage_years)
    r = annual_rate / 12
    if r == 0:
        balance = loan - pmt * n
    else:
        f = (1 + r) ** n
        balance = loan * f - pmt * (f - 1) / r
    balance = max(balance, 0.0)
    return {"payment": pmt, "interest": pmt * n - (loan - balance), "balance": balance,
            "months": n}


def _rent_stream(rent: float, years: int, rent_growth: float) -> list[float]:
    """Rent a year for each year of the horizon, growing once a year."""
    return [rent * 12 * (1 + rent_growth) ** t for t in range(int(years))]


def buy_vs_rent(price: float, rent: float, years: int, rate: float, growth,
                *, buyer: str = "first", mortgage_years: int = 30, ltv: float | None = None,
                on: date | None = None, deposit_return: float = DEPOSIT_RETURN,
                rent_growth: float | None = None) -> dict:
    """Total cost of buying this flat against renting the same flat for `years`, one case
    per growth rate in `growth` (a number, or a GrowthRange / (low, mid, high)).

    Buying counts: stamp duty (costs.py, the buyer type's scale on `on`), agent 1% and legal
    0.3% on the purchase, mortgage interest at `rate` on the largest loan HKMA rules allow
    (mortgage.py; `ltv` caps it lower), rates and government rent, management fees, agent 1%
    on the sale, less the gain from selling at the end at the assumed growth. Renting counts
    the rent (growing at `rent_growth`, the price growth by default) less what the cash a
    buyer would need on day one (deposit, duty, fees) earns at `deposit_return`.
    Positive `difference` = buying comes out cheaper over the horizon."""
    on = on or date.today()
    rng = _as_range(growth)
    duty = costs.stamp_duty(price, on, buyer)
    agent_buy, legal = costs.AGENT_PCT * price, costs.LEGAL_PCT * price
    loan = mortgage.max_loan(price, buyer=buyer, on=on)
    if ltv is not None:
        loan = min(loan, price * ltv)
    deposit = price - loan
    upfront = deposit + duty + agent_buy + legal
    m = mortgage_interest(loan, rate, mortgage_years, years)
    cases = {}
    for name in GROWTH_CASES:
        g = getattr(rng, name)
        rg = g if rent_growth is None else rent_growth
        rents = _rent_stream(rent, years, rg)
        rent_total = sum(rents)
        rates_gov = (RATES_PCT + GOV_RENT_PCT) * rent_total
        mgmt = MGMT_FEE_SHARE * rent_total
        sale = price * (1 + g) ** years
        agent_sell = costs.AGENT_PCT * sale
        gain = sale - price
        buy_cost = duty + agent_buy + legal + m["interest"] + rates_gov + mgmt + agent_sell - gain
        earned = upfront * ((1 + deposit_return) ** years - 1)
        rent_cost = rent_total - earned
        cases[name] = {
            "growth": g, "rent_growth": rg, "buy_cost": buy_cost, "rent_cost": rent_cost,
            "difference": rent_cost - buy_cost, "sale_price": sale, "gain": gain,
            "agent_sell": agent_sell, "rates_gov_rent": rates_gov, "management": mgmt,
            "rent_paid": rent_total, "deposit_earns": earned,
        }
    return {
        "price": price, "rent": rent, "years": years, "rate": rate, "buyer": buyer,
        "growth": rng.as_dict(), "growth_basis": rng.basis,
        "stamp_duty": duty, "agent_buy": agent_buy, "legal": legal, "loan": loan,
        "deposit": deposit, "upfront_cash": upfront, "monthly_payment": m["payment"],
        "interest": m["interest"], "loan_balance_at_end": m["balance"],
        "cases": cases, "verdict": verdict_text(cases, years),
        "assumptions": assumptions(deposit_return),
    }


def sell_vs_let(value: float, rent: float, years: int, growth, *,
                deposit_return: float = DEPOSIT_RETURN, rent_growth: float | None = None
                ) -> dict:
    """Cash from selling now against letting for `years` and selling then, one case per
    growth rate. Selling now: value less agent 1%, grown at `deposit_return` to the end of
    the horizon so the two sums are at the same date. Letting: rent (growing at
    `rent_growth`, the price growth by default) less a month empty and half a month's agent
    fee at each new tenancy, rates and government rent, management fees and property tax,
    plus the sale at the end less agent 1%. No mortgage on the flat is assumed. Positive
    `difference` = letting comes out ahead."""
    rng = _as_range(growth)
    sell_now = value * (1 - costs.AGENT_PCT)
    sell_now_grown = sell_now * (1 + deposit_return) ** years
    tenancies = int(np.ceil(years / TENANCY_YEARS))
    cases = {}
    for name in GROWTH_CASES:
        g = getattr(rng, name)
        rg = g if rent_growth is None else rent_growth
        rents = _rent_stream(rent, years, rg)
        gross = sum(rents)
        # a new tenancy every TENANCY_YEARS: the agent's fee each time, a month empty at
        # each change (not before the first tenancy); both at that year's rent
        letting_fee = sum(LETTING_FEE_MONTHS * rents[t * TENANCY_YEARS] / 12
                          for t in range(tenancies))
        vacancy = sum(VACANCY_MONTHS * rents[t * TENANCY_YEARS] / 12
                      for t in range(1, tenancies))
        received = gross - vacancy
        rates = RATES_PCT * gross
        gov_rent = GOV_RENT_PCT * gross
        mgmt = MGMT_FEE_SHARE * gross
        tax = PROPERTY_TAX * (1 - PROPERTY_TAX_ALLOWANCE) * max(received - rates, 0.0)
        net_rent = received - letting_fee - rates - gov_rent - mgmt - tax
        sale = value * (1 + g) ** years
        sale_net = sale * (1 - costs.AGENT_PCT)
        cases[name] = {
            "growth": g, "rent_growth": rg, "rent_received": received, "vacancy": vacancy,
            "letting_fee": letting_fee, "rates_gov_rent": rates + gov_rent,
            "management": mgmt, "property_tax": tax, "net_rent": net_rent,
            "sale_price": sale, "sale_net": sale_net, "let_cash": net_rent + sale_net,
            "sell_now_grown": sell_now_grown,
            "difference": net_rent + sale_net - sell_now_grown,
        }
    return {
        "value": value, "rent": rent, "years": years, "growth": rng.as_dict(),
        "growth_basis": rng.basis, "sell_now": sell_now, "tenancies": tenancies,
        "cases": cases, "verdict": verdict_text(cases, years, let=True),
        "assumptions": assumptions(deposit_return),
    }


def assumptions(deposit_return: float = DEPOSIT_RETURN) -> dict[str, str]:
    """The stated assumptions, for the page (values and where they come from)."""
    return {
        "stamp duty": "IRD scale for the buyer type on the purchase date (costs.py)",
        "agent fee": f"{costs.AGENT_PCT:.0%} of the price on each sale (costs.py)",
        "legal fee": f"{costs.LEGAL_PCT:.1%} of the price, once, on the purchase (costs.py)",
        "mortgage": "largest loan HKMA rules allow (mortgage.py), equal monthly instalments",
        "rates": f"{RATES_PCT:.0%} of the annual rent (rateable value; RVD)",
        "government rent": f"{GOV_RENT_PCT:.0%} of the annual rent (Lands Department, Cap. 515)",
        "management fee": f"{MGMT_FEE_SHARE:.0%} of the rent (a guess)",
        "deposit's return": f"{deposit_return:.1%} a year (a guess)",
        "property tax": f"{PROPERTY_TAX:.0%} of rent received less rates, after a "
                        f"{PROPERTY_TAX_ALLOWANCE:.0%} allowance (IRD)",
        "tenancies": f"{TENANCY_YEARS} years each; {LETTING_FEE_MONTHS:g} month's rent to the "
                     f"agent per tenancy, {VACANCY_MONTHS:g} month empty per change (a guess)",
        "rent growth": "same as price growth unless given",
    }


def verdict_text(cases: dict, years: int, let: bool = False) -> str:
    """One line a reader can act on, e.g. 'buying comes out HK$1.2m ahead over 10 years
    (HK$0.3m behind to HK$2.0m ahead across the growth cases)'."""
    def hk(x: float) -> str:
        return f"HK${abs(x) / 1e6:,.1f}m"

    def side(x: float) -> str:
        return "ahead" if x >= 0 else "behind"

    mid, lo, hi = (cases[k]["difference"] for k in ("mid", "low", "high"))
    what = "letting" if let else "buying"
    return (f"{what} comes out {hk(mid)} {side(mid)} over {years} years "
            f"({hk(lo)} {side(lo)} to {hk(hi)} {side(hi)} across the growth cases)")


def fair_value(building_id: str, area_sqft: float, data: AdvisorData,
               floor_text: str | None = None, rooms: float | None = None) -> tuple[float, str]:
    """The fair-price model's value of a flat (HK$, at today's index level), the way rank.py
    values a listing, and the basis text; (nan, reason) when it cannot be valued."""
    m = data.fair_model
    if m is None:
        return np.nan, "no fair-price model loaded"
    if building_id not in data.buildings.index:
        return np.nan, "building not in the snapshot"
    if not area_sqft > 0:
        return np.nan, "no area"
    b = data.buildings.loc[building_id]
    top = b.get("top_floor")
    floor_num, rel, basis = features.listing_floor(floor_text, None, top)
    built = b.get("built_year")
    row = {
        "area_sqft": float(area_sqft), "rooms": rooms if rooms is not None else np.nan,
        "floor_num": floor_num, "rel_floor": rel, "top_floor": top,
        "age_years": data.today.year - built if pd.notna(built) else np.nan,
        "mtr_m": b.get("mtr_m"), "lat": b.get("lat"), "lon": b.get("lon"),
        "estate_units": b.get("estate_units"),
        "month_idx": m.meta.get("valuation_month_idx", features.month_index(data.today)),
        "sea_view": False, "special_unit": False, "facing": "unknown",
        "district": b.get("district"), "estate_key": b.get("estate_key"),
        "building_id": building_id,
    }
    df = pd.DataFrame([row])
    adj = float(model.predict_adj(m, df)[0])
    logd = 0.0
    if not data.price_index.empty and "index_reference_month" in m.meta:
        cls = pd.Series([features.rvd_class(area_sqft)])
        now = features.deflator(data.price_index, cls, pd.Series([pd.Timestamp(data.today)]))
        ref = features.deflator(data.price_index, cls,
                                pd.Series([pd.Timestamp(m.meta["index_reference_month"])]))
        logd = float(np.log(now[0]) - np.log(ref[0]))
    psf = float(np.exp(adj + logd))
    return psf * float(area_sqft), f"fair-price model, floor {basis}"


def advise_owner(building_id: str, area_sqft: float, data: AdvisorData,
                 profile: Profile | None = None, floor_text: str | None = None,
                 rooms: float | None = None, value_hkd: float | None = None,
                 rent_hkd: float | None = None) -> dict:
    """For an owner asking sell or let: the flat's value (the fair-price model, or
    `value_hkd`), its rent estimate (rent.py, or `rent_hkd`), the district's growth range
    and the sell-vs-let figures over the profile's horizon."""
    profile = profile or Profile(intent="sell")
    notes = []
    value_basis, rent_basis = "given", "given"
    if value_hkd is None:
        value_hkd, value_basis = fair_value(building_id, area_sqft, data, floor_text, rooms)
    if rent_hkd is None:
        if data.rent_model is None:
            rent_hkd, rent_basis = np.nan, "none"
        else:
            district = data.buildings["district"].get(building_id)
            rent_hkd, rent_basis = data.rent_model.estimate(
                building_id, float(area_sqft),
                district if isinstance(district, str) else None, on=data.today)
    district = data.buildings["district"].get(building_id) if building_id in \
        data.buildings.index else None
    rng = growth_range(district if isinstance(district, str) else None, profile.years, data)
    out = {"building_id": building_id, "area_sqft": area_sqft, "district": district,
           "value_hkd": value_hkd, "value_basis": value_basis, "rent_hkd": rent_hkd,
           "rent_basis": rent_basis, "growth": rng.as_dict(), "growth_basis": rng.basis,
           "years": profile.years, "sell_vs_let": None, "notes": notes}
    if not (pd.notna(value_hkd) and value_hkd > 0):
        notes.append(f"no value: {value_basis}")
        return out
    if not (pd.notna(rent_hkd) and rent_hkd > 0):
        notes.append("no rent estimate: letting cannot be judged")
        return out
    out["sell_vs_let"] = sell_vs_let(float(value_hkd), float(rent_hkd), profile.years, rng)
    return out


def _vs_renting(c: pd.DataFrame, profile: Profile, data: AdvisorData) -> pd.DataFrame:
    """Per buy listing: the buy-vs-rent difference (mid, low, high; HK$, positive = buying
    cheaper) and a one-line text, at the district's growth range. NaN and 'unknown' without
    a rent estimate."""
    c = c.copy()
    for col in ("vs_rent_hkd", "vs_rent_low_hkd", "vs_rent_high_hkd"):
        c[col] = np.nan
    c["vs_rent"] = "vs renting: unknown (no rent estimate)"
    ranges: dict = {}
    for i, row in c.iterrows():
        est = row["rent_est"]
        if not (pd.notna(est) and est > 0 and row["price_hkd"] > 0):
            continue
        d = row["district"] if isinstance(row["district"], str) else None
        if d not in ranges:
            ranges[d] = growth_range(d, profile.years, data)
        r = buy_vs_rent(float(row["price_hkd"]), float(est), profile.years, profile.rate,
                        ranges[d], buyer=profile.buyer, mortgage_years=profile.mortgage_years,
                        on=data.today)
        cs = r["cases"]
        c.at[i, "vs_rent_hkd"] = cs["mid"]["difference"]
        c.at[i, "vs_rent_low_hkd"] = cs["low"]["difference"]
        c.at[i, "vs_rent_high_hkd"] = cs["high"]["difference"]
        c.at[i, "vs_rent"] = (f"vs renting a similar flat (HK${est:,.0f}/month): "
                              f"{r['verdict']}; growth {ranges[d].mid * 100:.1f}%/yr "
                              f"({ranges[d].low * 100:.1f} to {ranges[d].high * 100:.1f})")
    return c


# ---- the answer ------------------------------------------------------------------------------

LISTING_COLUMNS = ["estate_rank", "estate_name", "building_name", "district", "floor", "flat",
                   "area_sqft", "rooms", "age_years", "price_hkd", "fair_hkd", "discount_pct",
                   "confidence", "rent_est", "rent_basis", "commute_min", "school_net",
                   "overall", "score_investment", "score_near_place", "score_school",
                   "score_space", "reason", "why_investment", "why_near_place", "why_school",
                   "why_space", "vs_rent", "vs_rent_hkd", "vs_rent_low_hkd", "vs_rent_high_hkd",
                   "also_listed", "url", "key", "source", "listing_id", "building_id",
                   "estate_key"]


def _empty_result(profile: Profile, notes: list[str], left_out: dict | None = None) -> Result:
    return Result(pd.DataFrame(), pd.DataFrame(columns=LISTING_COLUMNS), left_out or {},
                  notes, dict(profile.weights))


def recommend(profile: Profile, data: AdvisorData) -> Result:
    """Estates ranked by the mean overall score of their listings, then the listings in
    estate order (best first within an estate). `sell` and `let` have no listing search:
    their figures are Task 19's."""
    if profile.intent in ("sell", "let"):
        return _empty_result(profile, [f"'{profile.intent}' has no listing search; use the "
                                       "sell-vs-let figures (advise_owner)"])
    c, notes = _candidates(profile, data)
    if c.empty:
        return _empty_result(profile, notes)
    if profile.stations and not data.stations:
        notes.append("no MTR station data: commute unknown for every listing")
    if profile.school_net is not None and c["school_net"].isna().all():
        notes.append("no school net data: net unknown for every listing")
    kept, left_out = _apply_limits(c, profile)
    if kept.empty:
        return _empty_result(profile, notes + ["no listing passes the hard limits"], left_out)
    s = score(kept, profile, data)
    for col in ("vs_rent", "vs_rent_hkd", "vs_rent_low_hkd", "vs_rent_high_hkd", "also_listed"):
        if col not in s:
            s[col] = np.nan
    if profile.intent == "buy":
        s = _vs_renting(s, profile, data)
    cols = [f"score_{g}" for g in GOALS]
    estates = s.groupby("estate_key").agg(
        estate_name=("estate_name", "first"), district=("district", "first"),
        listings=("key", "size"), overall=("overall", "mean"),
        lowest_price_hkd=("price_hkd", "min"),
        **{col: (col, "mean") for col in cols})
    estates = estates.sort_values("overall", ascending=False).reset_index()
    estates.insert(0, "estate_rank", np.arange(1, len(estates) + 1))
    best = s.sort_values("overall", ascending=False).drop_duplicates("estate_key")
    estates["reason"] = estates["estate_key"].map(best.set_index("estate_key")["reason"])
    s["estate_rank"] = s["estate_key"].map(estates.set_index("estate_key")["estate_rank"])
    s = s.sort_values(["estate_rank", "overall"], ascending=[True, False])
    return Result(estates, s[LISTING_COLUMNS].reset_index(drop=True), left_out, notes,
                  dict(profile.weights))
