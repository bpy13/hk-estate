"""Feature table for the fair-price model (Task 4.1): one row per deal and one per listing.

Read once from the database in a short session (`extract`, other jobs write to the same
file), then built in pandas (`deal_features`, `listing_features`). `snapshot` saves the raw
extract under `data/model/snapshot/` so training, ranking and the dashboard don't hold the
database.

Features (plan Task 4.1): saleable area, rooms, floor number and relative floor (floor ÷
the building's top floor), facing in 8 bins + unknown, sea view, special unit (roof,
duplex, top floor), building age, district, distance to the nearest MTR station, estate
size, first-hand flag and a month index. Missing values stay missing (NaN): LightGBM
learns a direction for them, and coverage is reported in model_eval.md.

Facing and sea view are properties of a flat (the same letter on every floor faces the same
way), so a listing that states them lends them to every deal on that building + flat.

Rows left out of training (column `exclude_reason`, docs/decisions.md "Phase 4"):
- the `txn_all` exclusions (duplicate, under HK$500k, first-hand, non-market, bulk, outlier)
- first-hand sales by rule where SRPE doesn't cover the building (`first_hand_rule`)
- deals that include a parking space (28Hse 連車位, SRPE parking column)
- houses, villas and subsidised estates (master list coverage filter, SRPE house registers)
- Ricacorp previous deals derived from days held and % gain (not a recorded price)
- the quarantined LOHAS Park rows (db.QUARANTINE_SQL), dropped before the flags are computed
- deals at under half or over double their building's median price per sq ft (`price_check`)
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from hkestate import db, geo
from hkestate.config import get_data_dir
from hkestate.normalise import parse_floor
from hkestate.sources import cd_master

FACING_BINS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "unknown"]

# 28Hse gives pilot estates no district; these are the estates' own districts.
PILOT_DISTRICTS = {
    "taikoo-shing": "東區",
    "mei-foo": "深水埗區",
    "city-one": "沙田區",
    "lohas-park": "西貢區",
    "goldwin-heights": "中西區",
}

# 28Hse dates Land Registry deals by registration date; the agreement is typically about two
# weeks earlier. Measured from Centadata (which gives both) on every run; this is the fallback.
AGREEMENT_LAG_DEFAULT = 14

# First-hand rule (used where SRPE does not cover the building): a flat's first recorded deal
# within 24 months of the building's completion is a developer sale. Without a completion
# date, the building's first deal is the anchor, but only for a launch: at least 25% of the
# building's flats first sold within those 24 months, and not before 1998 (the data starts in
# 1995, so an early first deal says nothing about the building's age).
FIRST_HAND_WINDOW_DAYS = 730
FIRST_DEAL_ANCHOR_MIN = pd.Timestamp("1998-01-01")
LAUNCH_MIN_SHARE = 0.25

MIN_DEAL_DATE = pd.Timestamp("1993-01-01")  # the RVD price index starts here
MAX_REL_FLOOR = 1.2
BAND_POSITION = {"low": 1 / 6, "mid": 0.5, "high": 5 / 6}
_BAND_WORDS = {"低層": "low", "中層": "mid", "高層": "high"}
SQFT_PER_SQM = 10.7639
# RVD size classes by saleable area in m2: A < 40, B 40-69.9, C 70-99.9, D 100-159.9, E 160+
_RVD_LIMITS = [(40, "A"), (70, "B"), (100, "C"), (160, "D")]
_SPECIAL = re.compile(r"天台|複式|頂層|特色")
_HOUSE_ESTATE_REASONS = {"house", "villa", "village_lot"}


def model_dir() -> Path:
    return get_data_dir() / "model"


# ---- single definitions ---------------------------------------------------------------------

_ZH_DIR = {"東": "E", "西": "W", "南": "S", "北": "N"}


def facing_bin(text) -> str:
    """'東南' / 'South East' -> 'SE'. Anything else (two opposite directions, words like
    開揚 'open view', nothing) -> 'unknown'."""
    if not text or (isinstance(text, float) and math.isnan(text)):
        return "unknown"
    t = str(text).strip()
    letters = [_ZH_DIR[c] for c in t if c in _ZH_DIR]
    if not letters:
        for w in re.findall(r"[a-z]+", t.lower()):
            if w in ("north", "south", "east", "west"):
                letters.append(w[0].upper())
            elif re.fullmatch(r"[nsew]{1,2}", w):
                letters += [c.upper() for c in w]
    letters = letters[:2]
    ns = [x for x in letters if x in "NS"]
    ew = [x for x in letters if x in "EW"]
    if not letters or len(ns) > 1 or len(ew) > 1:
        return "unknown"
    return "".join(ns + ew)


def district_name(text) -> str | None:
    """The 18-district name: '將軍澳 (西貢區)' -> '西貢區'."""
    if not text or (isinstance(text, float) and math.isnan(text)):
        return None
    found = re.findall(r"([^\s()（）|]+區)", str(text))
    return found[-1] if found else (str(text).strip() or None)


def month_index(d) -> int:
    """Months since January 2000 (= 0)."""
    return (d.year - 2000) * 12 + d.month - 1


def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x)) or x is pd.NA or x is pd.NaT


def relative_floor(num, top) -> float:
    """Floor ÷ the building's top floor, capped at 1.2 (skipped labels make a label higher
    than the storey count). NaN when either is unknown."""
    if _missing(num) or _missing(top) or not top or top <= 0:
        return float("nan")
    return min(float(num) / float(top), MAX_REL_FLOOR)


def listing_floor(floor_text, band, top) -> tuple[float, float, str]:
    """(floor number, relative floor, basis) for a listing. Agents often give only a band
    (高層/中層/低層) or nothing: a band is valued at its middle (1/6, 1/2, 5/6 of the
    building), no floor at all at the middle of the building (basis 'imputed')."""
    fl = parse_floor(floor_text) if isinstance(floor_text, str) and floor_text else None
    if fl and fl.num is not None and fl.kind in ("number", "ground", "podium"):
        return float(fl.num), relative_floor(fl.num, top), "exact"
    b = _BAND_WORDS.get(fl.label) if fl and fl.kind == "band" else None
    b = b or (band if band in BAND_POSITION else None)
    rel, basis = (BAND_POSITION[b], "band") if b else (0.5, "imputed")
    if _missing(top) or not top:
        return float("nan"), float("nan"), basis
    return float(round(rel * top)), rel, basis


def rvd_class(area_sqft) -> str:
    """RVD size class of a flat by saleable area ('ALL' if unknown)."""
    if _missing(area_sqft) or not area_sqft:
        return "ALL"
    sqm = float(area_sqft) / SQFT_PER_SQM
    for limit, cls in _RVD_LIMITS:
        if sqm < limit:
            return cls
    return "E"


def is_special_unit(text) -> bool:
    """Roof, duplex, top-floor or 'special' units (天台, 複式, 頂層, 特色): kept but marked."""
    return bool(text) and not _missing(text) and bool(_SPECIAL.search(str(text)))


def index_wide(index_df: pd.DataFrame) -> pd.DataFrame:
    """RVD price index by month (rows, every month) and size class (columns)."""
    wide = index_df.assign(period=pd.to_datetime(index_df["period"])).pivot_table(
        index="period", columns="class", values="value", aggfunc="last")
    months = pd.date_range(wide.index.min(), wide.index.max(), freq="MS")
    return wide.reindex(months).ffill()


def deflator(index_df: pd.DataFrame, classes, dates,
             known_until: pd.Timestamp | None = None) -> np.ndarray:
    """Index(class, month) / index(class, reference month). The reference is the last month
    known (`known_until`, or the last published month); later months carry it, so a deal
    after the reference month gets 1. NaN before the index starts (1993)."""
    wide = index_wide(index_df)
    last = wide.index.max()
    ref = last if known_until is None else min(
        pd.Timestamp(known_until).to_period("M").to_timestamp(), last)
    classes = pd.Series(classes).reset_index(drop=True)
    classes = classes.where(classes.isin(wide.columns), "ALL")
    months = pd.to_datetime(pd.Series(dates)).reset_index(drop=True)
    months = months.dt.to_period("M").dt.to_timestamp().clip(upper=ref)
    stacked = wide.stack()
    vals = stacked.reindex(pd.MultiIndex.from_arrays([months, classes])).to_numpy(dtype=float)
    refs = wide.loc[ref].reindex(classes).to_numpy(dtype=float)
    return vals / refs


# Price check: a deal at under half or over double its building's median price per sq ft
# (index-adjusted, all years) is not a normal sale of a whole flat: part-share transfers
# (one co-owner buying out another) are registered like sales and 28Hse doesn't mark them.
# Unlike the yearly MAD rule in txn_all, this holds when such deals cluster in one building.
PRICE_CHECK_LOW, PRICE_CHECK_HIGH, PRICE_CHECK_MIN_DEALS = 0.5, 2.0, 5


def price_check(adj_psf: pd.Series, building: pd.Series) -> pd.Series:
    """True for deals outside [0.5, 2] x their building's median adjusted price per sq ft
    (buildings with 5+ priced deals only)."""
    med = adj_psf.groupby(building).transform("median")
    n = adj_psf.groupby(building).transform("count")
    ratio = adj_psf / med
    out = (n >= PRICE_CHECK_MIN_DEALS) & ((ratio < PRICE_CHECK_LOW) | (ratio > PRICE_CHECK_HIGH))
    return out.fillna(False).astype(bool)


def agreement_dates(df: pd.DataFrame, lag_days: int) -> pd.Series:
    """Agreement (instrument) date per deal: the source's own where it gives one (Centadata,
    SRPE); 28Hse Land Registry deals carry the registration date, so they move back by the
    measured lag; agency reports and others keep the date shown."""
    shift = (df["source"] == "hse28") & (df["deal_type"] == "registry")
    days = pd.to_timedelta(np.where(shift, lag_days, 0), unit="D")
    fallback = pd.to_datetime(df["deal_date"]) - days
    return pd.to_datetime(df["instrument_date"]).where(df["instrument_date"].notna(), fallback)


def first_hand_rule(df: pd.DataFrame) -> pd.Series:
    """True for deals the rule treats as developer sales. Columns: unit_id, building_id,
    agreement_date, anchor_date (completion-based, NaT if unknown), srpe_building (SRPE
    covers it: its own flag is used instead), building_flats (NaN: count flats seen)."""
    if df.empty:
        return pd.Series([], dtype=bool, index=df.index)
    d = df
    unit_first = d.groupby("unit_id")["agreement_date"].transform("min")
    is_first = d["agreement_date"].eq(unit_first)

    per_unit = d.groupby("unit_id").agg(b=("building_id", "first"), f=("agreement_date", "min"))
    b_first = per_unit.groupby("b")["f"].min()
    early = per_unit["f"] < per_unit["b"].map(b_first) + pd.Timedelta(days=FIRST_HAND_WINDOW_DAYS)
    n_early = early.groupby(per_unit["b"]).sum()
    n_units = per_unit.groupby("b").size()
    flats = pd.to_numeric(d.groupby("building_id")["building_flats"].first(), errors="coerce")
    denom = flats.where(flats > 0, n_units).reindex(n_units.index).fillna(n_units)
    launch = (n_early / denom >= LAUNCH_MIN_SHARE) & (b_first >= FIRST_DEAL_ANCHOR_MIN)

    anchor = pd.to_datetime(d["anchor_date"])
    from_first = d["building_id"].map(b_first).where(
        d["building_id"].map(launch).fillna(False).astype(bool))
    anchor = anchor.where(anchor.notna(), from_first)
    within = d["agreement_date"] < anchor + pd.Timedelta(days=FIRST_HAND_WINDOW_DAYS)
    srpe = d["srpe_building"].fillna(False).astype(bool)
    return (is_first & anchor.notna() & within & ~srpe).fillna(False).astype(bool)


# ---- reading the database --------------------------------------------------------------------


def _deals_sql() -> str:
    return f"""
    SELECT a.source, a.source_id, a.fetched_at, a.unit_id, a.building_id, a.estate_id,
        a.floor_label, a.flat, a.deal_date, a.instrument_date, a.registration_date,
        a.deal_type, a.price_hkd, a.area_sqft, a.area_basis, a.psf_hkd,
        coalesce(a.rooms, u.rooms) AS rooms, a.floor_num, a.floor_kind,
        a.excl_duplicate, a.excl_low_price, a.excl_first_hand, a.excl_non_market,
        a.excl_bulk, a.excl_outlier,
        CASE WHEN json_valid(a.raw_json) THEN
            coalesce(json_extract_string(a.raw_json, '$.with_parking') = 'true'
                     OR json_extract_string(a.raw_json, '$.has_parking') = 'true', false)
            ELSE false END AS parking,
        CASE WHEN json_valid(a.raw_json)
             THEN json_extract_string(a.raw_json, '$.feature') END AS cd_feature
    FROM ({db.txn_all_sql('_model_latest')}) a
    LEFT JOIN unit u ON u.unit_id = a.unit_id
    """


_BUILDINGS_SQL = """
    SELECT b.building_id, b.estate_id, b.name_zh AS building_name, b.completion_date,
        b.floors, b.flats, e.name_zh AS estate_name, e.group_name,
        e.district AS estate_district, mb.district AS mb_district, mb.lat, mb.lon, mb.mtr_m,
        mb.mtr_station, mb.built_year, mb.built_basis, me.code AS me_code, me.parent_code,
        me.district AS me_district, me.units AS me_units, me.excluded AS me_excluded,
        me.exclude_reason, uu.max_floor, uu.n_units
    FROM building b
    LEFT JOIN estate e ON e.estate_id = b.estate_id
    LEFT JOIN master_building mb ON mb.building_id = b.building_id
    LEFT JOIN master_estate me ON 'cd-' || me.code = b.estate_id
    LEFT JOIN (SELECT building_id, max(floor_num) AS max_floor, count(*) AS n_units
               FROM unit GROUP BY 1) uu ON uu.building_id = b.building_id
"""

_LISTINGS_SQL = """
    WITH lr AS (
        SELECT source, listing_id,
            arg_max(facing, fetched_at) FILTER (WHERE facing IS NOT NULL) AS facing,
            arg_max(floor_text, fetched_at) FILTER (WHERE floor_text IS NOT NULL) AS floor_text,
            arg_max(rooms, fetched_at) FILTER (WHERE rooms IS NOT NULL) AS rooms,
            arg_max(features, fetched_at) FILTER (WHERE features IS NOT NULL) AS features,
            arg_max(age_years, fetched_at) FILTER (WHERE age_years IS NOT NULL) AS age_years,
            max(fetched_at) AS last_read
        FROM listing_raw GROUP BY 1, 2),
    fl AS (
        SELECT source, listing_id,
            array_to_string(list_sort(list_distinct(list(flag))), '|') AS flags
        FROM risk_flag WHERE listing_id IS NOT NULL GROUP BY 1, 2)
    SELECT l.source, l.listing_id, l.kind, l.group_name, l.url, l.building_text,
        l.building_id, l.match_method, l.floor_band, l.flat, l.price_hkd, l.saleable_sqft,
        l.first_seen, l.last_seen, l.status, l.dup_group,
        lr.facing, lr.floor_text, lr.rooms, lr.features, lr.age_years, lr.last_read, fl.flags
    FROM listing l
    LEFT JOIN lr ON lr.source = l.source AND lr.listing_id = l.listing_id
    LEFT JOIN fl ON fl.source = l.source AND fl.listing_id = l.listing_id
"""


def extract(con) -> dict:
    """Everything the model, ranking and dashboard need, in one pass over the database."""
    cd_master.ensure_tables(con)
    geo.ensure_tables(con)
    con.execute("CREATE OR REPLACE TEMP VIEW _model_latest AS "
                f"SELECT * FROM txn_latest WHERE NOT {db.QUARANTINE_SQL}")
    quarantined = con.execute(
        f"SELECT count(*) FROM txn_latest WHERE {db.QUARANTINE_SQL}").fetchone()[0]
    lag = con.execute("""
        SELECT median(date_diff('day', instrument_date, registration_date))
        FROM txn_latest
        WHERE source = 'centadata' AND instrument_date >= DATE '2018-01-01'
          AND date_diff('day', instrument_date, registration_date) BETWEEN 0 AND 120
    """).fetchone()[0]
    raw = {
        "deals": con.execute(_deals_sql()).df(),
        "buildings": con.execute(_BUILDINGS_SQL).df(),
        "estates": con.execute(
            "SELECT code, parent_code, units, excluded FROM master_estate").df(),
        "listings": con.execute(_LISTINGS_SQL).df(),
        "index": con.execute(
            "SELECT class, period, value FROM price_index "
            "WHERE series = 'rvd_price_index' AND region = 'ALL'").df(),
        "srpe_buildings": [r[0] for r in con.execute(
            "SELECT DISTINCT building_id FROM txn_latest "
            "WHERE source = 'srpe' AND building_id IS NOT NULL").fetchall()],
        "run_log": con.execute(
            "SELECT run_id, command, args, started_at, finished_at, status, message "
            "FROM run_log ORDER BY run_id DESC LIMIT 200").df(),
    }
    raw["meta"] = {
        "quarantined_rows": int(quarantined),
        "agreement_lag_days": int(round(lag)) if lag is not None else AGREEMENT_LAG_DEFAULT,
        "agreement_lag_measured": lag is not None,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
    }
    return raw


_FRAMES = ["deals", "buildings", "estates", "listings", "index", "run_log"]


def snapshot(db_path=None, out_dir: Path | None = None) -> dict:
    """Extract in one short database session and save it (parquet + meta.json)."""
    with db.session(db_path) as con:
        raw = extract(con)
    save_snapshot(raw, out_dir)
    return raw


def save_snapshot(raw: dict, out_dir: Path | None = None) -> Path:
    out = out_dir or model_dir() / "snapshot"
    out.mkdir(parents=True, exist_ok=True)
    for name in _FRAMES:
        raw[name].to_parquet(out / f"{name}.parquet", index=False)
    meta = {**raw["meta"], "srpe_buildings": raw["srpe_buildings"]}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    return out


def load_snapshot(out_dir: Path | None = None) -> dict:
    src = out_dir or model_dir() / "snapshot"
    raw = {name: pd.read_parquet(src / f"{name}.parquet") for name in _FRAMES}
    meta = json.loads((src / "meta.json").read_text("utf-8"))
    raw["srpe_buildings"] = meta.pop("srpe_buildings", [])
    raw["meta"] = meta
    return raw


# ---- building and flat attributes ------------------------------------------------------------


def _listing_built_year(listings: pd.DataFrame) -> pd.Series:
    """A building's completion year implied by listings' 'age' (Centanet), by building."""
    li = listings.dropna(subset=["building_id", "age_years", "last_read"])
    if li.empty:
        return pd.Series(dtype=float)
    years = pd.to_datetime(li["last_read"]).dt.year - li["age_years"].astype(float)
    return years.groupby(li["building_id"]).median().round()


def building_table(raw: dict) -> pd.DataFrame:
    """One row per building: district, location, top floor, age anchor, estate size."""
    b = raw["buildings"].copy()
    est = raw["estates"]
    b["district"] = [
        district_name(mb) or district_name(me) or district_name(ed) or PILOT_DISTRICTS.get(g)
        for mb, me, ed, g in zip(b["mb_district"], b["me_district"], b["estate_district"],
                                 b["group_name"], strict=True)]
    floors = pd.to_numeric(b["floors"], errors="coerce")
    b["top_floor"] = floors.where(floors > 0, pd.to_numeric(b["max_floor"], errors="coerce"))
    flats = pd.to_numeric(b["flats"], errors="coerce")
    b["building_flats"] = flats.where(flats > 0, pd.to_numeric(b["n_units"], errors="coerce"))

    completion = pd.to_datetime(b["completion_date"])
    comp_year = completion.dt.year.where(completion.dt.year > 1950)
    mb_year = pd.to_numeric(b["built_year"], errors="coerce").where(
        b["built_basis"] == "completion")
    li_year = b["building_id"].map(_listing_built_year(raw["listings"]))
    b["built_year"] = comp_year.fillna(mb_year).fillna(li_year)
    b["anchor_date"] = completion.where(comp_year.notna(), pd.to_datetime(
        b["built_year"].dropna().astype(int).astype(str) + "-01-01").reindex(b.index))

    # estate: a big estate's phases (Centadata 2- codes under one 3- code) count together;
    # pilot estates by group (LOHAS Park is 22 developments on 28Hse)
    parent = est.set_index("code")["parent_code"]
    est_key = est["parent_code"].fillna(est["code"])
    units_by_key = pd.to_numeric(est["units"], errors="coerce").groupby(est_key).sum()
    me_key = b["me_code"].map(parent).fillna(b["me_code"])
    b["estate_key"] = np.where(b["group_name"].notna(), b["group_name"],
                               np.where(me_key.notna(), "cd-" + me_key.fillna(""),
                                        b["estate_id"]))
    pilot_units = pd.to_numeric(b["n_units"], errors="coerce").groupby(b["estate_key"]).sum()
    b["estate_units"] = me_key.map(units_by_key)
    b["estate_units"] = b["estate_units"].fillna(b["estate_key"].map(pilot_units))
    b.loc[b["estate_units"] <= 0, "estate_units"] = np.nan
    b["is_pilot"] = b["group_name"].notna()
    reason = b["exclude_reason"].fillna("")
    b["excluded_estate"] = np.where(
        b["me_excluded"].fillna(False).astype(bool),
        np.where(reason.isin(_HOUSE_ESTATE_REASONS), "house", "subsidised"), None)
    return b.set_index("building_id")


def flat_attributes(listings: pd.DataFrame) -> pd.DataFrame:
    """Facing and sea view per building + flat, from listings that state them."""
    li = listings.dropna(subset=["building_id", "flat"]).copy()
    if li.empty:
        return pd.DataFrame(columns=["facing", "sea_view"],
                            index=pd.MultiIndex.from_tuples([], names=["building_id", "flat"]))
    li["facing_bin"] = li["facing"].map(facing_bin)
    li["sv"] = li["flags"].fillna("").str.contains("sea_view")
    known = li[li["facing_bin"] != "unknown"]
    facing = known.groupby(["building_id", "flat"])["facing_bin"].agg(
        lambda s: s.value_counts().index[0])
    sea = li.groupby(["building_id", "flat"])["sv"].any()
    return pd.DataFrame({"facing": facing, "sea_view": sea})


def _attach(df: pd.DataFrame, raw: dict, buildings: pd.DataFrame) -> pd.DataFrame:
    cols = ["district", "lat", "lon", "mtr_m", "top_floor", "built_year", "estate_key",
            "estate_units", "is_pilot", "excluded_estate", "building_flats", "anchor_date",
            "building_name", "estate_name"]
    out = df.join(buildings[cols], on="building_id")
    out["is_pilot"] = out["is_pilot"].fillna(False).astype(bool)
    return out


# ---- the two tables ----------------------------------------------------------------------------

EXCLUDE_ORDER = [
    "no_building", "house", "duplicate", "first_hand", "first_hand_rule", "parking",
    "non_market", "low_price", "bulk", "outlier", "derived", "no_area", "too_old",
    "price_check",  # applied last, against the deals left (see price_check)
]


def deal_features(raw: dict) -> pd.DataFrame:
    """One row per deal (newest version of each source row, quarantined rows already gone)
    with features, target (log price per sq ft) and `exclude_reason` (None = trainable)."""
    d = raw["deals"].copy()
    lag = raw["meta"].get("agreement_lag_days", AGREEMENT_LAG_DEFAULT)
    for col in ("deal_date", "instrument_date", "registration_date"):
        d[col] = pd.to_datetime(d[col])
    d["agreement_date"] = agreement_dates(d, lag)
    buildings = building_table(raw)
    d = _attach(d, raw, buildings)

    parsed = d["floor_label"].map(lambda x: parse_floor(x).num if isinstance(x, str) else None)
    d["floor_num"] = pd.to_numeric(d["floor_num"], errors="coerce").fillna(
        pd.to_numeric(parsed, errors="coerce"))
    d["rel_floor"] = [relative_floor(n, t) for n, t in zip(d["floor_num"], d["top_floor"],
                                                           strict=True)]
    attrs = flat_attributes(raw["listings"])
    keys = pd.MultiIndex.from_arrays([d["building_id"], d["flat"]])
    d["facing"] = attrs["facing"].reindex(keys).fillna("unknown").to_numpy()
    d["sea_view"] = attrs["sea_view"].reindex(keys).fillna(False).astype(bool).to_numpy()
    d["special_unit"] = (d["cd_feature"].map(is_special_unit)
                         | d["floor_label"].eq("頂層") | d["floor_kind"].eq("top"))
    d["age_years"] = d["agreement_date"].dt.year - d["built_year"]
    d["month_idx"] = d["agreement_date"].map(
        lambda x: month_index(x) if not pd.isna(x) else np.nan)
    d["log_psf"] = np.log(pd.to_numeric(d["psf_hkd"], errors="coerce"))
    d["rvd_class"] = d["area_sqft"].map(rvd_class)
    d["rooms"] = pd.to_numeric(d["rooms"], errors="coerce")

    srpe = set(raw.get("srpe_buildings") or [])
    d["srpe_building"] = d["building_id"].isin(srpe)
    eligible = ~d["excl_duplicate"].astype(bool) & d["building_id"].notna()
    rule = pd.Series(False, index=d.index)
    rule[eligible] = first_hand_rule(d.loc[eligible])
    d["first_hand_rule"] = rule
    d["first_hand"] = d["excl_first_hand"].astype(bool) | rule

    house = d["excluded_estate"].notna() | ((d["source"] == "srpe") & d["floor_label"].eq("H"))
    conditions = {
        "no_building": d["building_id"].isna(),
        "house": house,
        "duplicate": d["excl_duplicate"].astype(bool),
        "first_hand": d["excl_first_hand"].astype(bool),
        "first_hand_rule": rule,
        "parking": d["parking"].astype(bool),
        "non_market": d["excl_non_market"].astype(bool),
        "low_price": d["excl_low_price"].astype(bool),
        "bulk": d["excl_bulk"].astype(bool),
        "outlier": d["excl_outlier"].astype(bool),
        "derived": (d["source"] == "ricacorp") & d["source_id"].str.endswith(":prev"),
        "no_area": ~d["area_basis"].isin(["saleable", "converted"]) | d["log_psf"].isna(),
        "too_old": d["agreement_date"].isna() | (d["agreement_date"] < MIN_DEAL_DATE),
    }
    reason = pd.Series([None] * len(d), index=d.index, dtype=object)
    for name in reversed(EXCLUDE_ORDER):  # the first reason in EXCLUDE_ORDER wins
        if name in conditions:
            reason = reason.mask(conditions[name].fillna(False).astype(bool), name)
    # the price check judges each deal against the building's otherwise-clean deals
    clean = reason.isna()
    if clean.any() and not raw["index"].empty:
        adj = d.loc[clean, "psf_hkd"].to_numpy(dtype=float) / deflator(
            raw["index"], d.loc[clean, "rvd_class"], d.loc[clean, "agreement_date"])
        flagged = price_check(pd.Series(adj, index=d.index[clean]), d.loc[clean, "building_id"])
        reason = reason.mask(flagged.reindex(d.index, fill_value=False), "price_check")
    d["exclude_reason"] = reason
    d = d.drop(columns=["anchor_date", "building_flats"])
    return d.reset_index(drop=True)


def listing_features(raw: dict, today: date | None = None) -> pd.DataFrame:
    """One row per active sale listing, with the same features as the deals (valued as of
    `today`). `value_note` says why a listing can't be valued (no building match, no area,
    a house); those rows are kept so the counts are visible."""
    today = today or date.today()
    li = raw["listings"]
    li = li[(li["kind"] == "sale") & (li["status"] == "active")].copy()
    buildings = building_table(raw)
    li = _attach(li, raw, buildings)
    floors = [listing_floor(ft, band, top) for ft, band, top in
              zip(li["floor_text"], li["floor_band"], li["top_floor"], strict=True)]
    li["floor_num"] = [f[0] for f in floors]
    li["rel_floor"] = [f[1] for f in floors]
    li["floor_basis"] = [f[2] for f in floors]

    attrs = flat_attributes(raw["listings"])
    keys = pd.MultiIndex.from_arrays([li["building_id"], li["flat"]])
    own = li["facing"].map(facing_bin)
    unit_facing = attrs["facing"].reindex(keys).fillna("unknown").to_numpy()
    li["facing"] = own.where(own != "unknown", unit_facing)
    flags = li["flags"].fillna("")
    unit_sea = attrs["sea_view"].reindex(keys).fillna(False).astype(bool).to_numpy()
    li["sea_view"] = flags.str.contains("sea_view") | unit_sea
    li["special_unit"] = li["features"].map(is_special_unit) | li["floor_text"].map(
        is_special_unit)
    parking = li["features"].fillna("").str.contains("車位")
    li["flags"] = [("|".join(x for x in [f, "with_parking"] if x) if p else f) or None
                   for f, p in zip(flags, parking, strict=True)]
    li["area_sqft"] = pd.to_numeric(li["saleable_sqft"], errors="coerce")
    li["rooms"] = pd.to_numeric(li["rooms"], errors="coerce")
    age_from_building = today.year - li["built_year"]
    li["age_years"] = age_from_building.fillna(pd.to_numeric(li["age_years"], errors="coerce"))
    li["month_idx"] = month_index(today)
    li["rvd_class"] = li["area_sqft"].map(rvd_class)
    li["first_hand"] = False

    note = pd.Series([None] * len(li), index=li.index, dtype=object)
    note = note.mask(li["price_hkd"].fillna(0) < db.MIN_PRICE_HKD, "price under HK$500k")
    note = note.mask(~(li["area_sqft"] > 0), "no saleable area")
    note = note.mask(li["excluded_estate"].notna() | li["floor_text"].eq("洋房"),
                     "house or subsidised estate")
    note = note.mask(li["building_id"].isna(), "no building match")
    li["value_note"] = note
    return li.drop(columns=["anchor_date", "building_flats"]).reset_index(drop=True)
