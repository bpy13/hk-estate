"""Transaction backfill from 28Hse, estate by estate, resumable.

For each 28Hse estate:
1. grid  - the phase and block menus and every block grid: phases, buildings and flats,
           with the number of deals 28Hse holds for each flat (since 2018).
2. list  - every page of the estate deal list (newest first, back to about March 2021).
3. flats - the per-flat page of each flat with more deals than the lists showed: this is
           where deals from 2018 to March 2021 come from.

Each finished step is written to `run_progress`, so a stopped run carries on where it left
off; pages are also cached by the fetcher for 7 days. Deals go into the append-only
`txn_raw`, where an unchanged deal is never stored twice.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from hkestate import db
from hkestate.fetch import Fetcher, FetchError, SourceBlocked
from hkestate.normalise import infer_skipped, norm_name, parse_floor
from hkestate.sources import hse28
from hkestate.sources.hse28 import EstateRef

COMMAND = "backfill"
COOLDOWN_S = 900  # pause after the site answers 429/5xx despite the fetcher's back-off
BUSY_LIMIT = 3  # tries per page before the run stops (it resumes on the next run)
# Gap between 28Hse requests for backfills. 28Hse answered 429 after ~45 minutes (~1,600
# requests) at the 1 s minimum and then allowed only ~25 requests per 10-minute pause, which
# looks like a rolling hourly quota; 4 s (~900 an hour) stays under it.
BACKFILL_MIN_INTERVAL_S = 4.0


class SourceBusy(Exception):
    """The site keeps answering 'too many requests' or server errors: stop and resume later."""

# Pilot estates (decisions.md). 28Hse lists LOHAS Park as separate developments.
PILOT_ESTATES: dict[str, list[EstateRef]] = {
    "taikoo-shing": [EstateRef("太古城", 1140)],
    "mei-foo": [EstateRef("美孚新邨", 2520)],
    "city-one": [EstateRef("沙田第一城", 4080)],
    "lohas-park": [
        EstateRef(name, id_)
        for name, id_ in [
            ("首都", 26141),
            ("領都", 23560),
            ("領峯", 23563),
            ("領凱", 23567),
            ("緻藍天", 26145),
            ("MALIBU", 23599),
            ("LP6", 26149),
            ("MONTARA", 23609),
            ("Grand-Montara", 23611),
            ("Marini", 23613),
            ("Grand-Marini", 23614),
            ("晉海", 23595),
            ("晉海II", 26155),
            ("Ocean-Marini", 23619),
            ("Sea-To-Sky", 23621),
            ("LP10", 23624),
            ("凱柏峰-I", 23747),
            ("凱柏峰-II", 23986),
            ("凱柏峰-III", 28425),
            ("Seasons-Place", 28780),
            ("Park-Seasons", 28857),
            ("Grand-Seasons", 32982),
        ]
    ],
    "goldwin-heights": [EstateRef("高雲臺", 383)],
}

Log = Callable[[str], None]


def estate_id(ref: EstateRef) -> str:
    return f"h28-{ref.id}"


def building_id(ref: EstateRef, stateno: int | None, blockno: int | None) -> str:
    state = f"-s{stateno}" if stateno is not None else ""
    return f"h28-{ref.id}{state}-b{blockno if blockno is not None else 0}"


# Character variants the sources use for the same name (臺/台 in estate names, 廬/盧 ...).
_VARIANTS = str.maketrans({"臺": "台", "廬": "盧", "峯": "峰"})
_RANGE = re.compile(r"^(.*?)(\d+)-(\d+)(號)$")


def _key(text: str) -> str:
    # 街/道/路/徑 before a street number are treated alike: the sources disagree
    # (Centadata 蘭秀道1-3號, 28Hse 蘭秀街1號).
    return re.sub(r"[街道路徑](?=\d)", "街", norm_name(text).translate(_VARIANTS))


def match_buildings(location: str, buildings: dict[str, tuple[str, str]]) -> list[str]:
    """Buildings named in `location` text. `buildings` maps building_id -> (phase text,
    block name). An address range such as '百老匯街1-3號' (Centadata groups Mei Foo's blocks
    this way) returns every numbered building in it; otherwise at most one building."""
    if len(buildings) == 1:
        return [next(iter(buildings))]
    tokens = {t for t in re.split(r"[\s()（）]+", _key(location)) if t}
    is_range = False
    for t in list(tokens):
        m = _RANGE.match(t)
        if m and int(m.group(3)) - int(m.group(2)) < 20:
            is_range = True
            tokens |= {f"{m.group(1)}{n}{m.group(4)}"
                       for n in range(int(m.group(2)), int(m.group(3)) + 1)}
    hits = [b for b, (_, block) in buildings.items() if _key(block) in tokens]
    if len(hits) > 1:
        # the phase text may be several words (e.g. estate + phase); any word can decide
        narrowed = [
            b
            for b in hits
            if _key(buildings[b][0]) in tokens
            or any(w in tokens for w in _key(buildings[b][0]).split())
        ]
        if narrowed:
            hits = narrowed
    if is_range or len(hits) == 1:
        return hits
    return []


def match_building(location: str, buildings: dict[str, tuple[str, str]]) -> str | None:
    """The one building named in `location` (e.g. an agency-reported deal), else None."""
    hits = match_buildings(location, buildings)
    return hits[0] if len(hits) == 1 else None


class _Loader:
    def __init__(self, fetcher, db_path, log: Log, run_id: int, pause=time.sleep,
                 page_type: str = "transaction"):
        self.pause = pause
        self.page_type = page_type  # cache lifetime: 'listing' (1 day) forces a fresh read
        self.f = fetcher
        self.db_path = db_path
        self.log = log
        self.run_id = run_id
        self.rows_added = 0

    # ---- helpers ----------------------------------------------------------------------------

    def get(self, url: str) -> str:
        """Fetch a page. A network error, or the site still answering 429/5xx after the
        fetcher's own back-off, gets a long cool-down and another try; after BUSY_LIMIT
        tries the run stops with SourceBusy (rerun later: it resumes)."""
        for attempt in range(1, BUSY_LIMIT + 1):
            try:
                return self.f.get(url, self.page_type)
            except httpx.TransportError as e:
                reason = type(e).__name__
            except FetchError as e:
                if e.status != 429 and (e.status or 0) < 500:
                    raise
                reason = f"HTTP {e.status}"
            if attempt == BUSY_LIMIT:
                raise SourceBusy(f"{reason} {BUSY_LIMIT} times in a row: {url}")
            self.log(f"  site busy ({reason}); pausing {COOLDOWN_S // 60} min: {url}")
            self.pause(COOLDOWN_S)
        raise AssertionError("unreachable")

    def done(self, key: str) -> bool:
        with db.session(self.db_path) as con:
            return db.step_done(con, COMMAND, key)

    def mark(self, key: str, detail: str = "") -> None:
        with db.session(self.db_path) as con:
            db.mark_step(con, COMMAND, key, self.run_id, detail)

    def insert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with db.session(self.db_path) as con:
            n = db.insert_txn_raw(con, rows)
        self.rows_added += n
        return n

    # ---- step 1: grid -----------------------------------------------------------------------

    def grid(self, ref: EstateRef, group: str) -> None:
        key = f"hse28:{ref.id}:grid"
        if self.done(key):
            return
        eid = estate_id(ref)
        first = hse28.parse_history_page(self.get(ref.history_url()))
        phases = first.phases
        # Which grid pages to read: (stateno, blockno, block name)
        targets: list[tuple[int | None, int | None, str]] = []
        if phases:
            for stateno, _ in phases:
                page = first if stateno == first.current_state else hse28.parse_history_page(
                    self.get(ref.history_url(stateno=stateno))
                )
                targets += [(stateno, b, name) for b, name in page.blocks]
        elif first.blocks:
            targets = [(None, b, name) for b, name in first.blocks]
        else:
            targets = [(None, None, ref.name)]  # one block: the grid is on /history itself

        phase_rows = [(f"{eid}-s{s}", eid, name, str(s)) for s, name in phases]
        units: list[tuple] = []
        buildings: dict[str, tuple] = {}
        for stateno, blockno, block_name in targets:
            if blockno is None:
                page = first
            else:
                try:
                    page = hse28.parse_history_page(
                        self.get(ref.history_url(stateno=stateno, blockno=blockno))
                    )
                except FetchError as e:
                    self.log(f"  grid page missing ({e.status}): {ref.name} s{stateno} b{blockno}")
                    continue
            for u in page.units:
                s = u.stateno if u.stateno is not None else stateno
                b = u.blockno if u.blockno is not None else blockno
                bid = building_id(ref, s, b)
                if bid not in buildings:
                    buildings[bid] = (eid, f"{eid}-s{s}" if s is not None else None,
                                      block_name, str(b), [])
                fl = parse_floor(u.floor_text)
                buildings[bid][4].append(fl.label)
                units.append(
                    (db.unit_id(bid, fl.label, u.flat), bid, fl.label, fl.num, fl.kind, u.flat,
                     u.saleable_sqft, u.rooms, u.unit_key, u.deal_count, u.url)
                )
            if blockno is None:
                continue
            # a block with no flats listed still gets a row
            bid = building_id(ref, stateno, blockno)
            buildings.setdefault(
                bid,
                (eid, f"{eid}-s{stateno}" if stateno is not None else None, block_name,
                 str(blockno), []),
            )
        with db.session(self.db_path) as con:
            con.execute(
                "INSERT INTO estate (estate_id, name_zh, group_name, source, source_key) "
                "VALUES (?, ?, ?, 'hse28', ?) ON CONFLICT DO UPDATE SET "
                "name_zh = excluded.name_zh, group_name = excluded.group_name",
                [eid, norm_name(ref.name.replace("-", " ")), group, str(ref.id)],
            )
            for row in phase_rows:
                con.execute(
                    "INSERT INTO phase VALUES (?, ?, ?, ?) ON CONFLICT DO UPDATE SET "
                    "name_zh = excluded.name_zh",
                    list(row),
                )
            for bid, (e, p, name, bkey, labels) in buildings.items():
                skipped = ",".join(str(x) for x in sorted(infer_skipped(labels)))
                con.execute(
                    "INSERT INTO building (building_id, estate_id, phase_id, name_zh, source_key, "
                    "skipped_floors) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET "
                    "name_zh = excluded.name_zh, skipped_floors = excluded.skipped_floors",
                    [bid, e, p, norm_name(name), bkey, skipped],
                )
            if units:  # DuckDB's executemany rejects an empty list (an estate with no flats)
                con.executemany(
                    "INSERT INTO unit (unit_id, building_id, floor_label, floor_num, floor_kind,"
                    " flat, saleable_sqft, rooms, source_unit_key, src_deal_count, src_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET "
                    "saleable_sqft = coalesce(excluded.saleable_sqft, unit.saleable_sqft), "
                    "rooms = coalesce(excluded.rooms, unit.rooms), "
                    "source_unit_key = excluded.source_unit_key, "
                    "src_deal_count = excluded.src_deal_count, src_url = excluded.src_url",
                    units,
                )
        self.log(
            f"  grid: {len(phase_rows)} phases, {len(buildings)} buildings, {len(units)} flats, "
            f"{sum(u[9] for u in units)} deals held by 28Hse"
        )
        self.mark(key, f"{len(units)} flats")

    # ---- step 2: deal lists --------------------------------------------------------------------

    def _buildings(self, ref: EstateRef) -> dict[str, tuple[str, str]]:
        with db.session(self.db_path) as con:
            rows = con.execute(
                "SELECT b.building_id, coalesce(p.name_zh, ''), b.name_zh FROM building b "
                "LEFT JOIN phase p USING (phase_id) WHERE b.estate_id = ?",
                [estate_id(ref)],
            ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def lists(self, ref: EstateRef) -> None:
        key = f"hse28:{ref.id}:list"
        if self.done(key):
            return
        buildings = self._buildings(ref)
        url: str | None = ref.list_url(1)
        pages = rows_seen = added = 0
        seen_pages: set[tuple] = set()
        while url:
            page = hse28.parse_list_page(self.get(url))
            ids = tuple(r.source_id for r in page.rows)
            if not ids or ids in seen_pages:  # the last page repeats once past the end
                break
            seen_pages.add(ids)
            pages += 1
            rows_seen += len(page.rows)
            added += self.insert([self._list_row(ref, r, url, buildings) for r in page.rows])
            if pages % 25 == 0:
                last = page.rows[-1].deal_date
                self.log(f"  list: {pages} pages, {rows_seen} deals, back to {last}")
            url = page.next_url
        self.log(f"  list: {pages} pages, {rows_seen} deals read, {added} new rows")
        self.mark(key, f"{pages} pages, {rows_seen} deals")

    def _list_row(self, ref, r: hse28.ListRow, url: str, buildings) -> dict:
        if r.blockno is not None:
            bid = building_id(ref, r.stateno, r.blockno)
        else:
            bid = match_building(r.location, buildings)
        fl = parse_floor(r.floor_text)
        raw = {
            "labels": r.labels,
            "with_parking": r.with_parking,
            "location": r.location,
            "floor_text": r.floor_text,
            "href": r.href,
            "page": url,
        }
        return {
            "source": hse28.SOURCE,
            "source_id": r.source_id,
            "fetched_at": datetime.now(),
            "url": r.href,
            "estate_id": estate_id(ref),
            "building_id": bid,
            "source_unit_key": r.unit_key,
            "estate_name": r.estate_name,
            "building_name": r.location,
            "floor_label": fl.label,
            "flat": r.flat,
            "deal_date": r.deal_date,
            "deal_type": r.deal_type,
            "price_hkd": r.price_hkd,
            "saleable_sqft": r.saleable_sqft,
            "rooms": r.rooms,
            "gain_pct": r.gain_pct,
            "first_hand": r.first_hand,
            "raw_json": json.dumps(raw, ensure_ascii=False),
        }

    # ---- step 3: per-flat pages ------------------------------------------------------------------

    def flats(self, ref: EstateRef) -> None:
        key = f"hse28:{ref.id}:flats"
        if self.done(key):
            return
        with db.session(self.db_path) as con:
            todo = con.execute(
                """
                SELECT u.unit_id, u.building_id, u.floor_label, u.flat, u.source_unit_key,
                       u.src_url, u.src_deal_count
                FROM unit u JOIN building b USING (building_id)
                WHERE b.estate_id = ? AND u.src_deal_count > (
                    SELECT count(*) FROM txn_latest t
                    WHERE t.source = 'hse28' AND t.deal_type = 'registry'
                      AND t.source_unit_key = u.source_unit_key)
                ORDER BY u.unit_id
                """,
                [estate_id(ref)],
            ).fetchall()
            # flats an older run gave up on because the site was busy are tried again
            done = {
                r[0]
                for r in con.execute(
                    "SELECT step_key FROM run_progress "
                    "WHERE command = ? AND starts_with(step_key, ?) "
                    "AND NOT (coalesce(detail, '') LIKE 'error 429%' "
                    "         OR coalesce(detail, '') LIKE 'error 5%')",
                    [COMMAND, f"hse28:{ref.id}:flat:"],
                ).fetchall()
            }
        todo = [t for t in todo if f"hse28:{ref.id}:flat:{t[4]}" not in done]
        self.log(f"  flats: {len(todo)} per-flat pages to read")
        for i, (uid, bid, floor_label, flat, ukey, url, _) in enumerate(todo, 1):
            step = f"hse28:{ref.id}:flat:{ukey}"
            try:
                page = hse28.parse_unit_page(self.get(url))
            except FetchError as e:  # a lasting error (e.g. 404); busy answers raise instead
                self.log(f"  flat page failed ({e.status}): {url}")
                self.mark(step, f"error {e.status}")
                continue
            rows = [
                {
                    "source": hse28.SOURCE,
                    "source_id": f"u{ukey}:{d.deal_date.isoformat()}",
                    "fetched_at": datetime.now(),
                    "url": url,
                    "estate_id": estate_id(ref),
                    "building_id": bid,
                    "source_unit_key": ukey,
                    "building_name": f"{page.phase_name or ''} {page.block_name or ''}".strip(),
                    "floor_label": floor_label,
                    "flat": flat,
                    "deal_date": d.deal_date,
                    "deal_type": d.deal_type,
                    "price_hkd": d.price_hkd,
                    "saleable_sqft": page.saleable_sqft,
                    "gross_sqft": page.gross_sqft,
                    "rooms": page.rooms,
                    "raw_json": json.dumps(
                        {"deal_label": d.deal_label, "title": page.title}, ensure_ascii=False
                    ),
                }
                for d in page.deals
            ]
            self.insert(rows)
            with db.session(self.db_path) as con:
                con.execute(
                    "UPDATE unit SET gross_sqft = coalesce(?, gross_sqft), "
                    "saleable_sqft = coalesce(?, saleable_sqft), rooms = coalesce(?, rooms) "
                    "WHERE unit_id = ?",
                    [page.gross_sqft, page.saleable_sqft, page.rooms, uid],
                )
                db.mark_step(con, COMMAND, step, self.run_id, f"{len(rows)} deals")
            if i % 100 == 0:
                self.log(f"  flats: {i}/{len(todo)}")
        self.mark(key, f"{len(todo)} pages")

    def finish_estate(self, ref: EstateRef) -> None:
        """Building saleable-to-gross ratio from flats where both areas are known."""
        with db.session(self.db_path) as con:
            con.execute(
                """
                UPDATE building SET saleable_to_gross = r.ratio FROM (
                    SELECT building_id, median(saleable_sqft / gross_sqft) AS ratio
                    FROM unit WHERE saleable_sqft > 0 AND gross_sqft > 0 GROUP BY building_id
                ) r WHERE building.building_id = r.building_id AND building.estate_id = ?
                """,
                [estate_id(ref)],
            )


UPDATE_PAGE_CAP = 50  # a week brings a few pages per estate; this is a safety stop


def update_estates(
    estates: dict[str, list[EstateRef]],
    *,
    fetcher=None,
    db_path: Path | str | None = None,
    log: Log = print,
    pause=time.sleep,
) -> int:
    """Weekly refresh: read each loaded estate's deal list from page 1 (a fresh copy, not the
    7-day cache) and stop at the first page that adds nothing new. Estates the backfill has
    not loaded yet are skipped (the backfill picks them up). Returns rows added."""
    with db.session(db_path) as con:
        db.init_db(con)
        run_id = db.start_run(con, "update", json.dumps(sorted(estates), ensure_ascii=False))
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(min_interval=BACKFILL_MIN_INTERVAL_S)
    loader = _Loader(fetcher, db_path, log, run_id, pause=pause, page_type="listing")
    status, message = "ok", ""
    try:
        for group, refs in estates.items():
            for ref in refs:
                buildings = loader._buildings(ref)
                if not buildings:
                    log(f"{group}: {ref.name} not loaded yet; skipped (run the backfill)")
                    continue
                url: str | None = ref.list_url(1)
                pages = added = 0
                while url and pages < UPDATE_PAGE_CAP:
                    try:
                        page = hse28.parse_list_page(loader.get(url))
                    except FetchError as e:
                        log(f"  {ref.name}: list page failed ({e.status}): {url}")
                        break
                    pages += 1
                    n = loader.insert([loader._list_row(ref, r, url, buildings)
                                       for r in page.rows])
                    added += n
                    if n == 0:
                        break  # everything on this page is already stored
                    url = page.next_url
                log(f"{group}: {ref.name}: {pages} pages, {added} new rows")
    except SourceBlocked as e:
        status, message = "failed", f"source blocked: {e}"
        log(message)
    except BaseException as e:
        status, message = "failed", f"{type(e).__name__}: {e}"
        raise
    finally:
        with db.session(db_path) as con:
            db.finish_run(con, run_id, status, loader.rows_added, message)
        if own_fetcher:
            fetcher.close()
    return loader.rows_added


def backfill_estates(
    estates: dict[str, list[EstateRef]],
    *,
    fetcher=None,
    db_path: Path | str | None = None,
    log: Log = print,
    refresh: bool = False,
    skip_flats: bool = False,
    pause=time.sleep,
) -> int:
    """Load every estate in `estates` (group name -> 28Hse estates). Returns rows added."""
    with db.session(db_path) as con:
        db.init_db(con)
        con.execute(
            "UPDATE run_log SET status = 'interrupted', finished_at = current_timestamp "
            "WHERE command = ? AND status = 'running'",
            [COMMAND],
        )
        if refresh:
            for refs in estates.values():
                for ref in refs:
                    db.clear_steps(con, COMMAND, f"hse28:{ref.id}:")
        run_id = db.start_run(con, COMMAND, json.dumps(sorted(estates), ensure_ascii=False))
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(min_interval=BACKFILL_MIN_INTERVAL_S)
    loader = _Loader(fetcher, db_path, log, run_id, pause=pause)
    status, message = "ok", ""
    try:
        for group, refs in estates.items():
            for ref in refs:
                log(f"{group}: {ref.name} ({ref.id})")
                loader.grid(ref, group)
                loader.lists(ref)
                if not skip_flats:
                    loader.flats(ref)
                loader.finish_estate(ref)
    except KeyboardInterrupt:
        status, message = "interrupted", "stopped by user"
        raise
    except SourceBlocked as e:
        status, message = "failed", f"source blocked: {e}"
        log(message)
    except BaseException as e:
        status, message = "failed", f"{type(e).__name__}: {e}"
        raise
    finally:
        with db.session(db_path) as con:
            db.finish_run(con, run_id, status, loader.rows_added, message)
        if own_fetcher:
            fetcher.close()
    return loader.rows_added
