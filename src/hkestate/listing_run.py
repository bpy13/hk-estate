"""The daily listing run for the pilot estates: fetch, store, match, snapshot, flag.

Per source, per estate group:
- Centanet: the detail sitemap lists every live listing; detail pages are read for new
  listings and, because the sitemap's lastmod is always today, for a rotating seventh of
  known listings each day (oldest read first), so every price is re-read within a week.
  2 s per request (slower when the site pushes back), holding the `centanet` lock (a marker;
  cd-history and the master pass run alongside at their own slower paces). Every due page
  is read (no default cap).
  Each batch of 25 pages records its reads, so a stopped run doesn't read them again.
- 28Hse: the estate's search pages, every day; a detail page only for a new listing or a
  changed price. Skipped while another job holds the `hse28` lock (a backfill), since 28Hse
  rate-limits hard.
- Spacious: the neighbourhood search pages, every day, cards only.

A source that finished its whole scope records a run day; listings missing from three run
days in a row are delisted (listings.py). Then every listing in the group is matched to a
building and floor band, cross-site duplicates are grouped, and same-flat relistings linked.

The three sources run at the same time, each in its own thread (different hosts); the
finishing steps (run days, delisting, matching, flags) run after all of them are done.
The database is opened only in short sessions (one at a time per process, db.session), so a
backfill can share it.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from hkestate import db, flags, listings, locks, match
from hkestate.backfill import BACKFILL_MIN_INTERVAL_S, PILOT_ESTATES
from hkestate.fetch import Fetcher, FetchError, RobotsDisallowed, SourceBlocked, _hk_today
from hkestate.listing_types import Listing
from hkestate.patience import RETRY, STOP, Patience
from hkestate.patience import kind as patience_kind
from hkestate.sources import centanet, hse28_listings, spacious

Log = Callable[[str], None]

ROTATE_DAYS = 7  # every known Centanet listing is re-read at least this often
PAGE_CAP = 60  # search pages per target, a safety stop
MAX_ERROR_SHARE = 0.05  # more failed pages than this: the run doesn't count for delisting
# hk.centanet.com (robots crawl-delay 1) also serves the Centadata jobs (cd-history 6 s, the
# master pass 4 s), which run at the same time (no pauses since 2026-10-20): 0.67 requests
# a second with cd-history, 0.92 if the rare master pass runs too. The `centanet` lock is
# held around the Centanet part as a marker.
# 2 s base: at 1 s, Centanet answered five HTTP 500s in a row after ~800 pages (~23 min) on
# 2026-10-20, and cd-history then drew 429s; 2 s had run 8,000 pages without trouble. The
# fetcher slows down further by itself when the site pushes back.
CENTANET_MIN_INTERVAL_S = 2.0
CENTANET_LOCK = "centanet"
# Circuit breaker for errors that are not throttling (e.g. HTTP 400): this many detail pages
# in a row failing means something is wrong; stop that source for the run.
MAX_CONSECUTIVE_ERRORS = 5
# Throttling (429/503, 5xx runs, timeouts): cool down 10, 20, 30 min and resume; after the
# third cool-down the source is given up for the day (patience.py).
MAX_COOLDOWNS = 3
LISTING_COOLDOWNS_S = (10 * 60, 20 * 60, 30 * 60)

TERRITORY = "territory"  # every estate outside the pilot groups
# Optional caps on the territory pages read per run (None = read everything due). The caps
# (30,000 Centanet, 6,000 Spacious) were a run-length choice of the 2026-10-16 build plan;
# the owner removed them on 2026-10-20: every due page is read however long it takes (the
# first Centanet pass ~26,000 pages, ~14 h at 2 s; later ~6,000 a day). The sources run in
# parallel threads, so Spacious and 28Hse don't wait behind Centanet.
# Spacious has no "due" rule like Centanet's, so with no budget it reads a seventh of its
# buildings a run (oldest read first; see _spacious_quota), not all ~33,500 (~27 h).
CENTANET_TERRITORY_BUDGET: int | None = None
SPACIOUS_TERRITORY_BUDGET: int | None = None

SP = "https://www.spacious.hk/zh-tw/%E9%A6%99%E6%B8%AF/n/"
SP_SALE = "/%E8%B2%B7%E6%A8%93"  # 買樓


@dataclass(frozen=True)
class Target:
    centanet: tuple[str, ...]  # estate names as Centanet's listing slugs start
    hse28: tuple[tuple[str, int], ...]  # (28Hse area/district path, estate id)
    spacious: tuple[tuple[str, tuple[str, ...]], ...]  # (search url, building-name filter)


def _lohas_hse28() -> tuple[tuple[str, int], ...]:
    return tuple(("a3/dg41", ref.id) for ref in PILOT_ESTATES["lohas-park"])


PILOT: dict[str, Target] = {
    "taikoo-shing": Target(
        ("太古城",), (("a1/dg9", 1140),),
        ((SP + "48-%E5%A4%AA%E5%8F%A4%E5%9F%8E" + SP_SALE, ("太古城",)),),
    ),
    "mei-foo": Target(
        ("美孚新邨",), (("a2/dg107", 2520),),
        ((SP + "88-%E8%8D%94%E6%9E%9D%E8%A7%92" + SP_SALE, ("美孚新邨", "美孚新村")),),
    ),
    "city-one": Target(
        ("沙田第一城",), (("a3/dg44", 4080),),
        ((SP + "77-%E6%B2%99%E7%94%B0" + SP_SALE, ("沙田第一城",)),),
    ),
    "lohas-park": Target(
        ("日出康城",), _lohas_hse28(),
        ((SP + "128-%E6%97%A5%E5%87%BA%E5%BA%B7%E5%9F%8E" + SP_SALE, ()),),
    ),
    "goldwin-heights": Target(
        ("高雲臺", "高雲台"), (("a1/dg3", 383),),
        ((SP + "35-%E8%A5%BF%E5%8D%8A%E5%B1%B1" + SP_SALE, ("高雲臺", "高雲台")),),
    ),
}


class _Breaker:
    """Counts consecutive failed fetches; `fail()` is True once the source should stop."""

    def __init__(self) -> None:
        self.n = 0

    def ok(self) -> None:
        self.n = 0

    def fail(self) -> bool:
        self.n += 1
        return self.n >= MAX_CONSECUTIVE_ERRORS

    @property
    def message(self) -> str:
        return f"circuit breaker: {self.n} consecutive fetch errors"


@dataclass
class SourceResult:
    source: str
    group: str
    seen: list[str] = field(default_factory=list)
    parsed: list[Listing] = field(default_factory=list)  # read today (flags, last_detail_at)
    detail_read: set[str] = field(default_factory=set)
    pages: int = 0
    errors: int = 0
    complete: bool = True
    note: str = ""


def match_text(lst_estate: str | None, building_text: str | None) -> str | None:
    if lst_estate and building_text and not building_text.startswith(lst_estate):
        return f"{lst_estate} {building_text}"
    return building_text or lst_estate


class ListingRun:
    def __init__(self, *, db_path=None, log: Log = print, fetcher=None, hse28_fetcher=None,
                 now: datetime | None = None, today: date | None = None,
                 centanet_budget: int | None = CENTANET_TERRITORY_BUDGET,
                 spacious_budget: int | None = SPACIOUS_TERRITORY_BUDGET,
                 sleep: Callable[[float], None] = time.sleep):
        self.centanet_budget = centanet_budget
        self.spacious_budget = spacious_budget
        self.sleep = sleep
        self.db_path = db_path
        self.log = log
        self.now = now or datetime.now()
        self.today = today or _hk_today()
        self._fetcher = fetcher
        self._centanet_fetcher = fetcher
        self._hse28_fetcher = hse28_fetcher
        self.results: list[SourceResult] = []

    # ---- helpers -------------------------------------------------------------------------

    # The listing fetchers use the daily run's short cool-downs (10/20/30 min) and make no
    # probe of their own: after a 429 the item is retried by `_patience` (which counts the
    # cool-downs and gives the source up for the day after MAX_COOLDOWNS).
    @staticmethod
    def _new_fetcher(min_interval: float = 1.0) -> Fetcher:
        return Fetcher(min_interval=min_interval, cooldowns_s=LISTING_COOLDOWNS_S,
                       throttle_probes=0)

    @property
    def fetcher(self):
        if self._fetcher is None:
            self._fetcher = self._new_fetcher()
        return self._fetcher

    @property
    def centanet_fetcher(self):
        if self._centanet_fetcher is None:
            self._centanet_fetcher = self._new_fetcher(CENTANET_MIN_INTERVAL_S)
        return self._centanet_fetcher

    @property
    def hse28_fetcher(self):
        if self._hse28_fetcher is None:
            self._hse28_fetcher = self._new_fetcher(BACKFILL_MIN_INTERVAL_S)
        return self._hse28_fetcher

    def _patience(self, what: str, fetcher) -> Patience:
        return Patience(self.log, what=what, fetcher=fetcher, sleep=self.sleep,
                        cooldowns_s=LISTING_COOLDOWNS_S, max_cooldowns=MAX_COOLDOWNS)

    def close(self):
        seen = set()
        for f in (self._fetcher, self._centanet_fetcher, self._hse28_fetcher):
            if f is not None and id(f) not in seen:
                seen.add(id(f))
                f.close()

    def _store(self, group: str, items: list[Listing], page_type: str) -> None:
        if items:
            with db.session(self.db_path) as con:
                listings.store_raw(con, items, datetime.now(), group=group, page_type=page_type)
                if page_type == "detail":
                    # Progress lands batch by batch, so a stopped run doesn't read these pages
                    # again. Listings new today have no `listing` row until `finish`; `_known`
                    # finds them from their stored detail page instead.
                    con.execute(
                        "UPDATE listing SET last_detail_at = ? WHERE source = ? "
                        "AND listing_id IN (SELECT unnest(?))",
                        [self.now, items[0].source, sorted({x.listing_id for x in items})],
                    )

    def _known(self, source: str) -> dict[str, tuple]:
        """listing_id -> (last detail read, price) for every listing of the source: the
        `listing` row, or for a listing whose run stopped before `finish` made that row (or
        recorded its read), the time of its newest stored detail page (an unchanged re-read
        stores nothing, so this can be earlier than the last read, never later)."""
        with db.session(self.db_path) as con:
            return {
                r[0]: (r[1], r[2])
                for r in con.execute(
                    """
                    WITH d AS (
                        SELECT listing_id, max(fetched_at) AS t,
                               arg_max(price_hkd, fetched_at) AS price
                        FROM listing_raw WHERE source = ? AND page_type = 'detail'
                        GROUP BY listing_id)
                    SELECT l.listing_id, coalesce(l.last_detail_at, d.t), l.price_hkd
                    FROM listing l LEFT JOIN d USING (listing_id) WHERE l.source = ?
                    UNION ALL
                    SELECT listing_id, t, price FROM d WHERE listing_id NOT IN (
                        SELECT listing_id FROM listing WHERE source = ?)
                    """,
                    [source, source, source],
                ).fetchall()
            }

    # ---- Centanet --------------------------------------------------------------------------

    def centanet(self, groups: list[str]) -> None:
        """Read Centanet holding the `centanet` lock: the Centadata jobs on the same host
        pause meanwhile, so the listing pace is the host's whole load."""
        holder = locks.held_by(CENTANET_LOCK)
        if holder is not None:
            note = f"skipped: another Centanet listing job (process {holder}) is running"
            self.log(f"centanet: {note}")
            for g in groups:
                self.results.append(SourceResult("centanet", g, complete=False, note=note))
            return
        with locks.hold(CENTANET_LOCK):
            self._centanet(groups)

    def _centanet(self, groups: list[str]) -> None:
        # every pilot estate name is recognised, so the territory never takes a pilot listing
        prefixes = {name: g for g, t in PILOT.items() for name in t.centanet}
        territory = TERRITORY in groups
        try:
            xml = self.centanet_fetcher.get(centanet.SITEMAP_URL, "listing")
        except (FetchError, SourceBlocked, RobotsDisallowed, httpx.TransportError) as e:
            self.log(f"centanet: sitemap failed ({type(e).__name__}: {e}); source skipped")
            for g in groups:
                self.results.append(SourceResult("centanet", g, complete=False, note=str(e)))
            return
        entries = centanet.sitemap_entries(xml, prefixes,
                                           TERRITORY if territory else None)
        known = self._known("centanet")
        blocked = False
        breaker = _Breaker()
        patience = self._patience("centanet", self.centanet_fetcher)
        for g in groups:
            res = SourceResult("centanet", g)
            mine = [e for e in entries if e.group == g]  # (entries of unrequested groups: skipped)
            new = [e for e in mine if e.listing_id not in known or known[e.listing_id][0] is None]
            new_ids = {e.listing_id for e in new}
            old = sorted(
                (e for e in mine if e.listing_id not in new_ids),
                key=lambda e: known[e.listing_id][0],
            )
            quota = math.ceil(len(old) / ROTATE_DAYS)
            # pages read earlier today are not read again by a second run
            old = [e for e in old if known[e.listing_id][0].date() < self.now.date()]
            stale = [e for e in old[quota:]
                     if (self.now - known[e.listing_id][0]).days >= ROTATE_DAYS]
            todo = new + old[:quota] + stale
            due = len(todo)
            if g == TERRITORY and self.centanet_budget is not None:
                todo = todo[: self.centanet_budget]  # new first, then oldest-read; rest later
            n_new = min(len(new), len(todo))  # new listings come first in todo
            cut = f" ({due - len(todo)} left for later runs)" if due > len(todo) else ""
            self.log(f"centanet {g}: {len(mine)} live, {len(new)} new; reading {n_new} new "
                     f"+ {len(todo) - n_new} re-read{cut}")
            batch: list[Listing] = []
            gone: set[str] = set()
            for i, e in enumerate(todo, 1):
                if blocked:
                    res.complete = False
                    break
                while True:
                    try:
                        lst = centanet.parse_detail(
                            self.centanet_fetcher.get(e.url, "listing"), e.url)
                        lst.kind = e.kind
                        batch.append(lst)
                        res.detail_read.add(e.listing_id)
                        breaker.ok()
                        patience.ok()
                    except (FetchError, httpx.TransportError) as ex:
                        if patience_kind(ex) is not None:
                            action = patience.on_error(ex)
                            if action == RETRY:
                                continue  # same page again, after the cool-down
                            res.errors += 1
                            self.log(f"  centanet {e.listing_id}: {type(ex).__name__} {ex}")
                            if action == STOP:
                                res.note = f"stopped for the day: {patience.note}"
                                res.complete = False
                                self.log(f"centanet {res.note}")
                                blocked = True
                        elif ex.status in (404, 410):
                            gone.add(e.listing_id)
                            breaker.ok()  # the site answered
                            patience.ok()
                        else:
                            res.errors += 1
                            self.log(f"  centanet {e.listing_id}: {ex}")
                            if breaker.fail():
                                res.note = breaker.message
                                self.log(f"centanet stopped for this run: {breaker.message}")
                                blocked = True
                    except RobotsDisallowed as ex:
                        res.errors += 1
                        self.log(f"  centanet {e.listing_id}: {type(ex).__name__}")
                    except SourceBlocked as ex:
                        self.log(f"centanet blocked: {ex}")
                        blocked = True
                    break
                res.pages += 1
                if len(batch) >= 25:
                    self._store(g, batch, "detail")
                    res.parsed += batch
                    batch = []
                if i % 200 == 0:
                    self.log(f"  centanet {g}: {i}/{len(todo)}")
            self._store(g, batch, "detail")
            res.parsed += batch
            # Seen = in today's sitemap, not gone, and read at least once (a new listing whose
            # page failed today has no data yet; it is picked up tomorrow).
            stored = self._stored_ids("centanet", [e.listing_id for e in mine])
            res.seen = [e.listing_id for e in mine
                        if e.listing_id not in gone and e.listing_id in stored]
            if res.pages and res.errors / res.pages > MAX_ERROR_SHARE:
                res.complete = False
            self.results.append(res)

    def _stored_ids(self, source: str, ids: list[str]) -> set[str]:
        with db.session(self.db_path) as con:
            return {
                r[0] for r in con.execute(
                    "SELECT DISTINCT listing_id FROM listing_raw "
                    "WHERE source = ? AND listing_id IN (SELECT unnest(?))",
                    [source, ids],
                ).fetchall()
            }

    # ---- 28Hse ------------------------------------------------------------------------------

    def hse28(self, groups: list[str]) -> None:
        if TERRITORY in groups:
            self.log("hse28: territory listings are not read yet (pilot estates only)")
            groups = [g for g in groups if g in PILOT]
        if not groups:
            return
        holder = locks.held_by("hse28")
        if holder is not None:
            note = f"skipped: another 28Hse job (process {holder}, e.g. a backfill) is running"
            self.log(f"hse28: {note}")
            for g in groups:
                self.results.append(SourceResult("hse28", g, complete=False, note=note))
            return
        with locks.hold("hse28"):
            known = self._known("hse28")
            for g in groups:
                res = SourceResult("hse28", g)
                try:
                    self._hse28_group(g, res, known)
                except SourceBlocked as e:
                    res.complete = False
                    res.note = f"blocked: {e}"
                    self.log(f"hse28 blocked: {e}")
                    self.results.append(res)
                    break
                self.results.append(res)

    def _hse28_group(self, g: str, res: SourceResult, known: dict) -> None:
        f = self.hse28_fetcher
        seen: dict[str, Listing] = {}
        for area, eid in PILOT[g].hse28:
            for kind in ("sale", "rent"):
                url = hse28_listings.estate_search_url(area, eid, kind)
                ids_pages: set[tuple] = set()
                for _ in range(PAGE_CAP):
                    try:
                        page = hse28_listings.parse_search_page(f.get(url, "listing"), kind)
                    except (FetchError, RobotsDisallowed, httpx.TransportError) as e:
                        res.errors += 1
                        res.complete = False
                        self.log(f"  hse28 {url}: {e}")
                        break
                    res.pages += 1
                    ids = tuple(x.listing_id for x in page.listings)
                    if not ids or ids in ids_pages:
                        break
                    ids_pages.add(ids)
                    for x in page.listings:
                        if x.source_estate_key and x.source_estate_key != str(eid):
                            continue  # a promoted listing from another estate
                        seen[x.listing_id] = x
                    if not page.next_url:
                        break
                    url = page.next_url
        items = list(seen.values())
        self._store(g, items, "search")
        details, pending = [], []
        breaker = _Breaker()
        patience = self._patience("hse28", f)
        for x in items:
            k = known.get(x.listing_id)
            if k is not None and k[1] == x.price_hkd:
                continue  # known listing, same price: the search card is enough
            d = None
            while True:
                try:
                    d = hse28_listings.parse_detail(f.get(x.url, "listing"), x.url, x.kind)
                except (FetchError, RobotsDisallowed, httpx.TransportError) as e:
                    action = patience.on_error(e) if patience_kind(e) is not None else None
                    if action == RETRY:
                        continue
                    res.errors += 1
                    self.log(f"  hse28 detail {x.listing_id}: {e}")
                    if action == STOP:
                        self._store(g, pending, "detail")
                        raise SourceBlocked(f"stopped for the day: {patience.note}") from e
                    if (action is None and isinstance(e, FetchError)
                            and e.status not in (404, 410) and breaker.fail()):
                        self._store(g, pending, "detail")
                        raise SourceBlocked(breaker.message) from e
                break
            if d is None:
                continue
            breaker.ok()
            patience.ok()
            d.estate_name = d.estate_name or x.estate_name
            d.source_estate_key = x.source_estate_key
            d.text_for_flags = f"{d.text_for_flags} {x.text_for_flags}"
            details.append(d)
            pending.append(d)
            res.detail_read.add(x.listing_id)
            if len(pending) >= 25:
                self._store(g, pending, "detail")
                pending = []
        self._store(g, pending, "detail")
        res.pages += len(details)
        by_id = {d.listing_id: d for d in details}
        res.parsed = [by_id.get(x.listing_id, x) for x in items]
        res.seen = [x.listing_id for x in items]
        self.log(f"hse28 {g}: {len(items)} listings, {len(details)} detail pages")

    # ---- Spacious --------------------------------------------------------------------------

    def spacious(self, groups: list[str]) -> None:
        for g in sorted(groups, key=lambda x: x == TERRITORY):  # pilot first, territory last
            if g == TERRITORY:
                self._spacious_territory()
                continue
            res = SourceResult("spacious", g)
            seen: dict[str, Listing] = {}
            try:
                for url, names in PILOT[g].spacious:
                    page_ids: set[tuple] = set()
                    for _ in range(PAGE_CAP):
                        try:
                            page = spacious.parse_search_page(
                                self.fetcher.get(url, "listing"), "sale", url)
                        except (FetchError, RobotsDisallowed, httpx.TransportError) as e:
                            res.errors += 1
                            res.complete = False
                            self.log(f"  spacious {url}: {e}")
                            break
                        res.pages += 1
                        ids = tuple(x.listing_id for x in page.listings)
                        if not ids or ids in page_ids:
                            break
                        page_ids.add(ids)
                        for x in page.listings:
                            text = match.norm_key(x.building_text)
                            if not names or any(match.norm_key(n) in text for n in names):
                                seen[x.listing_id] = x
                        if not page.next_url:
                            break
                        url = page.next_url
            except SourceBlocked as e:
                res.complete = False
                res.note = f"blocked: {e}"
                self.log(f"spacious blocked: {e}")
            items = list(seen.values())
            self._store(g, items, "search")
            res.parsed = items
            res.seen = [x.listing_id for x in items]
            self.log(f"spacious {g}: {len(items)} listings from {res.pages} pages")
            self.results.append(res)

    def _spacious_quota(self, n_buildings: int) -> int:
        """Building pages this run: the --spacious-budget if given, else a seventh of all
        buildings (every building read about weekly). A full pass of ~33,500 pages at ~1 s
        plus response time is ~27 h, longer than a day."""
        if self.spacious_budget is not None:
            return self.spacious_budget
        return math.ceil(n_buildings / ROTATE_DAYS)

    def _spacious_territory(self) -> None:
        """Building sale pages (a neighbourhood page shows only its first ~14 listings).
        Budgeted per run: buildings that had listings first, then never-read ones."""
        res = SourceResult("spacious", TERRITORY, complete=False)  # own delisting rule
        today = self.today
        with db.session(self.db_path) as con:
            spacious.ensure_tables(con)
            n = con.execute("SELECT count(*) FROM spacious_building").fetchone()[0]
            plan = spacious.plan_reads(con, self._spacious_quota(n)) if n else []
            claimed = {r[0] for r in con.execute(
                "SELECT listing_id FROM listing WHERE source = 'spacious' AND "
                "group_name <> ?", [TERRITORY]).fetchall()}
        if not n:
            try:
                pages = self._spacious_building_index()
            except (FetchError, SourceBlocked, RobotsDisallowed, httpx.TransportError) as e:
                self.log(f"spacious: building index failed ({type(e).__name__}: {e})")
                self.results.append(res)
                return
            with db.session(self.db_path) as con:
                added = spacious.add_buildings(con, pages, today)
                plan = spacious.plan_reads(con, self._spacious_quota(added))
            self.log(f"spacious: {added} buildings in the sitemap")
        pilot_seen = {i for r in self.results if r.source == "spacious" for i in r.seen}
        seen: dict[str, Listing] = {}
        breaker = _Breaker()
        patience = self._patience("spacious", self.fetcher)
        stop = False
        for k, (bid, first_url) in enumerate(plan, 1):
            failed = False
            while True:
                url = first_url
                try:
                    count = 0
                    for _ in range(3):
                        page = spacious.parse_search_page(self.fetcher.get(url, "listing"),
                                                          "sale", url)
                        mine = [x for x in page.listings if x.source_building_key == str(bid)]
                        new = [x for x in mine if x.listing_id not in seen]
                        for x in mine:
                            if x.listing_id not in claimed and x.listing_id not in pilot_seen:
                                seen[x.listing_id] = x
                        count += len(new)
                        res.pages += 1
                        if not page.next_url or not new:
                            break
                        url = page.next_url
                except SourceBlocked as e:
                    res.note = f"blocked: {e}"
                    self.log(f"spacious blocked: {e}")
                    stop = True
                except (FetchError, RobotsDisallowed, httpx.TransportError) as e:
                    action = patience.on_error(e) if patience_kind(e) is not None else None
                    if action == RETRY:
                        continue
                    failed = True
                    res.errors += 1
                    self.log(f"  spacious building {bid}: {e}")
                    if action == STOP:
                        res.note = f"stopped for the day: {patience.note}"
                        self.log(f"spacious {res.note}")
                        stop = True
                    elif (action is None and isinstance(e, FetchError)
                          and e.status not in (404, 410) and breaker.fail()):
                        res.note = breaker.message
                        self.log(f"spacious stopped for this run: {breaker.message}")
                        stop = True
                break
            if stop:
                break
            if failed:
                continue
            breaker.ok()
            patience.ok()
            with db.session(self.db_path) as con:
                spacious.record_read(con, bid, today, count)
            if k % 200 == 0:
                self.log(f"  spacious territory: {k}/{len(plan)} buildings, {len(seen)} listings")
        items = list(seen.values())
        for i in range(0, len(items), 500):
            self._store(TERRITORY, items[i:i + 500], "search")
        res.parsed = items
        res.seen = [x.listing_id for x in items]
        self.log(f"spacious territory: {len(items)} listings from {res.pages} pages")
        self.results.append(res)

    def _spacious_building_index(self) -> list:
        f = self.fetcher
        pages = []
        for child in spacious.sitemap_children(spacious.gunzip_text(
                f.get_bytes(spacious.SITEMAP_INDEX, "static"))):
            pages += spacious.building_sale_pages(
                spacious.gunzip_text(f.get_bytes(child, "static")))
        return pages

    # ---- bookkeeping -------------------------------------------------------------------------

    def finish(self) -> dict:
        """Record what each source saw, delist, match, dedupe, link relistings, flag."""
        summary: dict = {"sources": {}, "match": {}}
        for res in self.results:
            if res.group == TERRITORY:
                self._finish_territory(res, summary)
        with db.session(self.db_path) as con:
            for res in self.results:
                if res.group == TERRITORY:
                    continue
                latest = listings.latest_rows(con, res.source, res.seen)
                items = [
                    {"listing_id": lid, "kind": r.get("kind"), "url": r.get("url"),
                     "price_hkd": r.get("price_hkd"), "saleable_sqft": r.get("saleable_sqft"),
                     "flat": r.get("flat"),
                     "building_text": match_text(r.get("estate_name"), r.get("building_text"))}
                    for lid, r in latest.items()
                ]
                counts = listings.record_seen(con, res.source, res.group, items, self.today,
                                              complete=res.complete)
                delisted = (listings.mark_delisted(con, res.source, res.group, self.today)
                            if res.complete else 0)
                if res.detail_read:
                    con.execute(
                        "UPDATE listing SET last_detail_at = ? WHERE source = ? "
                        "AND listing_id IN (SELECT unnest(?))",
                        [self.now, res.source, sorted(res.detail_read)],
                    )
                n_flags = flags.apply_flags(con, res.parsed, self.now)
                summary["sources"].setdefault(res.source, {})[res.group] = {
                    **counts, "delisted": delisted, "pages": res.pages, "errors": res.errors,
                    "complete": res.complete, "flags": n_flags, "note": res.note,
                }
            groups = sorted({r.group for r in self.results if r.group != TERRITORY})
            summary["match"] = match_listings(con, groups, self.today)
        if any(r.group == TERRITORY for r in self.results):
            summary["match"][TERRITORY] = match_territory(self.db_path, self.today, self.log)
        return summary

    def _finish_territory(self, res: SourceResult, summary: dict) -> None:
        """Like `finish` for one source, but in short database sessions of 1,000 listings
        (a territory is ~60,000 listings; one long session would lock out the other jobs)."""
        counts = {"new": 0, "seen": 0, "price_change": 0, "relisted": 0}
        for i in range(0, len(res.seen), 1000):
            ids = res.seen[i:i + 1000]
            with db.session(self.db_path) as con:
                latest = listings.latest_rows(con, res.source, ids)
                items = [
                    {"listing_id": lid, "kind": r.get("kind"), "url": r.get("url"),
                     "price_hkd": r.get("price_hkd"), "saleable_sqft": r.get("saleable_sqft"),
                     "flat": r.get("flat"),
                     "building_text": match_text(r.get("estate_name"), r.get("building_text"))}
                    for lid, r in latest.items()
                ]
                for k, v in listings.record_seen(con, res.source, res.group, items, self.today,
                                                 complete=False).items():
                    counts[k] += v
        delisted = 0
        with db.session(self.db_path) as con:
            if res.source == "spacious":
                delisted = spacious.mark_delisted(con, self.today)
            elif res.complete:
                con.execute("INSERT OR IGNORE INTO listing_run_day VALUES (?, ?, ?)",
                            [res.source, res.group, self.today])
                delisted = listings.mark_delisted(con, res.source, res.group, self.today)
        read = sorted(res.detail_read)
        for i in range(0, len(read), 1000):
            with db.session(self.db_path) as con:
                con.execute("UPDATE listing SET last_detail_at = ? WHERE source = ? "
                            "AND listing_id IN (SELECT unnest(?))",
                            [self.now, res.source, read[i:i + 1000]])
        n_flags = 0
        for i in range(0, len(res.parsed), 500):
            with db.session(self.db_path) as con:
                n_flags += flags.apply_flags(con, res.parsed[i:i + 500], self.now)
        summary["sources"].setdefault(res.source, {})[res.group] = {
            **counts, "delisted": delisted, "pages": res.pages, "errors": res.errors,
            "complete": res.complete, "flags": n_flags, "note": res.note,
        }


def match_listings(con, groups: list[str], today: date) -> dict:
    """Match every listing of the groups to a building and floor band; group cross-site
    duplicates; link same-flat relistings; copy the result onto today's snapshots."""
    manual = match.load_manual_map()
    out = {}
    for g in groups:
        buildings = match.load_buildings(con, g)
        top = {b.building_id: b.top_floor for b in buildings}
        rows = con.execute(
            """
            SELECT l.source, l.listing_id, l.kind, l.building_text, l.saleable_sqft,
                   r.floor_text, r.floor_range
            FROM listing l LEFT JOIN (
                SELECT source, listing_id, arg_max(floor_text, fetched_at) AS floor_text,
                       arg_max(floor_range, fetched_at) AS floor_range
                FROM listing_raw GROUP BY ALL) r USING (source, listing_id)
            WHERE l.group_name = ?
            """,
            [g],
        ).fetchall()
        updates, dd = [], []
        methods: dict[str, int] = {}
        for src, lid, kind, text, area, floor_text, floor_range in rows:
            bid, how = match.match_building(text, buildings, source=src, manual=manual)
            band = match.floor_band(floor_text, top.get(bid), floor_range=floor_range)
            updates.append([bid, how, band, src, lid])
            dd.append({"source": src, "listing_id": lid, "kind": kind, "building_id": bid,
                       "floor_band": band, "saleable_sqft": area})
            methods[how] = methods.get(how, 0) + 1
        if updates:
            con.executemany(
                "UPDATE listing SET building_id = ?, match_method = ?, floor_band = ? "
                "WHERE source = ? AND listing_id = ?",
                updates,
            )
        groups_of = match.dedupe(dd)
        if groups_of:
            con.executemany(
                "UPDATE listing SET dup_group = ? WHERE source = ? AND listing_id = ?",
                [[k, s, lid] for (s, lid), k in groups_of.items()],
            )
        matched = sum(1 for u in updates if u[0])
        out[g] = {"listings": len(rows), "matched": matched, "methods": methods,
                  "dup_groups": len(set(groups_of.values()))}
    for src in ("centanet", "hse28", "spacious"):
        listings.relink_relistings(con, src, today)
    con.execute(
        """
        UPDATE listing_snapshot s SET building_id = l.building_id, floor_band = l.floor_band
        FROM listing l
        WHERE s.source = l.source AND s.listing_id = l.listing_id AND s.snapshot_date = ?
        """,
        [today],
    )
    return out


def _run_sources(run: ListingRun, groups: list[str], sources: list[str],
                 parallel: bool) -> None:
    """Each source in its own thread (they are different hosts, each with its own fetcher
    and pace), so a long Centanet pass doesn't hold up 28Hse and Spacious. Returns when all
    are done; the first error any of them raised (including Ctrl+C) is raised then."""
    if not parallel or len(sources) < 2:
        for s in sources:
            getattr(run, s)(groups)
        return
    errors: list[BaseException] = []

    def work(s: str) -> None:
        try:
            getattr(run, s)(groups)
        except BaseException as e:  # noqa: BLE001 - handed to the main thread
            run.log(f"{s}: stopped by {type(e).__name__}: {e}")
            errors.append(e)

    threads = [threading.Thread(target=work, args=(s,), name=f"listings-{s}", daemon=True)
               for s in sources]
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():
            t.join(1.0)  # a short join, so Ctrl+C still reaches the main thread
    if errors:
        raise errors[0]


def run_listings(groups: list[str], sources: list[str], *, db_path=None, log: Log = print,
                 parallel: bool = True, **kw) -> dict:
    run = ListingRun(db_path=db_path, log=log, **kw)
    t0 = time.monotonic()
    try:
        _run_sources(run, groups, sources, parallel)
        summary = run.finish()  # after every source: delisting, matching, flags
    finally:
        run.close()
    summary["elapsed_s"] = round(time.monotonic() - t0)
    return summary


def match_territory(db_path, today: date, log: Log = print) -> dict:
    """Match active territory listings that have no building yet (new ones, and earlier misses now
    that the master list has grown), re-check those matched loosely ('contains', 'single')
    so a match the current rules reject is dropped ('manual' and 'exact' stay), group
    cross-site duplicates, link relistings. Short sessions of 1,000 listings."""
    with db.session(db_path) as con:
        matcher = match.TerritoryMatcher(con)
        manual = match.load_manual_map()
        rows = con.execute(
            """
            SELECT l.source, l.listing_id, l.building_text, r.estate_name, r.source_building_key,
                   r.floor_text, r.floor_range, l.building_id, l.url
            FROM listing l LEFT JOIN (
                SELECT source, listing_id, arg_max(estate_name, fetched_at) AS estate_name,
                       arg_max(source_building_key, fetched_at) AS source_building_key,
                       arg_max(floor_text, fetched_at) AS floor_text,
                       arg_max(floor_range, fetched_at) AS floor_range
                FROM listing_raw GROUP BY source, listing_id) r USING (source, listing_id)
            WHERE l.group_name = ? AND l.status = 'active'
              AND (l.building_id IS NULL OR l.match_method IN ('contains', 'single'))
            """, [TERRITORY]).fetchall()
    methods: dict[str, int] = {}
    matched = 0
    for i in range(0, len(rows), 1000):
        updates = []
        with db.session(db_path) as con:
            matcher.con = con
            for src, lid, text, estate, bkey, floor_text, floor_range, old_bid, url in \
                    rows[i:i + 1000]:
                bid, how = matcher.match(text, block_key=bkey, estate=estate, source=src,
                                         manual=manual, url=url)
                if old_bid is not None and bid == old_bid:
                    continue  # a loose match the current rules still accept: leave it
                band = match.floor_band(floor_text, matcher.top_floor(bid),
                                        floor_range=floor_range)
                updates.append([bid, how, band, src, lid])
                methods[how] = methods.get(how, 0) + 1
                matched += bid is not None and old_bid is None
            if updates:  # executemany rejects an empty list
                con.executemany("UPDATE listing SET building_id = ?, match_method = ?, "
                                "floor_band = ? WHERE source = ? AND listing_id = ?", updates)
    with db.session(db_path) as con:
        total = con.execute("SELECT count(*), count(building_id) FROM listing "
                            "WHERE group_name = ?", [TERRITORY]).fetchone()
        dd = [{"source": s, "listing_id": lid, "kind": k, "building_id": b, "floor_band": fb,
               "saleable_sqft": a}
              for s, lid, k, b, fb, a in con.execute(
                  "SELECT source, listing_id, kind, building_id, floor_band, saleable_sqft "
                  "FROM listing WHERE group_name = ? AND status = 'active' "
                  "AND building_id IS NOT NULL", [TERRITORY]).fetchall()]
    groups_of = match.dedupe(dd)
    items = list(groups_of.items())
    for i in range(0, len(items), 2000):
        with db.session(db_path) as con:
            con.executemany("UPDATE listing SET dup_group = ? WHERE source = ? AND "
                            "listing_id = ?", [[k, s, lid] for (s, lid), k in items[i:i + 2000]])
    with db.session(db_path) as con:
        for src in ("centanet", "spacious"):
            listings.relink_relistings(con, src, today)
        con.execute(
            "UPDATE listing_snapshot s SET building_id = l.building_id, floor_band = l.floor_band "
            "FROM listing l WHERE s.source = l.source AND s.listing_id = l.listing_id "
            "AND s.snapshot_date = ? AND l.group_name = ?", [today, TERRITORY])
    log(f"territory match: {matched}/{len(rows)} newly matched; "
        f"{total[1]}/{total[0]} all; {methods}")
    return {"listings": total[0], "matched": total[1], "methods": methods,
            "dup_groups": len(set(groups_of.values()))}
