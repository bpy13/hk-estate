"""Territory-wide master list of estates and buildings, from Centadata block pages.

The Centanet estate sitemap lists every estate or phase (`2-` codes) and big estate (`3-`).
Each `2-` page opens its first block and shows the menu of blocks, so one pass over the
`2-` codes plus the blocks they list covers the territory. For each estate we keep the
facts the page gives (completion date, flats, floors, district) and, for estates that pass
the coverage filter, every flat's latest recorded deal (`txn_raw`, source `centadata`).

Coverage filter (docs/decisions.md "Coverage filter"): HOS, TPS and other subsidised flats,
villas and village houses are left out. Centadata marks subsidised flats on their listings
(`hos_flag`, `prh_flag`), and houses with `isHouse`; villas and village-house lots are told
apart by name. An estate with no current listing carries no flag, so a subsidised estate
nobody is selling slips through: `data/manual/master_overrides.csv` (code,action) lets the
owner force `include` or `exclude`.

Estate ids are `cd-<2- code>` and building ids `cd-<1- code>`. The five pilot estates
already have 28Hse ids and were cross-checked block by block (`centadata.py`), so the
territory pass records them (`master_estate.pilot`) but loads nothing for them.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import unquote

import httpx

from hkestate import db
from hkestate.config import get_data_dir
from hkestate.fetch import FetchError, SourceBlocked
from hkestate.normalise import infer_skipped, norm_name, parse_floor, saleable_to_gross
from hkestate.sources import centadata as cd

COMMAND = "master"
SITEMAP_URL = "https://hk.centanet.com/sitemap-index/estate/sitemap_estate_zh_hk.xml"
MIN_INTERVAL_S = 2.0  # hk.centanet.com is shared with the Centanet listing scraper
MAX_CONSECUTIVE_ERRORS = 8

MASTER_DDL = [
    """
    CREATE TABLE IF NOT EXISTS master_estate (
        code            VARCHAR PRIMARY KEY,  -- Centadata 2- code (estate or phase)
        parent_code     VARCHAR,              -- the 3- code of the big estate, if any
        name_zh         VARCHAR,
        estate_type     VARCHAR,
        address         VARCHAR,
        district        VARCHAR,
        hma             VARCHAR,              -- Centanet's finer sub-district
        terr            VARCHAR,
        completion_date DATE,
        units           INTEGER,
        floors          INTEGER,
        hos_flag        BOOLEAN,
        prh_flag        BOOLEAN,
        excluded        BOOLEAN,
        exclude_reason  VARCHAR,
        pilot           BOOLEAN,              -- loaded by the pilot cross-check instead
        blocks          INTEGER,
        fetched_at      TIMESTAMP
    )""",
    """
    CREATE TABLE IF NOT EXISTS master_building (
        building_id     VARCHAR PRIMARY KEY,  -- cd-<1- code>, or a 28Hse id for pilot estates
        estate_code     VARCHAR,
        block_code      VARCHAR,
        estate_name     VARCHAR,
        phase_name      VARCHAR,
        block_name      VARCHAR,
        address         VARCHAR,
        completion_date DATE,
        units           INTEGER,
        floors          INTEGER,
        district        VARCHAR,
        hma             VARCHAR,
        -- location attributes (geo.py)
        lat             DOUBLE,
        lon             DOUBLE,
        geo_status      VARCHAR,              -- ok, no_match, error
        geo_query       VARCHAR,
        geo_name        VARCHAR,
        geo_district    VARCHAR,
        geo_at          TIMESTAMP,
        mtr_station     VARCHAR,
        mtr_m           DOUBLE,
        built_year      INTEGER,
        built_basis     VARCHAR,              -- completion, first_deal
        age_years       INTEGER
    )""",
]


def ensure_tables(con) -> None:
    for ddl in MASTER_DDL:
        con.execute(ddl)


# ---- sitemap -------------------------------------------------------------------------------


def sitemap_estates(xml: str) -> list[tuple[str, str]]:
    """(code, name) for each estate/phase (`2-`) and big estate (`3-`) in the sitemap.
    The sitemap spells names in Simplified Chinese; the page gives the Traditional name."""
    out = []
    for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
        m = re.search(r"/estate/(.+)/([23]-[A-Z]+)$", loc)
        if m:
            out.append((m.group(2), unquote(m.group(1))))
    return out


# ---- estate facts ----------------------------------------------------------------------------


@dataclass
class EstateInfo:
    code: str
    name: str
    estate_type: str
    is_house: bool
    address: str
    completion: date | None
    units: int | None
    floors: int | None
    district: str | None
    hma: str | None
    terr: str | None
    parent_code: str | None
    hos: bool
    prh: bool


def parse_estate_info(html: str) -> EstateInfo:
    data = cd.read_nuxt(html)["data"][0]
    bi = data.get("BuildingInfo") or {}
    table = data.get("tableList") or {}
    brief = data.get("briefList") or {}
    scope = brief.get("scope") or {}
    listings = []
    for key in ("salesearch", "rentsearch"):
        listings += (data.get(key) or {}).get("data") or []
    district = None
    for item in listings:
        district = (item.get("scope") or {}).get("db")
        if district:
            break
    district = district or ((brief.get("schoolNet") or {}).get("secondarySchoolScope") or {}).get(
        "db"
    )
    parent = str(data.get("bigestTypecode") or "")
    return EstateInfo(
        code=str(data.get("typeCode") or ""),
        name=str(bi.get("estate") or table.get("estate") or brief.get("name") or ""),
        estate_type=str(bi.get("estateType") or ""),
        is_house=bool(bi.get("isHouse")),
        address=str(table.get("address") or brief.get("address") or ""),
        completion=cd._d(brief.get("opDate")),
        units=brief.get("unitCnt") or None,
        floors=brief.get("noOfFloors") or None,
        district=district,
        hma=scope.get("hma") or None,
        terr=scope.get("terr") or None,
        parent_code=parent if parent.startswith("3-") else None,
        hos=any(bool(x.get("hos_flag")) for x in listings),
        prh=any(bool(x.get("prh_flag")) for x in listings),
    )


# ---- the coverage filter -----------------------------------------------------------------------

_VILLA = re.compile(r"別墅|洋房|\bvillas?\b", re.IGNORECASE)
_VILLAGE_LOT = re.compile(r"\bDD\s*\d+|\bLot\s*\d+|丈量約", re.IGNORECASE)
_VILLAGE_HOUSE = re.compile(r"村屋|village\s+house", re.IGNORECASE)


def exclusion_reason(info: EstateInfo, overrides: dict[str, str] | None = None) -> str | None:
    """Why an estate is outside the study (None = keep). Subsidised (HOS, TPS/PRH) and
    houses/villas/village houses are out; see the module docstring for what slips through."""
    action = (overrides or {}).get(info.code)
    if action == "include":
        return None
    if action == "exclude":
        return "manual"
    if info.hos:
        return "hos"
    if info.prh:
        return "tps_prh"
    if info.is_house:
        return "house"
    name = norm_name(info.name)
    if _VILLA.search(name):
        return "villa"
    if _VILLAGE_LOT.search(name):
        return "village_lot"
    if _VILLAGE_HOUSE.search(name):
        return "village_house"
    return None


def load_overrides(path=None) -> dict[str, str]:
    path = path or get_data_dir() / "manual" / "master_overrides.csv"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as fh:
        return {
            r["code"].strip(): r["action"].strip().lower()
            for r in csv.DictReader(fh)
            if r.get("code") and r.get("action")
        }


def block_codes(entry: cd.BlockPage) -> list[str]:
    """The block pages (`1-`) to read for an estate page: the block it opened, the blocks of
    its phase and, for a single building, the one block in its own menu. Sibling phases
    (`2-`) are separate estates in the sitemap."""
    codes = [entry.code] + [c for c, _ in entry.blocks] + [c for c, _ in entry.phases]
    seen: list[str] = []
    for c in codes:
        if c.startswith("1-") and c not in seen:
            seen.append(c)
    return seen


# ---- storing -------------------------------------------------------------------------------------


def estate_id(code: str) -> str:
    return f"cd-{code}"


def save_estate(con, info: EstateInfo, *, excluded: str | None, blocks: int, pilot: bool) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO master_estate VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [info.code, info.parent_code, norm_name(info.name), info.estate_type, info.address,
         info.district, info.hma, info.terr, info.completion, info.units, info.floors,
         info.hos, info.prh, excluded is not None, excluded, pilot, blocks, datetime.now()],
    )


def _gain(raw: dict) -> float | None:
    """Centadata's "% change vs the previous deal" of the flat. The previous deal's date is
    not on the page, so only the percentage (and the price it implies) is known."""
    v = raw.get("gainPercent")
    return float(v) if isinstance(v, (int, float)) else None


def prev_price(price_hkd: int | None, gain_pct: float | None) -> int | None:
    """The previous deal's price implied by the latest price and the % change."""
    if not price_hkd or gain_pct is None or gain_pct <= -100:
        return None
    return round(price_hkd / (1 + gain_pct / 100))


def backfill_gain_pct(con) -> int:
    """Fill gain_pct on Centadata rows loaded before it was stored, from their raw_json."""
    count = ("SELECT count(*) FROM txn_raw WHERE source = 'centadata' AND gain_pct IS NULL")
    before = con.execute(count).fetchone()[0]
    con.execute("UPDATE txn_raw SET gain_pct = try_cast(json_extract_string(raw_json, "
                "'$.gainPercent') AS DOUBLE) WHERE source = 'centadata' AND gain_pct IS NULL")
    return before - con.execute(count).fetchone()[0]


def load_block(con, est: EstateInfo, blk: cd.BlockPage, url: str,
               page: EstateInfo | None = None) -> int:
    """Store one block: estate, building, master_building, units and the latest deal of each
    flat. `est` is the estate page's facts, `page` this block page's own (completion date,
    flats and floors are per block). Returns the number of deals added."""
    page = page or est
    if not blk.code:
        return 0
    eid, bid = estate_id(est.code), f"cd-{blk.code}"
    name = norm_name(est.name)
    labels, pairs, units, rows = [], [], {}, []
    now = datetime.now()
    for d in blk.deals:
        day = d.instrument_date or d.registration_date
        if day is None:
            continue
        fl = parse_floor(d.floor_text)
        labels.append(fl.label)
        pairs.append((d.saleable_sqft, d.gross_sqft))
        uid = db.unit_id(bid, fl.label, d.flat)
        units[uid] = (uid, bid, fl.label, fl.num, fl.kind, d.flat, d.saleable_sqft, d.gross_sqft)
        rows.append({
            "source": cd.SOURCE, "source_id": d.trans_id, "fetched_at": now, "url": url,
            "estate_id": eid, "building_id": bid, "estate_name": name,
            "phase_name": norm_name(blk.phase), "building_name": norm_name(blk.block),
            "floor_label": fl.label, "flat": d.flat, "deal_date": day,
            "instrument_date": d.instrument_date, "registration_date": d.registration_date,
            "deal_type": "registry", "price_hkd": d.price_hkd,
            "saleable_sqft": d.saleable_sqft, "gross_sqft": d.gross_sqft,
            "gain_pct": _gain(d.raw),
            "raw_json": json.dumps(d.raw, ensure_ascii=False, default=str),
        })
    skipped = ",".join(str(x) for x in sorted(infer_skipped(labels)))
    con.execute(
        "INSERT INTO estate (estate_id, name_zh, district, source, source_key) "
        "VALUES (?, ?, ?, 'centadata', ?) ON CONFLICT DO UPDATE SET "
        "name_zh = excluded.name_zh, district = excluded.district",
        [eid, name, est.district, est.code],
    )
    con.execute(
        "INSERT INTO building (building_id, estate_id, name_zh, source_key, completion_date, "
        "floors, flats, saleable_to_gross, skipped_floors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT DO UPDATE SET name_zh = excluded.name_zh, "
        "completion_date = excluded.completion_date, floors = excluded.floors, "
        "flats = excluded.flats, saleable_to_gross = excluded.saleable_to_gross, "
        "skipped_floors = excluded.skipped_floors",
        [bid, eid, norm_name(blk.block), blk.code, page.completion, page.floors, page.units,
         saleable_to_gross(pairs), skipped],
    )
    con.execute(
        "INSERT INTO master_building (building_id, estate_code, block_code, estate_name, "
        "phase_name, block_name, address, completion_date, units, floors, district, hma) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET "
        "estate_name = excluded.estate_name, phase_name = excluded.phase_name, "
        "block_name = excluded.block_name, address = excluded.address, "
        "completion_date = excluded.completion_date, units = excluded.units, "
        "floors = excluded.floors, district = excluded.district, hma = excluded.hma",
        [bid, est.code, blk.code, name, norm_name(blk.phase), norm_name(blk.block),
         blk.address or page.address, page.completion, page.units, page.floors, est.district,
         est.hma],
    )
    if units:
        con.executemany(
            "INSERT INTO unit (unit_id, building_id, floor_label, floor_num, floor_kind, flat, "
            "saleable_sqft, gross_sqft) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET "
            "saleable_sqft = coalesce(excluded.saleable_sqft, unit.saleable_sqft), "
            "gross_sqft = coalesce(excluded.gross_sqft, unit.gross_sqft)",
            list(units.values()),
        )
    return db.insert_txn_raw(con, rows)


# ---- the territory pass --------------------------------------------------------------------------


def pilot_codes(fetcher, log=print) -> set[str]:
    """Centadata `2-` codes of the pilot estates (their phases), read from the entry pages."""
    codes: set[str] = set()
    for name, entry in cd.PILOT_ENTRIES.values():
        page = cd.parse_block_page(fetcher.get(cd.block_url(name, entry), "transaction"))
        codes.add(entry)
        codes.update(c for c, _ in page.phases if c.startswith("2-"))
    log(f"pilot estates cover {len(codes)} Centadata codes (not loaded again)")
    return codes


def _fetch_page(fetcher, name: str, code: str):
    url = cd.block_url(name, code)
    html = fetcher.get(url, "transaction")
    return url, html


def process_estate(fetcher, db_path, code: str, name: str, *, pilot: bool,
                   overrides: dict[str, str]) -> tuple[str | None, int, int]:
    """Read one `2-` page (and its blocks); returns (exclusion reason, blocks, deals added)."""
    url, html = _fetch_page(fetcher, name, code)
    info = parse_estate_info(html)
    if not info.code:
        info.code = code
    entry = cd.parse_block_page(html)
    if pilot:
        with db.session(db_path) as con:
            save_estate(con, info, excluded=None, blocks=0, pilot=True)
        return None, 0, 0
    reason = exclusion_reason(info, overrides)
    if reason:
        with db.session(db_path) as con:
            save_estate(con, info, excluded=reason, blocks=0, pilot=False)
        return reason, 0, 0
    added = 0
    codes = block_codes(entry)
    for bcode in codes:
        if bcode == entry.code:
            burl, bhtml, blk = url, html, entry
        else:
            burl, bhtml = _fetch_page(fetcher, name, bcode)
            blk = cd.parse_block_page(bhtml)
        with db.session(db_path) as con:
            added += load_block(con, info, blk, burl, parse_estate_info(bhtml))
    with db.session(db_path) as con:
        save_estate(con, info, excluded=None, blocks=len(codes), pilot=False)
    return None, len(codes), added


def run_master(fetcher, db_path=None, log=print, limit: int | None = None) -> dict:
    """Walk every estate in the sitemap; resumable through run_progress (one step per code)."""
    xml = fetcher.get(SITEMAP_URL, "static")
    estates = [(c, n) for c, n in sitemap_estates(xml) if c.startswith("2-")]
    overrides = load_overrides()
    pilot = pilot_codes(fetcher, log)
    with db.session(db_path) as con:
        db.init_db(con)
        ensure_tables(con)
        done = {r[0] for r in con.execute(
            "SELECT step_key FROM run_progress WHERE command = ?", [COMMAND]).fetchall()}
        run_id = db.start_run(con, COMMAND, f"limit={limit}" if limit else "")
    todo = [(c, n) for c, n in estates if c not in done]
    if limit:
        todo = todo[:limit]
    log(f"{len(estates)} estates in the sitemap, {len(done)} done, {len(todo)} to do")
    stats = {"estates": 0, "excluded": 0, "blocks": 0, "deals": 0, "errors": 0}
    streak, status, message = 0, "ok", ""
    try:
        for i, (code, name) in enumerate(todo, 1):
            try:
                reason, blocks, added = process_estate(
                    fetcher, db_path, code, name, pilot=code in pilot, overrides=overrides)
            except SourceBlocked:
                raise
            except (FetchError, httpx.HTTPError, cd.NotData, KeyError, ValueError) as e:
                stats["errors"] += 1
                streak += 1
                log(f"  {code} {name}: {type(e).__name__} {e}")
                if streak >= MAX_CONSECUTIVE_ERRORS:
                    raise RuntimeError(f"{streak} errors in a row; stopping (rerun)") from e
                continue
            streak = 0
            stats["estates"] += 1
            stats["excluded"] += reason is not None
            stats["blocks"] += blocks
            stats["deals"] += added
            with db.session(db_path) as con:
                db.mark_step(con, COMMAND, code, run_id, reason or "ok")
            if i % 50 == 0:
                log(f"{i}/{len(todo)} {stats}")
    except BaseException as e:  # incl. Ctrl-C: record, then let it propagate
        status = "interrupted" if isinstance(e, KeyboardInterrupt) else "failed"
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        with db.session(db_path) as con:
            backfill_gain_pct(con)
            db.finish_run(con, run_id, status, stats["deals"], message or json.dumps(stats))
    log(f"done: {stats}")
    return stats
