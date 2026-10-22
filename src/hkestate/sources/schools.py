"""Primary One Admission (POA) school nets, from Education Bureau (EDB) open data.

The EDB does not publish net boundaries as polygons (the GeoInfo map layer is a web map
only). It publishes two things that together give each school's net:

1. "School Location and Information" (DATA.GOV.HK, monthly CSV): every school with
   coordinates, keyed by a 12-digit school number.
2. "Primary School Lists by School Net" (EDB PDF, yearly): the government and aided primary
   schools of each of the ~36 nets, keyed by the first 6 digits of the same school number.

So this is the fallback route of the plan: schools with a net and coordinates. A building
takes the net most common among its 3 nearest schools that have a net (ties: the net of the
nearest school). It is an estimate near net borders; the EDB's own lookup by address
(https://www.map.gov.hk) is the authority. Direct Subsidy, private and ESF schools are
outside the POA system and have no net: they are stored but never used for assignment.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from hkestate import db
from hkestate.geo import update_from_frame

LOCATION_CSV = ("https://www.edb.gov.hk/attachment/en/student-parents/sch-info/sch-search/"
                "sch-location-info/SCH_LOC_EDB.csv")
NET_LISTS_PDF = ("https://www.edb.gov.hk/attachment/en/edu-system/primary-secondary/"
                 "spa-systems/primary-1-admission/school-lists/SchoolLists.pdf")
NEAREST_SCHOOLS = 3

DDL = [
    """
    CREATE TABLE IF NOT EXISTS primary_school (
        school_no VARCHAR PRIMARY KEY,  -- EDB's 12-digit number (the net lists use the first 6)
        name_zh   VARCHAR,
        name_en   VARCHAR,
        lat       DOUBLE,
        lon       DOUBLE,
        finance   VARCHAR,              -- GOVERNMENT, AIDED, DIRECT SUBSIDY SCHEME, ...
        district  VARCHAR,
        net       VARCHAR               -- POA school net, e.g. '11'; NULL outside the system
    )""",
]


class LayoutChanged(Exception):
    """The EDB files no longer look like what the parsers expect."""


def ensure_tables(con) -> None:
    for ddl in DDL:
        con.execute(ddl)
    con.execute("ALTER TABLE master_building ADD COLUMN IF NOT EXISTS school_net VARCHAR")


@dataclass(frozen=True)
class School:
    school_no: str
    name_zh: str
    name_en: str
    lat: float
    lon: float
    finance: str
    district: str
    net: str | None = None


# ---- parsers -----------------------------------------------------------------------------------


def parse_locations(content: bytes | str) -> list[School]:
    """Primary schools (any finance type) with coordinates from the EDB location CSV."""
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("SCHOOL LEVEL") != "PRIMARY":
            continue
        try:
            lat, lon = float(r["LATITUDE"]), float(r["LONGITUDE"])
        except (KeyError, ValueError):
            continue  # "N.A." or missing: unusable for distances
        out.append(School(r["SCHOOL NO."].strip(), r["中文名稱"].strip(), r["ENGLISH NAME"].strip(),
                          lat, lon, r["FINANCE TYPE"].strip(), r["DISTRICT"].strip()))
    return out


_NET_HEADER = "POA School Net"
_TABLE_START = "學校 資助"  # first line of the school table: ends the page's header block


def parse_net_lists(pages: list[str]) -> dict[str, str]:
    """{6-digit school number: net} from the text of the EDB net-list PDF's pages.

    A net page starts with a header block ("POA School Net", the area names, then the net
    number, either at the start of the English area line or alone on a line when the name
    wraps) and then a table whose rows start with the 6-digit school number. A page without
    a header continues the previous net. The contents pages ("POA School Net Areas Covered")
    are not net headers and hold no school rows.
    """
    nets: dict[str, str] = {}
    net = None
    for page in pages:
        in_header = False
        for line in page.splitlines():
            stripped = line.strip()
            if stripped.startswith(_NET_HEADER) and "Areas Covered" not in stripped:
                in_header = True
                continue
            if in_header:
                if stripped.startswith(_TABLE_START):
                    in_header = False
                    continue
                m = re.match(r"^(\d{2})(?:\s|$)", stripped)
                if m:
                    net = m.group(1)
                continue
            m = re.match(r"^(\d{6})\s", stripped)
            if m and net:
                nets[m.group(1)] = net
    return nets


def pdf_pages(content: bytes) -> list[str]:
    """Text of each page of a PDF."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def attach_nets(rows: list[School], nets: dict[str, str]) -> list[School]:
    return [replace(r, net=nets.get(r.school_no[:6])) for r in rows]


# ---- storage and assignment --------------------------------------------------------------------


def store(con, rows: list[School]) -> int:
    """Replace the school table with freshly parsed rows."""
    con.execute("DELETE FROM primary_school")
    if rows:
        con.executemany(
            "INSERT INTO primary_school VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(r.school_no, r.name_zh, r.name_en, r.lat, r.lon, r.finance, r.district, r.net)
             for r in rows])
    return len(rows)


def assign_nets(con) -> int:
    """Set master_building.school_net for every geocoded building: the net most common among
    its 3 nearest netted schools (ties: the net of the nearest of the tied). Returns buildings
    updated; with no school data nothing is changed."""
    schools = con.execute(
        "SELECT lat, lon, net FROM primary_school WHERE net IS NOT NULL").fetchall()
    if not schools:
        return 0
    slat = np.radians(np.array([s[0] for s in schools]))
    slon = np.radians(np.array([s[1] for s in schools]))
    nets = [s[2] for s in schools]
    # a mismatch row's coordinates are distrusted (wrong district): no net from them
    con.execute("UPDATE master_building SET school_net = NULL WHERE geo_status = 'mismatch'")
    buildings = con.execute(
        "SELECT building_id, lat, lon FROM master_building WHERE lat IS NOT NULL "
        "AND lon IS NOT NULL AND geo_status IS DISTINCT FROM 'mismatch'").fetchall()
    chosen = []
    for bid, lat, lon in buildings:
        p1 = np.radians(lat)
        a = (np.sin((slat - p1) / 2) ** 2
             + np.cos(p1) * np.cos(slat) * np.sin((slon - np.radians(lon)) / 2) ** 2)
        order = np.argsort(a, kind="stable")[:NEAREST_SCHOOLS]  # haversine is monotonic in a
        votes = Counter(nets[i] for i in order)
        top = max(votes.values())
        net = next(nets[i] for i in order if votes[nets[i]] == top)  # nearest first
        chosen.append((bid, net))
    # one statement for all buildings: per-row UPDATEs would hold the database file for minutes
    update_from_frame(con, pd.DataFrame(chosen, columns=["building_id", "school_net"]),
                      ["school_net"])
    return len(buildings)


def load(fetcher, db_path=None, log=print) -> int:
    """Download both EDB files (through the polite fetcher), store the schools and assign
    nets to geocoded buildings. Returns buildings assigned. No DB session is open while
    fetching; one short session then writes and assigns."""
    rows = parse_locations(fetcher.get_bytes(LOCATION_CSV, "static"))
    nets = parse_net_lists(pdf_pages(fetcher.get_bytes(NET_LISTS_PDF, "static")))
    rows = attach_nets(rows, nets)
    netted = sum(1 for r in rows if r.net)
    if not netted:
        raise LayoutChanged("no school on the net lists matched the school location file")
    unmatched = len(nets) - netted
    n = _write_and_assign(db_path, rows, log)
    log(f"{len(rows)} primary schools, {netted} with a net ({len(set(nets.values()))} nets); "
        f"{unmatched} listed schools not in the location file")
    return n


def _write_and_assign(db_path, rows: list[School] | None, log) -> int:
    """One short session: store rows (None = keep the stored ones) and assign nets."""
    with db.session(db_path) as con:
        db.init_db(con)
        from hkestate.sources import cd_master
        cd_master.ensure_tables(con)
        ensure_tables(con)
        if rows is not None:
            store(con, rows)
        n = assign_nets(con)
        total = con.execute(
            "SELECT count(*) FROM master_building WHERE lat IS NOT NULL "
            "AND lon IS NOT NULL").fetchone()[0]
    log(f"school net for {n} of {total} geocoded buildings")
    return n


def main(argv=None) -> int:
    import argparse

    from hkestate.cli import _logger
    from hkestate.fetch import Fetcher

    p = argparse.ArgumentParser(prog="python -m hkestate.sources.schools")
    p.add_argument("--db", default=None)
    p.add_argument("--assign-only", action="store_true",
                   help="skip fetching; reuse the stored school rows and re-assign nets")
    args = p.parse_args(argv)
    log = _logger("schools")
    with db.session(args.db) as con:
        db.init_db(con)
        run_id = db.start_run(con, "schools")
    status, message, n = "failed", "", 0
    try:
        if args.assign_only:
            n = _write_and_assign(args.db, None, log)
        else:
            with Fetcher() as f:
                n = load(f, args.db, log)
        status = "ok"
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        with db.session(args.db) as con:
            db.finish_run(con, run_id, status, n, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
