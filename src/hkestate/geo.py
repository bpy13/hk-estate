"""Location attributes for buildings: coordinates, nearest MTR station, district, age.

Coordinates come from the government Address Lookup Service (ALS): one request per building,
using the street address Centadata shows. ALS also names the district, which is kept next to
Centadata's own. Distance is straight-line to the nearest MTR *station centre* (decisions.md:
the CSDI exit data is behind a disallowed API).

ALS knows few MTR stations by name (14 of 97 resolved exactly), so the rest come from
Wikipedia's station articles (title coordinates; ordinary article pages, which robots.txt
allows). A hand-made file `data/manual/mtr_stations.csv` (code,lat,lon) overrides both.
Distances are only written once every station has coordinates, because a station without
them could be the true nearest one.
"""

from __future__ import annotations

import csv
import io
import math
import re
import time
from dataclasses import dataclass
from datetime import date
from html import unescape
from urllib.parse import quote

import httpx

from hkestate import db
from hkestate.config import get_data_dir
from hkestate.fetch import FetchError, SourceBlocked

ALS_URL = "https://www.als.gov.hk/lookup?q={q}&n=1"
MTR_CSV = "https://opendata.mtr.com.hk/data/mtr_lines_and_stations.csv"

GEO_DDL = [
    """
    CREATE TABLE IF NOT EXISTS mtr_station (
        code    VARCHAR PRIMARY KEY,
        name_zh VARCHAR,
        name_en VARCHAR,
        lat     DOUBLE,
        lon     DOUBLE
    )""",
    # which stations follow each other on each line: the commute graph's input
    """
    CREATE TABLE IF NOT EXISTS mtr_line_station (
        line_code    VARCHAR,
        direction    VARCHAR,
        station_code VARCHAR,
        sequence     DOUBLE
    )""",
]


def ensure_tables(con) -> None:
    for ddl in GEO_DDL:
        con.execute(ddl)


# ---- ALS -------------------------------------------------------------------------------------


@dataclass
class AlsResult:
    lat: float
    lon: float
    name_zh: str
    district_zh: str
    score: float | None


def _tag(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", block)
    return unescape(m.group(1)) if m else ""


def parse_als(xml: str) -> AlsResult | None:
    """The first suggested address of an ALS answer (read with regexes: it is untrusted)."""
    m = re.search(r"<SuggestedAddress>.*?</SuggestedAddress>", xml, re.S)
    if not m:
        return None
    block = m.group(0)
    lat, lon = _tag(block, "Latitude"), _tag(block, "Longitude")
    if not lat or not lon:
        return None
    chi = re.search(r"<ChiPremisesAddress>.*?</ChiPremisesAddress>", block, re.S)
    chi_text = chi.group(0) if chi else ""
    score = _tag(block, "Score")
    return AlsResult(float(lat), float(lon), _tag(chi_text, "BuildingName"),
                     _tag(chi_text, "DcDistrict"), float(score) if score else None)


def als_query(address: str | None) -> str | None:
    """'沙田/銀城街1號' -> '銀城街1號'; a range '1-3號' -> '1號'."""
    if not address:
        return None
    street = address.split("/", 1)[-1].strip()
    street = re.sub(r"(\d+[A-Za-z]?)\s*[-–]\s*\d+[A-Za-z]?(?=號)", r"\1", street)
    return street or None


def seed_queries(con) -> int:
    """Fill geo_query for buildings that have an address (pilot 28Hse buildings, which have
    none, are queried by estate + block name)."""
    con.execute("""
        INSERT INTO master_building (building_id, estate_name, block_name, district, geo_query)
        SELECT b.building_id, e.name_zh, b.name_zh, e.district, e.name_zh || ' ' || b.name_zh
        FROM building b JOIN estate e USING (estate_id)
        WHERE e.source = 'hse28'
          AND b.building_id NOT IN (SELECT building_id FROM master_building)""")
    rows = con.execute(
        "SELECT building_id, address FROM master_building "
        "WHERE geo_query IS NULL AND address IS NOT NULL").fetchall()
    for bid, addr in rows:
        q = als_query(addr)
        if q:
            con.execute("UPDATE master_building SET geo_query = ? WHERE building_id = ?", [q, bid])
    return len(rows)


def lookup(fetcher, query: str) -> AlsResult | None:
    return parse_als(fetcher.get(ALS_URL.format(q=quote(query)), "static"))


def geocode_buildings(fetcher, db_path=None, log=print, limit: int | None = None) -> dict:
    """Geocode every master building not yet tried. Resumable: geo_status marks the done ones."""
    with db.session(db_path) as con:
        ensure_tables(con)
        seed_queries(con)
        todo = con.execute(
            "SELECT building_id, geo_query, district FROM master_building "
            "WHERE geo_status IS NULL AND geo_query IS NOT NULL ORDER BY building_id"
        ).fetchall()
    if limit:
        todo = todo[:limit]
    log(f"geocoding {len(todo)} buildings")
    stats = {"ok": 0, "no_match": 0, "error": 0}
    for i, (bid, query, district) in enumerate(todo, 1):
        try:
            res = lookup(fetcher, query)
            # the same street name exists in several districts: retry with the district
            if res and district and res.district_zh and district[:2] != res.district_zh[:2]:
                retry = lookup(fetcher, f"{district} {query}")
                if retry and retry.district_zh[:2] == district[:2]:
                    res = retry
            status = "ok" if res else "no_match"
        except SourceBlocked:
            raise
        except (FetchError, httpx.HTTPError) as e:
            log(f"  {bid}: {type(e).__name__} {e}")
            stats["error"] += 1
            continue
        stats[status] += 1
        with db.session(db_path) as con:
            con.execute(
                "UPDATE master_building SET lat = ?, lon = ?, geo_status = ?, geo_name = ?, "
                "geo_district = ?, geo_at = now() WHERE building_id = ?",
                [res.lat if res else None, res.lon if res else None, status,
                 res.name_zh if res else None, res.district_zh if res else None, bid],
            )
        if i % 200 == 0:
            log(f"{i}/{len(todo)} {stats}")
    log(f"geocoding done: {stats}")
    return stats


# ---- MTR ---------------------------------------------------------------------------------------


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def nearest_station(lat: float, lon: float, stations) -> tuple[str, float]:
    """(station, metres) for the closest of [(name, lat, lon), ...]."""
    return min(((n, haversine_m(lat, lon, la, lo)) for n, la, lo in stations),
               key=lambda x: x[1])


WIKI_LIST = "https://en.wikipedia.org/wiki/List_of_MTR_stations"
HK_BOX = (22.15, 22.60, 113.80, 114.45)  # south, north, west, east: anything else is a mistake


def _name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def wiki_station_links(html: str) -> dict[str, str]:
    """Station article addresses in Wikipedia's list of MTR stations, by comparable English
    name ("Admiralty station (MTR)" -> "admiralty"). Plain article pages only."""
    from urllib.parse import unquote

    out: dict[str, str] = {}
    for url in re.findall(r'href="(https://en\.wikipedia\.org/wiki/[^"#?]+)"', html):
        slug = unquote(url.rsplit("/", 1)[1]).replace("_", " ")
        if ":" in slug or not re.search(r"station", slug, re.IGNORECASE):
            continue
        base = re.sub(r"\s*\(.*?\)", "", slug)
        base = re.sub(r"\s+station$", "", base, flags=re.IGNORECASE)
        out.setdefault(_name_key(base), url)
    return out


def wiki_coords(html: str) -> tuple[float, float] | None:
    """The article's title coordinates (decimal degrees), if inside Hong Kong."""
    # the machine-readable copy: <span class="geo">22.2788; 114.1646</span> (the visible
    # degrees-minutes-seconds spans would need converting)
    m = re.search(r'<span class="geo">\s*(-?[\d.]+)\s*;\s*(-?[\d.]+)', html)
    if not m:
        m = re.search(r'class="geo-dec"[^>]*>\s*([\d.]+)°N\s*([\d.]+)°E', html)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    s, n, w, e = HK_BOX
    return (lat, lon) if s <= lat <= n and w <= lon <= e else None


def wikipedia_coords(fetcher, wanted: set[str], log=print) -> dict[str, tuple[float, float]]:
    """Coordinates for the stations whose comparable names are in `wanted`, from their
    Wikipedia articles (ordinary article pages; robots.txt allows them, the API it does not).
    One request for the list and one per station."""
    links = wiki_station_links(fetcher.get(WIKI_LIST, "static"))
    out = {}
    for key in sorted(wanted & links.keys()):
        try:
            pos = wiki_coords(fetcher.get(links[key], "static"))
        except (FetchError, httpx.HTTPError) as e:
            log(f"  wikipedia {key}: {type(e).__name__} {e}")
            continue
        if pos:
            out[key] = pos
    return out


def store_lines(con, text: str) -> int:
    """Replace the `mtr_line_station` rows with those of the open-data CSV text; returns the
    number of rows kept (rows without a station code are skipped)."""
    rows = [(r["Line Code"], r["Direction"], r["Station Code"], float(r["Sequence"]))
            for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))) if r.get("Station Code")]
    ensure_tables(con)
    con.execute("DELETE FROM mtr_line_station")
    if rows:
        con.executemany("INSERT INTO mtr_line_station VALUES (?, ?, ?, ?)", rows)
    return len(rows)


def load_stations(fetcher, db_path=None, log=print) -> int:
    """MTR station list (open data) with coordinates from ALS where it names the station
    exactly, then from data/manual/mtr_stations.csv. Returns stations still without coordinates."""
    text = fetcher.get(MTR_CSV, "static").lstrip("﻿")
    stations: dict[str, tuple[str, str]] = {}
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("Station Code"):
            stations.setdefault(r["Station Code"], (r["Chinese Name"], r["English Name"]))
    manual = {}
    path = get_data_dir() / "manual" / "mtr_stations.csv"
    if path.exists():
        with open(path, encoding="utf-8", newline="") as fh:
            manual = {r["code"]: (float(r["lat"]), float(r["lon"])) for r in csv.DictReader(fh)}
    with db.session(db_path) as con:
        ensure_tables(con)
        store_lines(con, text)
        have = {r[0] for r in con.execute(
            "SELECT code FROM mtr_station WHERE lat IS NOT NULL").fetchall()}
    wiki = None
    for code, (zh, en) in stations.items():
        if code in have:
            continue
        pos = manual.get(code)
        if pos is None:
            for q in (f"港鐵{zh}站", f"MTR {en} Station"):
                res = lookup(fetcher, q)
                if res and res.name_zh.startswith(f"港鐵{zh}站"):
                    pos = (res.lat, res.lon)
                    break
        if pos is None:
            if wiki is None:  # fetched once, only if some station is still without a position
                wiki = wikipedia_coords(
                    fetcher, {_name_key(e) for c, (_, e) in stations.items() if c not in have},
                    log)
            pos = wiki.get(_name_key(en))
        with db.session(db_path) as con:
            con.execute("INSERT OR REPLACE INTO mtr_station VALUES (?, ?, ?, ?, ?)",
                        [code, zh, en, pos[0] if pos else None, pos[1] if pos else None])
    with db.session(db_path) as con:
        missing = con.execute("SELECT count(*) FROM mtr_station WHERE lat IS NULL").fetchone()[0]
    log(f"{len(stations)} MTR stations, {missing} without coordinates")
    return missing


def update_from_frame(con, frame, columns: list[str]) -> None:
    """Write `columns` of master_building for every row of `frame` (which has a building_id
    column) in ONE statement. Per-row UPDATEs of tens of thousands of buildings keep the
    database file locked for minutes; this takes seconds."""
    if frame.empty:
        return
    con.register("_upd", frame)
    try:
        sets = ", ".join(f"{c} = _upd.{c}" for c in columns)
        con.execute(f"UPDATE master_building SET {sets} FROM _upd "
                    "WHERE master_building.building_id = _upd.building_id")
    finally:
        con.unregister("_upd")


def mark_mismatches(con) -> int:
    """Set geo_status = 'mismatch' on 'ok' geocodes whose ALS district differs from the
    building's own district (same first-two-characters test as geocode_buildings). Buildings
    with no district of their own are left alone. Returns rows newly marked."""
    n = con.execute(
        "SELECT count(*) FROM master_building WHERE geo_status = 'ok' AND district IS NOT NULL "
        "AND geo_district IS NOT NULL AND left(district, 2) <> left(geo_district, 2)"
    ).fetchone()[0]
    con.execute(
        "UPDATE master_building SET geo_status = 'mismatch' WHERE geo_status = 'ok' "
        "AND district IS NOT NULL AND geo_district IS NOT NULL "
        "AND left(district, 2) <> left(geo_district, 2)")
    return n


def assign_mtr(con) -> int:
    """Nearest-station distance for every geocoded building; 0 if any station lacks
    coordinates (see module docstring). Returns buildings updated."""
    if con.execute("SELECT count(*) FROM mtr_station WHERE lat IS NULL").fetchone()[0]:
        return 0
    stations = con.execute("SELECT name_zh, lat, lon FROM mtr_station").fetchall()
    if not stations:
        return 0
    # a mismatch row's coordinates are distrusted: no distance from them
    con.execute("UPDATE master_building SET mtr_station = NULL, mtr_m = NULL "
                "WHERE geo_status = 'mismatch'")
    rows = con.execute(
        "SELECT building_id, lat, lon FROM master_building WHERE lat IS NOT NULL "
        "AND geo_status IS DISTINCT FROM 'mismatch'").fetchall()
    if not rows:
        return 0
    import numpy as np
    import pandas as pd

    slat = np.radians(np.array([s[1] for s in stations], dtype=float))
    slon = np.radians(np.array([s[2] for s in stations], dtype=float))
    names = [s[0] for s in stations]
    out_name, out_m = [], []
    for _, lat, lon in rows:  # one numpy pass per building over the ~100 stations
        p1 = math.radians(lat)
        a = (np.sin((slat - p1) / 2) ** 2
             + math.cos(p1) * np.cos(slat) * np.sin((slon - math.radians(lon)) / 2) ** 2)
        i = int(a.argmin())  # haversine is monotonic in a; ties go to the first station
        out_name.append(names[i])
        out_m.append(round(2 * 6_371_000 * math.asin(math.sqrt(a[i]))))
    frame = pd.DataFrame({"building_id": [r[0] for r in rows], "mtr_station": out_name,
                          "mtr_m": out_m})
    update_from_frame(con, frame, ["mtr_station", "mtr_m"])
    return len(rows)


# ---- district ----------------------------------------------------------------------------------

NEAREST_DISTRICT_M = 1000  # last resort: a neighbour further away may be in another district


def _als_district(text) -> str | None:
    """ALS's Chinese district ('沙田區') in the same 18-district naming as the rest of the data;
    None for anything else (English names, empty)."""
    found = re.findall(r"([^\s()（）|]+區)", str(text or ""))
    return found[-1] if found else None


def fill_districts(con) -> int:
    """Set master_building.district where it is NULL; existing values are never changed.
    In order: (1) the building's own ALS answer, (2) the most common district of the other
    buildings of the same estate, (3) the district of the nearest geocoded building within
    1 km. Returns the number of buildings set."""
    import pandas as pd

    def write(pairs: list[tuple[str, str]]) -> int:
        update_from_frame(con, pd.DataFrame(pairs, columns=["building_id", "district"]),
                          ["district"])
        return len(pairs)

    n = 0
    found = []
    for bid, gd in con.execute(
            "SELECT building_id, geo_district FROM master_building "
            "WHERE district IS NULL AND geo_district IS NOT NULL").fetchall():
        d = _als_district(gd)
        if d:
            found.append((bid, d))
    n += write(found)
    # ties go to the alphabetically first name, so a rerun gives the same answer
    rows = con.execute("""
        SELECT m.building_id, (SELECT d.district FROM master_building d
                               WHERE d.estate_code = m.estate_code AND d.district IS NOT NULL
                               GROUP BY d.district ORDER BY count(*) DESC, d.district LIMIT 1)
        FROM master_building m WHERE m.district IS NULL AND m.estate_code IS NOT NULL""").fetchall()
    n += write([(bid, d) for bid, d in rows if d])
    import numpy as np

    donors = con.execute("SELECT lat, lon, district FROM master_building "
                         "WHERE district IS NOT NULL AND lat IS NOT NULL "
                         "AND geo_status IS DISTINCT FROM 'mismatch'").fetchall()
    todo = con.execute("SELECT building_id, lat, lon FROM master_building "
                       "WHERE district IS NULL AND lat IS NOT NULL "
                       "AND geo_status IS DISTINCT FROM 'mismatch'").fetchall()
    if donors and todo:
        dlat = np.array([r[0] for r in donors])
        dlon = np.array([r[1] for r in donors])
        names = [r[2] for r in donors]
        near = []
        for bid, lat, lon in todo:
            p1, p2 = np.radians(lat), np.radians(dlat)
            a = (np.sin((p2 - p1) / 2) ** 2
                 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(dlon - lon) / 2) ** 2)
            dist = 2 * 6_371_000 * np.arcsin(np.sqrt(a))
            i = int(dist.argmin())
            if dist[i] <= NEAREST_DISTRICT_M:
                near.append((bid, names[i]))
        n += write(near)
    return n


# ---- age ---------------------------------------------------------------------------------------


def set_building_ages(con, today: date | None = None) -> None:
    """built_year from the completion date (Centadata; 1900/1950-or-earlier placeholders are
    ignored), else the year of the building's first recorded deal (a proxy: marked)."""
    year = (today or date.today()).year
    con.execute(
        f"""
        UPDATE master_building SET
            built_year = y, built_basis = basis, age_years = {year} - y
        FROM (
            SELECT b.building_id,
                CASE WHEN year(b.completion_date) > 1950 THEN year(b.completion_date)
                     ELSE f.first_year END AS y,
                CASE WHEN year(b.completion_date) > 1950 THEN 'completion'
                     WHEN f.first_year IS NOT NULL THEN 'first_deal' END AS basis
            FROM master_building b
            LEFT JOIN (SELECT building_id, min(year(deal_date)) AS first_year
                       FROM txn_raw GROUP BY building_id) f USING (building_id)
        ) s
        WHERE master_building.building_id = s.building_id
        """
    )


# ---- command line: python -m hkestate.geo [--limit N] ---------------------------------------


def main(argv=None) -> int:
    import argparse

    from hkestate.cli import _logger
    from hkestate.fetch import Fetcher

    p = argparse.ArgumentParser(prog="python -m hkestate.geo")
    p.add_argument("--db", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--stations-only", action="store_true",
                   help="only load MTR stations and set distances (no building geocoding)")
    args = p.parse_args(argv)
    log = _logger("geo")
    with db.session(args.db) as con:
        db.init_db(con)
        from hkestate.sources import cd_master
        cd_master.ensure_tables(con)
        ensure_tables(con)
        run_id = db.start_run(con, "geo")
    status, message, stats = "failed", "", {}
    try:
        with Fetcher() as f:
            load_stations(f, args.db, log)
            stats = {} if args.stations_only else geocode_buildings(f, args.db, log, args.limit)
        # one short session per step, so other jobs waiting for the database file get a turn
        def step(name, fn):
            t = time.perf_counter()
            with db.session(args.db) as con:
                result = fn(con)
            log(f"{name}: {time.perf_counter() - t:.1f}s")
            return result

        bad = step("mark_mismatches", mark_mismatches)
        log(f"{bad} geocodes marked mismatch (ALS district differs from the building's)")
        n = step("assign_mtr", assign_mtr)
        step("set_building_ages", set_building_ages)
        filled = step("fill_districts", fill_districts)
        log(f"district filled for {filled} buildings")
        with db.session(args.db) as con:
            total, ok = con.execute(
                "SELECT count(*), count(lat) FROM master_building").fetchone()
        log(f"coordinates for {ok}/{total} buildings; MTR distance set for {n}")
        status = "ok"
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        with db.session(args.db) as con:
            db.finish_run(con, run_id, status, stats.get("ok", 0), message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
