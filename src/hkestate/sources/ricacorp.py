"""Ricacorp (www.ricacorp.com) estate transaction pages: previous-deal pairs (Task 3.6).

Each estate page (`/zh-hk/property/transaction/estate/<name>-<type>-<district>-hma-hk`,
type `bigest`, `estate` or `single`) lists the estate's latest resales of the last 90 days:
address (estate, phase, block, floor, flat), price, saleable area, registration date, the
**days the previous owner held the flat** and the **% gain** against the previous deal. From
the last two we get the previous deal's date (registration date minus days held) and price
(price / (1 + gain)): a pre-2018 data point for the flat. Gain is a whole percent, so the
previous price is only good to about 0.5-1%.

Only the estate pages are read, never `/api/` (robots.txt disallows it; the fetcher enforces
robots.txt and `estate_pages` only yields estate paths). The page is 1 MB of Angular markup;
only the deal table cells are read, and the cached copy is deleted right after parsing. Deal
detail pages show agents' names and are never fetched. Pages are untrusted data.

Stored in `ricacorp_deal` (parsed fields only). `place_deals` then turns placed deals into
`txn_raw` rows (source `ricacorp`) and `deal_pair` links, skipping any sale that 28Hse or
Centadata already hold (same unit, price within 1%, date within 90 days).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import unquote

from selectolax.lexbor import LexborHTMLParser

from hkestate import db, link
from hkestate.backfill import match_buildings
from hkestate.fetch import FetchError, SourceBlocked
from hkestate.link import est_key, match_estate
from hkestate.normalise import norm_name, parse_area, parse_floor, parse_price_hkd

SOURCE = "ricacorp"
COMMAND = "ricacorp"
BASE = "https://www.ricacorp.com"
SITEMAP_URL = f"{BASE}/zh-hk/property/sitemaps/transaction/sitemap-0.xml"
ESTATE_PATH = "/zh-hk/property/transaction/estate/"
MIN_INTERVAL_S = 1.5
MAX_CONSECUTIVE_ERRORS = 8
DUP_PRICE_TOL = db.DUP_PRICE_TOL
DUP_DAYS = db.DUP_DAYS

DDL = [
    """
    CREATE TABLE IF NOT EXISTS ricacorp_deal (
        deal_id      VARCHAR PRIMARY KEY,   -- from the deal link, else a hash of the row
        estate_slug  VARCHAR,
        estate_name  VARCHAR,
        address      VARCHAR,               -- estate phase block (座) floor flat; no personal data
        floor_label  VARCHAR,
        flat         VARCHAR,
        price_hkd    BIGINT,
        area_sqft    DOUBLE,
        reg_date     DATE,
        hold_days    INTEGER,               -- previous owner's holding period
        gain_pct     DOUBLE,                -- signed % against the previous deal
        source_tag   VARCHAR,               -- registry or market
        first_seen   DATE,
        last_seen    DATE,
        building_id  VARCHAR,               -- master building, NULL until placed
        placed_at    TIMESTAMP,
        emitted      BOOLEAN DEFAULT false  -- txn_raw rows and pair written
    )""",
    """
    CREATE TABLE IF NOT EXISTS deal_pair (
        cur_source      VARCHAR NOT NULL,
        cur_id          VARCHAR NOT NULL,
        prev_source     VARCHAR NOT NULL,
        prev_id         VARCHAR NOT NULL,
        building_id     VARCHAR,
        unit_id         VARCHAR,
        cur_date        DATE,
        cur_price_hkd   BIGINT,
        prev_date       DATE,
        prev_price_hkd  BIGINT,             -- exact when it matched a stored deal, else derived
        hold_days       INTEGER,
        gain_pct        DOUBLE,
        basis           VARCHAR,            -- ricacorp_hold
        prev_derived    BOOLEAN,            -- true: price from the % gain (about +-0.5-1%)
        created_at      TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (cur_source, cur_id)
    )""",
]


def ensure_tables(con) -> None:
    link.ensure_tables(con)  # master_estate / master_building
    for ddl in DDL:
        con.execute(ddl)


# ---- sitemap --------------------------------------------------


@dataclass(frozen=True)
class EstatePage:
    url: str
    slug: str  # last path segment, decoded: <name>-<type>-<district>-hma-hk
    name: str
    kind: str  # bigest, estate, single


_SLUG = re.compile(r"^(.*?)-(bigest|estate|single)-(.+)-hma-hk$")


def estate_pages(xml: str) -> list[EstatePage]:
    """Estate pages in the transaction sitemap. Anything else (building pages, lists, and
    above all any `/api/` address) is ignored."""
    out, seen = [], set()
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
        if not loc.startswith(BASE + ESTATE_PATH) or "/api/" in loc:
            continue
        slug = unquote(loc.rsplit("/", 1)[-1])
        m = _SLUG.match(slug)
        if m and loc not in seen:
            seen.add(loc)
            out.append(EstatePage(loc, slug, m.group(1), m.group(2)))
    return out


# ---- the page --------------------------------------------------


@dataclass
class RicaDeal:
    deal_id: str
    address: str
    price_hkd: int | None
    area_sqft: float | None
    reg_date: date | None
    hold_days: int | None
    gain_pct: float | None
    source_tag: str  # registry, market


_DEAL_ID = re.compile(r"-(\d{10,})-sale-hk\b")
_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")


def _cell(row, name: str) -> str:
    node = row.css_first(f"mat-cell.cdk-column-{name}")
    return re.sub(r"\s+", " ", node.text()).strip() if node is not None else ""


def parse_gain(text: str) -> float | None:
    """'arrow_upward 169%' -> 169.0, 'arrow_downward 23%' -> -23.0, '-' -> None."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if not m:
        return None
    value = float(m.group(1))
    if "downward" in text:
        return -value
    return value if "upward" in text or value == 0 else None


def parse_estate_page(html: str, slug: str = "") -> list[RicaDeal]:
    """The deal rows of an estate page (empty for an estate with no deal in 90 days)."""
    tree = LexborHTMLParser(html)
    table = tree.css_first("mat-table")
    deals: list[RicaDeal] = []
    if table is None:
        return deals
    for row in table.css("mat-row"):
        address = _cell(row, "position")
        if not address:
            continue
        m = _DATE.search(_cell(row, "weight"))
        reg = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
        price = parse_price_hkd(_cell(row, "symbol"))
        link = row.css_first("a")
        mid = _DEAL_ID.search(link.attributes.get("href") or "") if link is not None else None
        deal_id = mid.group(1) if mid else hashlib.sha1(
            f"{slug}|{address}|{reg}|{price}".encode()).hexdigest()[:16]
        hold = re.search(r"(\d+)\s*天", _cell(row, "holdDay"))
        tag = _cell(row, "tag")
        deals.append(RicaDeal(
            deal_id=deal_id,
            address=address,
            price_hkd=price,
            area_sqft=parse_area(_cell(row, "name"))[1],
            reg_date=reg,
            hold_days=int(hold.group(1)) if hold else None,
            gain_pct=parse_gain(_cell(row, "returnRate")),
            source_tag="registry" if "註冊處" in tag else "market",
        ))
    return deals


def split_address(address: str, estate_name: str) -> tuple[str, str, str]:
    """'測試苑 一期 甲閣 (1座) 19樓 A室' -> ('一期 甲閣 (1座)', '19樓', 'A'). The estate
    name is dropped when it leads; the last two tokens are floor and flat."""
    t = re.sub(r"\s+", " ", address).strip()
    if estate_name and t.startswith(estate_name + " "):
        t = t[len(estate_name) + 1:]
    tokens = t.split(" ")
    flat = tokens[-1] if tokens else ""
    flat = flat[:-1] if flat.endswith("室") else flat
    floor = tokens[-2] if len(tokens) >= 2 else ""
    return " ".join(tokens[:-2]), floor, flat


def previous_deal(d: RicaDeal) -> tuple[date, int] | None:
    """(date, price) of the flat's previous deal from days held and % gain."""
    if d.reg_date is None or not d.price_hkd or d.hold_days is None or d.gain_pct is None:
        return None
    if d.gain_pct <= -100:
        return None
    return (d.reg_date - timedelta(days=d.hold_days),
            round(d.price_hkd / (1 + d.gain_pct / 100)))


# ---- storing the parsed rows --------------------------------------------------


def save_deals(con, page: EstatePage, deals: list[RicaDeal], today: date) -> int:
    """Upsert the parsed rows (keeping first_seen and any placement). Returns new deals."""
    new = 0
    for d in deals:
        _, floor_text, flat = split_address(d.address, page.name)
        floor = parse_floor(floor_text).label if floor_text else None
        exists = con.execute("SELECT 1 FROM ricacorp_deal WHERE deal_id = ?",
                             [d.deal_id]).fetchone()
        if exists:
            con.execute("UPDATE ricacorp_deal SET last_seen = ? WHERE deal_id = ?",
                        [today, d.deal_id])
            continue
        con.execute(
            "INSERT INTO ricacorp_deal (deal_id, estate_slug, estate_name, address, floor_label,"
            " flat, price_hkd, area_sqft, reg_date, hold_days, gain_pct, source_tag, first_seen,"
            " last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [d.deal_id, page.slug, norm_name(page.name), d.address, floor, flat or None,
             d.price_hkd, d.area_sqft, d.reg_date, d.hold_days, d.gain_pct, d.source_tag,
             today, today])
        new += 1
    return new


# ---- placing deals in buildings --------------------------------------------------

PILOT_ALIAS = {"太古城": "taikoo-shing", "美孚新邨": "mei-foo", "沙田第一城": "city-one",
               "日出康城": "lohas-park", "高雲臺": "goldwin-heights"}


class Resolver:
    """Finds the building dict (id -> (phase text, block)) for a Ricacorp estate name."""

    def __init__(self, con):
        masters = [
            {"code": c, "parent_code": p, "key": est_key(n)}
            for c, p, n in con.execute(
                "SELECT code, parent_code, name_zh FROM master_estate "
                "WHERE NOT coalesce(pilot, false) AND NOT coalesce(excluded, false)").fetchall()
            if est_key(n)
        ]
        masters.sort(key=lambda m: m["key"])
        self.masters = masters
        self.keys = [m["key"] for m in masters]
        self.con = con
        self.pilot = {est_key(k): v for k, v in PILOT_ALIAS.items()}

    def buildings(self, name: str) -> dict[str, tuple[str, str]]:
        from hkestate.sources.centadata import group_buildings

        key = est_key(name)
        if key in self.pilot:
            return group_buildings(self.con, self.pilot[key])
        status, hits = match_estate(key, self.masters, self.keys)
        if not hits:
            return {}
        marks = ",".join("?" for _ in hits)
        rows = self.con.execute(
            f"SELECT building_id, estate_name || ' ' || coalesce(phase_name, ''), "
            f"coalesce(block_name, '') FROM master_building WHERE estate_code IN ({marks})",
            [m["code"] for m in hits]).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}


def place_deals(con, today: date | None = None, log=print) -> dict:
    """Place unplaced deals in a building, then write `txn_raw` rows and `deal_pair` links
    for placed ones not yet written. Safe to run again at any time; DB only (no requests)."""
    ensure_tables(con)
    stats = {"placed": 0, "unplaced": 0, "current_added": 0, "current_existing": 0,
             "prev_added": 0, "prev_existing": 0, "pairs": 0}
    resolver = Resolver(con)
    groups = con.execute(
        "SELECT estate_slug, any_value(estate_name) FROM ricacorp_deal "
        "WHERE NOT emitted GROUP BY 1").fetchall()
    for slug, name in groups:
        buildings = resolver.buildings(name)
        rows = con.execute(
            "SELECT deal_id, address, floor_label, flat, price_hkd, area_sqft, reg_date, "
            "hold_days, gain_pct, source_tag, building_id FROM ricacorp_deal "
            "WHERE estate_slug = ? AND NOT emitted", [slug]).fetchall()
        todo = []
        for r in rows:
            bid = r[10]
            if bid is None and buildings and r[2] and r[3]:
                loc, _, _ = split_address(r[1], name)
                bid = _pick(loc, buildings)
                if bid:
                    con.execute("UPDATE ricacorp_deal SET building_id = ?, placed_at = now() "
                                "WHERE deal_id = ?", [bid, r[0]])
                    stats["placed"] += 1
            if bid is None:
                stats["unplaced"] += 1
            else:
                todo.append((r, bid))
        if todo:
            _emit(con, slug, name, todo, stats)
    log(f"ricacorp placement: {stats}")
    return stats


def _pick(location: str, buildings: dict) -> str | None:
    ids = match_buildings(location, buildings)
    return ids[0] if len(ids) == 1 else None


def _near(price: int | None, day: date | None, rows: list) -> tuple | None:
    """The stored deal (source, id, date, price) that is the same sale: price within 1% and
    date within 90 days."""
    if not price or day is None:
        return None
    best = None
    for src, sid, d, p in rows:
        if not p or d is None or abs(p - price) > DUP_PRICE_TOL * p:
            continue
        gap = abs((d - day).days)
        if gap <= DUP_DAYS and (best is None or gap < best[0]):
            best = (gap, src, sid, d, p)
    return best[1:] if best else None


def _emit(con, slug: str, name: str, todo: list, stats: dict) -> None:
    bids = sorted({b for _, b in todo})
    # 28Hse buildings that map to these master buildings hold deals under their own ids
    marks = ",".join("?" for _ in bids)
    related = bids + [r[0] for r in con.execute(
        f"SELECT source_key FROM source_map WHERE source = 'hse28' AND kind = 'building' "
        f"AND master_id IN ({marks})", bids).fetchall()]
    marks = ",".join("?" for _ in related)
    latest: dict[tuple, tuple] = {}
    for src, sid, fetched, floor, flat, day, price in con.execute(
        f"SELECT source, source_id, fetched_at, floor_label, flat, deal_date, price_hkd "
        f"FROM txn_raw WHERE building_id IN ({marks})", related).fetchall():
        key = (src, sid)
        if key not in latest or fetched > latest[key][0]:
            latest[key] = (fetched, floor, flat, day, price)
    by_unit: dict[tuple, list] = {}
    for (src, sid), (_, floor, flat, day, price) in latest.items():
        by_unit.setdefault((floor, flat), []).append((src, sid, day, price))
    now = datetime.now()
    new_rows, pairs = [], []
    for r, bid in todo:
        (deal_id, address, floor, flat, price, area, reg, hold, gain, tag, _) = r
        unit_rows = [x for x in by_unit.get((floor, flat), [])
                     if not (x[0] == SOURCE and x[1] in (deal_id, deal_id + ":prev"))]
        cur = _near(price, reg, unit_rows)
        if cur:
            stats["current_existing"] += 1
            cur_src, cur_id = cur[0], cur[1]
        else:
            stats["current_added"] += 1
            cur_src, cur_id = SOURCE, deal_id
            new_rows.append(_row(deal_id, slug, name, bid, address, floor, flat, reg, tag,
                                 price, area, gain, now))
        prev = previous_deal(RicaDeal(deal_id, address, price, area, reg, hold, gain, tag))
        if prev is None:
            continue
        match = _near(prev[1], prev[0], unit_rows)
        if match:
            stats["prev_existing"] += 1
            prev_src, prev_id, prev_date, prev_price = match
            derived = False
        else:
            stats["prev_added"] += 1
            prev_src, prev_id = SOURCE, deal_id + ":prev"
            prev_date, prev_price, derived = prev[0], prev[1], True
            new_rows.append(_row(prev_id, slug, name, bid, address, floor, flat, prev_date,
                                 "unknown", prev_price, area, None, now,
                                 derived_from=deal_id))
        pairs.append([cur_src, cur_id, prev_src, prev_id, bid, db.unit_id(bid, floor, flat),
                      reg, price, prev_date, prev_price, hold, gain, "ricacorp_hold", derived])
        stats["pairs"] += 1
    done = [r[0] for r, _ in todo]
    if new_rows:
        db.insert_txn_raw(con, new_rows)
    for p in pairs:
        con.execute("INSERT OR REPLACE INTO deal_pair (cur_source, cur_id, prev_source, prev_id,"
                    " building_id, unit_id, cur_date, cur_price_hkd, prev_date, prev_price_hkd,"
                    " hold_days, gain_pct, basis, prev_derived) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", p)
    marks = ",".join("?" for _ in done)
    con.execute(f"UPDATE ricacorp_deal SET emitted = true WHERE deal_id IN ({marks})", done)


def _row(deal_id, slug, name, bid, address, floor, flat, day, deal_type, price, area, gain,
         now, derived_from=None) -> dict:
    import json

    return {
        "source": SOURCE, "source_id": deal_id, "fetched_at": now,
        "url": f"{BASE}{ESTATE_PATH}{slug}", "estate_id": bid.split("-s")[0].split("-b")[0],
        "building_id": bid, "estate_name": norm_name(name), "building_name": address,
        "floor_label": floor, "flat": flat, "deal_date": day, "registration_date": day,
        "deal_type": deal_type, "price_hkd": price, "saleable_sqft": area, "gain_pct": gain,
        "raw_json": json.dumps({"address": address,
                                **({"derived_from": derived_from, "approx_price": True}
                                   if derived_from else {})}, ensure_ascii=False),
    }


# ---- the crawl --------------------------------------------------


def week_key(today: date) -> str:
    y, w, _ = today.isocalendar()
    return f"{y}-W{w:02d}"


def run_ricacorp(fetcher, db_path=None, log=print, *, limit: int | None = None,
                 max_hours: float | None = None, clock=None, today: date | None = None) -> dict:
    """One pass over the estate pages, resumable within an ISO week (a new week starts a new
    pass). Each page is parsed, its rows stored and its cached copy deleted. Placement runs
    at the end (and can be rerun with `place_deals`)."""
    import time

    clock = clock or time.monotonic
    today = today or datetime.now().date()
    wk = week_key(today)
    xml = fetcher.get(SITEMAP_URL, "static")
    pages = estate_pages(xml)
    with db.session(db_path) as con:
        db.init_db(con)
        ensure_tables(con)
        done = {r[0] for r in con.execute(
            "SELECT step_key FROM run_progress WHERE command = ? AND starts_with(step_key, ?)",
            [COMMAND, wk + ":"]).fetchall()}
        run_id = db.start_run(con, COMMAND, f"week {wk}")
    todo = [p for p in pages if f"{wk}:{p.slug}" not in done]
    if limit:
        todo = todo[:limit]
    log(f"{len(pages)} estate pages in the sitemap, {len(done)} done this week, "
        f"{len(todo)} to do")
    stats = {"pages": 0, "deals": 0, "new": 0, "errors": 0}
    status, message, streak = "ok", "", 0
    started = clock()
    try:
        for i, page in enumerate(todo, 1):
            if max_hours is not None and clock() - started > max_hours * 3600:
                message = f"stopped after {max_hours} hours"
                status = "partial"
                break
            try:
                html = fetcher.get(page.url, "transaction")
                deals = parse_estate_page(html, page.slug)
            except SourceBlocked:
                raise
            except FetchError as e:
                stats["errors"] += 1
                streak += 1
                log(f"  {page.slug}: {e}")
                if streak >= MAX_CONSECUTIVE_ERRORS:
                    raise RuntimeError(f"{streak} errors in a row; stopping (rerun)") from e
                continue
            finally:
                fetcher.forget(page.url)  # 1 MB of markup; only the parsed rows are kept
            streak = 0
            with db.session(db_path) as con:
                stats["new"] += save_deals(con, page, deals, today)
                db.mark_step(con, COMMAND, f"{wk}:{page.slug}", run_id, f"{len(deals)} deals")
            stats["pages"] += 1
            stats["deals"] += len(deals)
            if i % 200 == 0:
                log(f"{i}/{len(todo)} {stats}")
    except BaseException as e:
        status = "interrupted" if isinstance(e, KeyboardInterrupt) else "failed"
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        placed = {}
        try:
            with db.session(db_path) as con:
                placed = place_deals(con, today, log)
        finally:
            with db.session(db_path) as con:
                db.finish_run(con, run_id, status, stats["new"],
                              (message + f" {stats} {placed}").strip())
    log(f"{status}: {stats}")
    return stats
