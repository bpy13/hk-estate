"""Territory-wide 28Hse backfill (Task 3.2): which estates, in what order, and the run.

The pilot backfill (`backfill.py`) takes hand-listed estates. For the territory the list of
28Hse estates lives in table `hse28_estate` and comes from, in order of preference:

1. `discover`: 28Hse's own sitemap (robots.txt names `/sitemap_share/sitemap-index.xml`).
   Only child sitemaps with "estate" in their address are read; if there are none, the run
   says so and stops, rather than reading every sitemap.
2. `data/manual/hse28_estates.csv` (columns `id,name`), for a list you made yourself.
3. The 28Hse listing runs: every listing card names its estate (`source_estate_key`).

`link.map_estates` then matches each estate to the master list (Centadata), which gives its
size (flats, latest deals). The run takes the unfinished estates **largest first**
(`--order flats|deals|id`), skipping estates that only match excluded (HOS, TPS, village
house) master estates. Each estate runs the same three steps as the pilot (grid, deal lists,
per-flat pages for 2018 to March 2021) and is then linked to its master buildings. Progress is
in `run_progress`, so stopping and rerunning carries on; use `--max-estates` or `--max-hours`
to run in chunks.

Every request goes to www.28hse.com at the backfill's pace (4 s). Pages are untrusted data.
"""

from __future__ import annotations

import csv
import json
import re
import time
from datetime import datetime
from urllib.parse import unquote

import httpx

from hkestate import db, link
from hkestate.backfill import (
    BACKFILL_MIN_INTERVAL_S,
    PILOT_ESTATES,
    SourceBusy,
    _Loader,
)
from hkestate.config import get_data_dir
from hkestate.fetch import Fetcher, FetchError, SourceBlocked
from hkestate.sources.hse28 import BASE, EstateRef

COMMAND = "backfill"  # same progress table as the pilot backfill (step keys are per estate id)
RUN_COMMAND = "backfill-territory"
SITEMAP_INDEX = f"{BASE}/sitemap_share/sitemap-index.xml"
ESTATE_URL = re.compile(r"/estate/detail/([^/?#]+)-(\d+)(?:[/?#]|$)")
ORDERS = {
    "flats": "size_flats DESC NULLS LAST, size_deals DESC NULLS LAST, id",
    "deals": "size_deals DESC NULLS LAST, size_flats DESC NULLS LAST, id",
    "id": "id",
}


def pilot_ids() -> set[int]:
    return {r.id for refs in PILOT_ESTATES.values() for r in refs}


# ---- the estate list -----------------------------------------------------------------------------


def parse_sitemap(xml: str) -> tuple[list[str], list[EstateRef]]:
    """(child sitemap addresses, estates) from a sitemap or sitemap index. Read with
    regexes: the file is untrusted."""
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    estates: dict[int, EstateRef] = {}
    children = []
    for loc in locs:
        m = ESTATE_URL.search(loc)
        if m:
            estates.setdefault(int(m.group(2)), EstateRef(unquote(m.group(1)), int(m.group(2))))
        elif loc.endswith(".xml") or ".xml" in loc:
            children.append(loc)
    return children, list(estates.values())


def discover(fetcher, log=print) -> list[EstateRef]:
    """Estates from 28Hse's sitemap (a handful of requests)."""
    index = fetcher.get(SITEMAP_INDEX, "static")
    children, estates = parse_sitemap(index)
    found = {e.id: e for e in estates}
    wanted = [c for c in children if "estate" in c.lower()]
    log(f"sitemap index: {len(children)} child sitemaps, {len(wanted)} about estates")
    if children and not wanted and not found:
        log("no estate sitemap among: " + ", ".join(children[:10]))
    for url in wanted:
        _, more = parse_sitemap(fetcher.get(url, "static"))
        found.update({e.id: e for e in more})
    log(f"{len(found)} estates found in the sitemap")
    return list(found.values())


def add_estates(con, refs, found_by: str) -> int:
    """Add estates to the index (existing ids keep their data). Returns how many were new."""
    link.ensure_tables(con)
    have = {r[0] for r in con.execute("SELECT id FROM hse28_estate").fetchall()}
    new = [(r.id, r.name, found_by) for r in refs if r.id not in have]
    if new:
        con.executemany("INSERT INTO hse28_estate (id, name, found_by) VALUES (?, ?, ?)", new)
    return len(new)


def read_csv(path=None) -> list[EstateRef]:
    path = path or get_data_dir() / "manual" / "hse28_estates.csv"
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return [EstateRef(r["name"].strip(), int(r["id"]))
                for r in csv.DictReader(fh) if r.get("id", "").strip().isdigit()]


def refs_from_listings(con) -> list[EstateRef]:
    """Estates named on 28Hse listing cards (listing runs store the id and name)."""
    rows = con.execute(
        "SELECT source_estate_key, any_value(estate_name) FROM listing_raw "
        "WHERE source = 'hse28' AND source_estate_key ~ '^[0-9]+$' AND estate_name IS NOT NULL "
        "GROUP BY 1").fetchall()
    return [EstateRef(name.replace(" ", "-"), int(key)) for key, name in rows]


# ---- ordering ------------------------------------------------------------


def finished_ids(con, skip_flats: bool = False) -> set[int]:
    """Estates whose last step is recorded (or that 28Hse no longer has). With `skip_flats`
    the deal-list step is the last one."""
    rows = con.execute(
        "SELECT step_key FROM run_progress WHERE command = ? AND "
        "(step_key LIKE 'hse28:%:flats' OR step_key LIKE 'hse28:%:gone' "
        " OR step_key LIKE 'hse28:%:list')", [COMMAND]).fetchall()
    done = set()
    for (key,) in rows:
        _, eid, step = key.split(":", 2)
        if step in ("flats", "gone") or (step == "list" and skip_flats):
            done.add(int(eid))
    return done

def plan(con, *, order: str = "flats", mapped_only: bool = False, skip_flats: bool = False,
         skip_ids: set[int] | None = None) -> list[EstateRef]:
    """Unfinished estates in run order: matched estates first (largest first), then
    unmatched ones by id, unless `mapped_only`. Excluded estates are never run."""
    if order not in ORDERS:
        raise ValueError(f"order must be one of {sorted(ORDERS)}")
    skip = (pilot_ids() if skip_ids is None else skip_ids) | finished_ids(con, skip_flats)
    where = "NOT coalesce(excluded, false)"
    if mapped_only:
        where += " AND match_status IN ('exact', 'prefix')"
    rows = con.execute(
        f"SELECT id, name FROM hse28_estate WHERE {where} ORDER BY "
        f"(match_status IN ('exact', 'prefix')) DESC NULLS LAST, {ORDERS[order]}").fetchall()
    return [EstateRef(n, i) for i, n in rows if i not in skip]


# ---- the run ------------------------------------------------------------


def run_territory(
    *,
    fetcher=None,
    db_path=None,
    log=print,
    order: str = "flats",
    mapped_only: bool = False,
    skip_flats: bool = False,
    max_estates: int | None = None,
    max_hours: float | None = None,
    skip_ids: set[int] | None = None,
    pause=time.sleep,
    clock=time.monotonic,
) -> dict:
    """Backfill the unfinished estates in order. Stops after `max_estates` / `max_hours`
    (checked between estates) or when the site keeps refusing; rerun to resume."""
    with db.session(db_path) as con:
        db.init_db(con)
        link.ensure_tables(con)
        counts = link.map_estates(con, skip_ids)
        todo = plan(con, order=order, mapped_only=mapped_only, skip_flats=skip_flats,
                    skip_ids=skip_ids)
        run_id = db.start_run(con, RUN_COMMAND, f"order={order} mapped_only={mapped_only}")
    log(f"{len(todo)} estates to do; name matching {counts}")
    own = fetcher is None
    fetcher = fetcher or Fetcher(min_interval=BACKFILL_MIN_INTERVAL_S)
    loader = _Loader(fetcher, db_path, log, run_id, pause=pause)
    stats = {"estates": 0, "gone": 0, "errors": 0, "linked": 0}
    status, message = "ok", ""
    started = clock()
    try:
        for i, ref in enumerate(todo, 1):
            if max_estates is not None and stats["estates"] >= max_estates:
                message = f"stopped after {max_estates} estates"
                break
            if max_hours is not None and clock() - started > max_hours * 3600:
                message = f"stopped after {max_hours} hours"
                break
            log(f"[{i}/{len(todo)}] {ref.name} ({ref.id})")
            try:
                loader.grid(ref, None)
                with db.session(db_path) as con:
                    stats["linked"] += link.link_buildings(con, ref.id)["mapped"]
                loader.lists(ref)
                if not skip_flats:
                    loader.flats(ref)
                loader.finish_estate(ref)
            except FetchError as e:
                if e.status == 404:  # 28Hse no longer has the estate
                    loader.mark(f"hse28:{ref.id}:gone", "404")
                    stats["gone"] += 1
                else:
                    stats["errors"] += 1
                    log(f"  estate failed ({e.status}); will be retried on the next run")
                continue
            except httpx.TransportError as e:
                stats["errors"] += 1
                log(f"  estate failed ({type(e).__name__}); will be retried on the next run")
                continue
            stats["estates"] += 1
    except KeyboardInterrupt:
        status, message = "interrupted", "stopped by user"
        raise
    except SourceBlocked as e:
        status, message = "failed", f"source blocked: {e}"
        log(message)
    except SourceBusy as e:
        status, message = "partial", f"28Hse busy, rerun later: {e}"
        log(message)
    except BaseException as e:
        status, message = "failed", f"{type(e).__name__}: {e}"
        raise
    finally:
        with db.session(db_path) as con:
            db.finish_run(con, run_id, status, loader.rows_added,
                          (message + " " + json.dumps(stats)).strip())
        if own:
            fetcher.close()
        log(f"{status}: {stats}, {loader.rows_added} deal rows added "
            f"at {datetime.now():%H:%M}")
    return stats
