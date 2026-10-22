"""Decision helper (plan 2026-10-23, Task 18): from a person's profile to ranked estates and
listings, each with a 0-100 score per goal, an overall weighted score and a one-line reason
per goal. Task 19 adds buy-vs-rent and sell-vs-let figures in this module.

Steps, in order:
1. Candidates: for `buy`, the ranked sale listings (rank.py) with no `exclude_reason` (a data
   check failed); for `rent`, the active rent listings with their rent estimate (rent.py).
2. Hard limits (each row is kept or counted out, never scored around): budget, size, rooms,
   commute, school net. A limit on a value the data does not have (no coordinates, so no
   commute; no net) counts the row out unless `Profile.allow_unknown`.
3. Scores per goal (docs/decisions.md "Decision helper scoring"), NaN when the data is
   missing; the row's reason says "unknown" and the overall score counts it as 0, so missing
   data never reads as good.
4. Overall = weighted average of the goal scores by the profile's weights. Estates are ranked
   by the mean overall score of their listings; listings follow in estate order.

Data comes in through `AdvisorData` (`load_data` reads the snapshot, the ranked parquet, the
growth study's CSVs and the MTR files; tests build it from small frames). The live database
is never opened here.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from hkestate import commute, features, growth, mortgage, rent
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
    `mtr_station` table's columns; the dashboard dumps it, tests write a few lines)."""
    rows, names = [], {}
    for r in csv.DictReader(io.StringIO(csv_text.lstrip("﻿"))):
        try:
            rows.append((r["code"], float(r["lat"]), float(r["lon"])))
        except (KeyError, ValueError, TypeError):
            continue
        names[r["code"]] = r.get("name_en") or r["code"]
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
              school_nets: pd.Series | None = None, today: date | None = None) -> AdvisorData:
    """Read the saved files (never the live database). `stations_csv` has code, name_en,
    lat, lon; `mtr_csv` is the MTR open-data lines file (commute graph); `school_nets` maps
    building_id to net (the snapshot does not carry it yet)."""
    data_dir = get_data_dir()
    raw = features.load_snapshot(snapshot_dir)
    buildings = features.building_table(raw)
    if school_nets is not None:
        buildings["school_net"] = buildings.index.map(school_nets)
    ranked_path = ranked_path or features.model_dir() / "ranked_latest.parquet"
    sale = pd.read_parquet(ranked_path) if Path(ranked_path).exists() else pd.DataFrame()
    if "school_net" in buildings and not sale.empty:
        sale["school_net"] = sale["building_id"].map(buildings["school_net"])
    if growth_dir is None:
        root = data_dir / "reports" / "growth"
        dated = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        growth_dir = dated[-1] if dated else None
    if growth_dir is not None and (Path(growth_dir) / setting).is_dir():
        districts, effects = growth_tables(Path(growth_dir), setting)
    else:
        districts, effects = pd.DataFrame(), pd.DataFrame()
    stations, names, graph = [], {}, {}
    if stations_csv is not None and Path(stations_csv).exists():
        stations, names = read_stations(Path(stations_csv).read_text("utf-8"))
    if mtr_csv is not None and Path(mtr_csv).exists():
        graph = commute.build_graph(Path(mtr_csv).read_text("utf-8"),
                                    {c: (la, lo) for c, la, lo in stations})
    rent_model = rent.RentModel.from_snapshot(raw) if not raw["rvd"].empty else None
    return AdvisorData(sale=sale, rent_listings=rent_candidates(raw, buildings),
                       buildings=buildings, district_growth=districts, effects=effects,
                       stations=stations, station_names=names, graph=graph,
                       rent_model=rent_model, today=today or date.today(),
                       growth_setting=setting if not districts.empty else "")


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
        est = row["rent_est"]
        if pd.notna(est) and est > 0 and row["price_hkd"] > 0:
            y = est * 12 / row["price_hkd"]
            subs.append(_clip(y * 100 / YIELD_FULL_PCT * 100))
            parts.append(f"rent yield {y * 100:.1f}%")
        else:
            subs.append(0.0)
            parts.append("rent yield unknown")
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


# ---- the answer ------------------------------------------------------------------------------

LISTING_COLUMNS = ["estate_rank", "estate_name", "building_name", "district", "floor", "flat",
                   "area_sqft", "rooms", "age_years", "price_hkd", "fair_hkd", "discount_pct",
                   "confidence", "rent_est", "rent_basis", "commute_min", "school_net",
                   "overall", "score_investment", "score_near_place", "score_school",
                   "score_space", "reason", "why_investment", "why_near_place", "why_school",
                   "why_space", "url", "key", "source", "listing_id", "building_id",
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
                                       "sell-vs-let figures"])
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
