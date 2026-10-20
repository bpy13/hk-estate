"""Full per-flat sale history from Centadata (back to 1995), for the growth study.

The master pass (`cd_master.py`) and the cross-check (`centadata.py`) store one deal per
flat: the latest. Centadata's own block pages ask for the rest with one call per click,
`POST https://hk.centanet.com/findproperty/api/Transaction/Search` with the body
`{"cuntcodes": [<flat code>, ...], "size": 100, "offset": 0}`. robots.txt does not list this
path (it forbids only `/centanetapi/api/`, a different one); docs/sources.md "Centadata
history" has the details and the measured limits.

Flat codes (`cuntcode`) come from the latest-deal rows already in `txn_raw`, so each old
sale lands on the same building and unit as that flat's latest deal, whichever code placed
it (the master pass for the territory, the cross-check for the pilot estates). Sales are
stored in `txn_raw` with `source='centadata'` and `source_id` = the sale's `id`; a sale
already stored (the latest deal of each flat) is left alone.

Only the sale fields in `KEEP_FIELDS` are kept. No agent or owner fields exist in the
answer, and pictures, floor plans and map text are dropped. Answers are untrusted data.

Progress is kept per building (`run_progress`, command `cd-history`), so a restart skips
finished buildings. Buildings of one estate share batches (the old sales are placed by flat
code, not by building), and a building counts as done only when every batch holding one of
its flats has been stored.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from hkestate import db
from hkestate.fetch import FetchError
from hkestate.patience import RETRY, Patience
from hkestate.patience import kind as patience_kind
from hkestate.sources import centadata as cd

SOURCE = cd.SOURCE
COMMAND = "cd-history"
API_URL = "https://hk.centanet.com/findproperty/api/Transaction/Search"
PAGE_SIZE = 100  # the largest size the server honoured (500 gave HTTP 500)
BATCH_CODES = 40  # flats have about 2 sales each, so a batch is mostly one page
# hk.centanet.com is shared with the listings (2 s) and the master pass, which run at the same
# time: two jobs at 2 s is about 1 request a second, robots.txt's crawl-delay, and listings
# plus the master pass ran like that for days without trouble (2026-10-16 to 19). The 2026-10-20
# 429s followed one job at a 1 s gap. The fetcher slows down by itself if the site pushes back.
MIN_INTERVAL_S = 2.0
MAX_CONSECUTIVE_ERRORS = 5  # non-throttle errors (404, bad answers) in a row: stop
MAX_OFFSET = 10_000  # the site's own script never asks past this

KEEP_FIELDS = (
    "id", "cuntcode", "insDate", "regDate", "transactionPrice", "gainPercent", "nArea", "gArea",
    "nUnitPrice", "gUnitPrice", "prevInsDate", "prevRegDate", "prevTransactionPrice", "heldDay",
    "yAxis", "xAxis", "buildingName", "estateName", "bigEstateName", "firstOrSecondHand",
    "dataSource",
)


@dataclass
class Flat:
    code: str
    estate_id: str
    building_id: str
    estate_name: str
    phase_name: str
    building_name: str
    floor_label: str
    flat: str


@dataclass
class Building:
    building_id: str
    flats: list[Flat] = field(default_factory=list)


@dataclass
class Estate:
    estate_id: str
    buildings: list[Building] = field(default_factory=list)

    @property
    def flat_count(self) -> int:
        return sum(len(b.flats) for b in self.buildings)


# ---- reading the answer ---------------------------------------------------------------------


def parse_response(text: str) -> tuple[int, list[dict]]:
    """(count of all sales for the asked flats, this page's rows)."""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("unexpected Transaction/Search answer (no 'data' list)")
    count = data.get("count")
    return (count if isinstance(count, int) else len(data["data"])), data["data"]


def keep_fields(item: dict) -> dict:
    return {k: item[k] for k in KEEP_FIELDS if k in item}


def _day(text):
    return cd._d(text)


def sale_row(item: dict, flat: Flat | None, url: str, fetched_at: datetime) -> dict | None:
    """A txn_raw row for one sale, placed like the flat's latest deal; None to skip it
    (flat unknown, no id, no price, no usable date)."""
    if flat is None or not item.get("id"):
        return None
    price = item.get("transactionPrice")
    ins, reg = _day(item.get("insDate")), _day(item.get("regDate"))
    if not isinstance(price, (int, float)) or price <= 0 or not (ins or reg):
        return None
    gain = item.get("gainPercent")
    return {
        "source": SOURCE, "source_id": str(item["id"]), "fetched_at": fetched_at, "url": url,
        "estate_id": flat.estate_id, "building_id": flat.building_id,
        "estate_name": flat.estate_name, "phase_name": flat.phase_name,
        "building_name": flat.building_name, "floor_label": flat.floor_label,
        "flat": flat.flat, "deal_date": ins or reg, "instrument_date": ins,
        "registration_date": reg, "deal_type": "registry", "price_hkd": int(round(price)),
        "saleable_sqft": cd._area(item.get("nArea")), "gross_sqft": cd._area(item.get("gArea")),
        "gain_pct": float(gain) if isinstance(gain, (int, float)) else None,
        "raw_json": json.dumps(keep_fields(item), ensure_ascii=False, default=str),
    }


# ---- which flats ----------------------------------------------------------------------------


def load_flats(con, estates: list[str] | None = None) -> list[Estate]:
    """Flat codes from the stored Centadata latest deals, grouped estate -> building.

    `estates`: None for all, else estate group names (`estate.group_name`, e.g.
    'taikoo-shing') or 'pilot' (every group). Largest estates (most flats) first, and the
    largest buildings first within an estate."""
    where = ""
    if estates:
        if "pilot" in estates:
            where = "AND estate_id IN (SELECT estate_id FROM estate WHERE group_name IS NOT NULL)"
        else:
            names = ", ".join("'" + n.replace("'", "''") + "'" for n in estates)
            where = f"AND estate_id IN (SELECT estate_id FROM estate WHERE group_name IN ({names}))"
    rows = con.execute(
        f"""
        SELECT code, estate_id, building_id, estate_name, phase_name, building_name,
               floor_label, flat
        FROM (
            SELECT CASE WHEN json_valid(raw_json)
                        THEN json_extract_string(raw_json, '$.cuntcode') END AS code,
                   estate_id, building_id, estate_name, phase_name, building_name,
                   floor_label, flat,
                   row_number() OVER (
                       PARTITION BY CASE WHEN json_valid(raw_json)
                                         THEN json_extract_string(raw_json, '$.cuntcode') END
                       ORDER BY fetched_at DESC) AS rn
            FROM txn_raw
            WHERE source = 'centadata' AND building_id IS NOT NULL
              AND NOT {db.QUARANTINE_SQL} {where}
        ) WHERE rn = 1 AND code IS NOT NULL
        ORDER BY estate_id, building_id, floor_label, flat
        """
    ).fetchall()
    by_estate: dict[str, dict[str, Building]] = {}
    for code, eid, bid, en, pn, bn, fl, fl_flat in rows:
        flat = Flat(code, eid, bid, en or "", pn or "", bn or "", fl or "", fl_flat or "")
        by_estate.setdefault(eid, {}).setdefault(bid, Building(bid)).flats.append(flat)
    out = []
    for eid, buildings in by_estate.items():
        blist = sorted(buildings.values(), key=lambda b: (-len(b.flats), b.building_id))
        out.append(Estate(eid, blist))
    out.sort(key=lambda e: (-e.flat_count, e.estate_id))
    return out


# ---- reading and storing --------------------------------------------------------------------


def fetch_sales(fetcher, codes: list[str]) -> list[dict]:
    """Every sale of the flats, reading all pages of the answer."""
    rows: list[dict] = []
    offset = 0
    while True:
        body = {"cuntcodes": codes, "size": PAGE_SIZE, "offset": offset}
        count, page = parse_response(fetcher.post_json(API_URL, body, "transaction"))
        rows += page
        offset += PAGE_SIZE
        if not page or len(rows) >= count or offset >= MAX_OFFSET:
            return rows


def store_sales(con, items: list[dict], flats: dict[str, Flat]) -> int:
    """Add the sales not yet stored. Returns rows added."""
    ids = [str(i["id"]) for i in items if i.get("id")]
    have: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        have.update(r[0] for r in con.execute(
            f"SELECT source_id FROM txn_raw WHERE source = ? AND source_id IN ({marks})",
            [SOURCE, *chunk]).fetchall())
    now = datetime.now()
    rows = [r for i in items if str(i.get("id")) not in have
            if (r := sale_row(i, flats.get(i.get("cuntcode")), API_URL, now))]
    return db.insert_txn_raw(con, rows)


# ---- the run --------------------------------------------------------------------------------


def _batches(estate: Estate, todo: list[Building], size: int):
    """(codes, buildings finished once this batch is stored) for one estate's buildings."""
    seq = [(b.building_id, f) for b in todo for f in b.flats]
    last_index = {}
    for i, (bid, _) in enumerate(seq):
        last_index[bid] = i
    for start in range(0, len(seq), size):
        end = start + size
        chunk = seq[start:end]
        yield ([f.code for _, f in chunk],
               [bid for bid, i in last_index.items() if start <= i < end],
               {bid for bid, _ in chunk})


def run_history(fetcher, db_path=None, log=print, estates: list[str] | None = None,
                limit: int | None = None, max_hours: float | None = None,
                wait=time.sleep, clock=time.monotonic) -> dict:
    """Load full sale histories, largest estates first; resumable per building.

    `limit` counts buildings (this run). Runs alongside the Centanet listings (same host; the
    two paces together stay under what worked for days) and writes a run_log row. `wait` is
    the sleep used for its own cool-downs. When the site pushes back (429/503, 5xx, timeouts) it
    waits and tries the same batch again, for as long as it takes (patience.py); a batch
    that keeps failing with a 5xx on its own is skipped and its buildings stay unfinished."""
    with db.session(db_path) as con:
        db.init_db(con)
        all_estates = load_flats(con, estates)
        done = {r[0] for r in con.execute(
            "SELECT step_key FROM run_progress WHERE command = ?", [COMMAND]).fetchall()}
        run_id = db.start_run(
            con, COMMAND, ",".join(estates or ["all"]) + (f" limit={limit}" if limit else ""))
    # the buildings still to do, in order, cut to the limit
    plan: list[tuple[Estate, list[Building]]] = []
    left = limit if limit else None
    for est in all_estates:
        todo = [b for b in est.buildings if b.building_id not in done]
        if left is not None:
            todo = todo[:left]
            left -= len(todo)
        if todo:
            plan.append((est, todo))
        if left == 0:
            break
    total = sum(len(t) for _, t in plan)
    log(f"{sum(e.flat_count for e in all_estates)} flats in {len(all_estates)} estates; "
        f"{total} buildings to do ({len(done)} already done)")
    flats = {f.code: f for e in all_estates for b in e.buildings for f in b.flats}
    stats = {"buildings": 0, "batches": 0, "sales": 0, "deals": 0, "errors": 0, "stopped": ""}
    start, streak, status, message = clock(), 0, "ok", ""
    patience = Patience(log, what="cd-history", fetcher=fetcher, sleep=wait)
    try:
        for est, todo in plan:
            failed: set[str] = set()
            for codes, finished, touched in _batches(est, todo, BATCH_CODES):
                if max_hours is not None and clock() - start > max_hours * 3600:
                    stats["stopped"] = "max-hours"
                    break
                items = None
                while items is None:
                    try:
                        items = fetch_sales(fetcher, codes)
                    except (FetchError, httpx.HTTPError, ValueError) as e:
                        stats["errors"] += 1
                        log(f"  {est.estate_id}: {type(e).__name__} {e}")
                        if patience_kind(e) is not None:
                            if patience.on_error(e) == RETRY:
                                continue  # same batch again (after the cool-down)
                        else:  # 404, a parse error: as before, 5 in a row stop the run
                            streak += 1
                            if streak >= MAX_CONSECUTIVE_ERRORS:
                                raise RuntimeError(
                                    f"{streak} errors in a row; stopping (rerun)") from e
                        failed |= touched
                        break
                if items is None:
                    continue
                streak = 0
                patience.ok()
                with db.session(db_path) as con:
                    stats["deals"] += store_sales(con, items, flats)
                    for bid in finished:
                        if bid not in failed:
                            db.mark_step(con, COMMAND, bid, run_id, "ok")
                            stats["buildings"] += 1
                stats["batches"] += 1
                stats["sales"] += len(items)
                if stats["batches"] % 50 == 0:
                    log(f"{stats['buildings']}/{total} buildings {stats}")
            if stats["stopped"]:
                break
    except BaseException as e:  # incl. Ctrl-C: record, then let it propagate
        status = "interrupted" if isinstance(e, KeyboardInterrupt) else "failed"
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        with db.session(db_path) as con:
            db.finish_run(con, run_id, status, stats["deals"], message or json.dumps(stats))
    log(f"done: {stats}")
    return stats
