"""SRPE (Sales of First-hand Residential Properties Electronic platform): first-hand sales.

How the site works (probed 2026-10-16):
- It is a JavaScript app. After the visitor ticks the disclaimer box (`Auth/processAgmt`) its
  API calls carry a bearer token. We pass the disclaimer once with Playwright, take the token
  from the app's own request header, and then call the same JSON API with httpx.
- `DistrictAreaSearch/getDistrictAreaSearchResult` lists every development in one call.
- `Map/getMapDevResultById` gives one development's documents. Its Register of Transactions
  is a PDF (one per development, cumulative, re-issued when updated) at
  `/api/SrpeWebService/download/all_development_map/trx/<uniqueSessionId>/<id>/<file>/en`.
- robots.txt asks for `Crawl-delay: 30`, which we honour for every request after the login.

The register's table has one row per sale: preliminary agreement (PASP) date, agreement (ASP)
date, termination date, block, floor, unit, parking space, price, price revision, payment
terms and whether the buyer is a related party. Buyer names are not in the table and are
never stored. Sales that were terminated are dropped; a sale with only a PASP is kept and
marked, since the agreement may still fall through.

Stored in `txn_raw` as source `srpe`, `first_hand = true` (so `txn` leaves them out), with
SOURCE rank 0 in `txn_all`: when Centadata or 28Hse report the same flat later, their copy is
the duplicate and the SRPE row remains as the flagged first-hand sale.

Limits: only developments in the "Index for Residential Developments" (sales active or ended
under 18 months ago) are listed; older, finished developments sit in a second index that is
not read yet. Price-list PDFs (unit facing and size) are not parsed yet.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from hkestate import db, fetch
from hkestate.config import USER_AGENT, get_cache_dir
from hkestate.normalise import norm_name, parse_floor
from hkestate.sources import cd_master

SOURCE = "srpe"
COMMAND = "srpe"
HOST = "www.srpe.gov.hk"
API = f"https://{HOST}/api/SrpeWebService"
DISCLAIMER_URL = f"https://{HOST}/opip/disclaimer_index_for_all_residential"
DEFAULT_DELAY_S = 30.0

DDL = [
    """
    CREATE TABLE IF NOT EXISTS srpe_development (
        dev_id           VARCHAR PRIMARY KEY,
        name_zh          VARCHAR,
        phase_zh         VARCHAR,
        addresses        VARCHAR,
        district         VARCHAR,
        lat              DOUBLE,
        lon              DOUBLE,
        register_id      VARCHAR,           -- newest Register of Transactions read
        register_updated DATE,
        buildings        VARCHAR,           -- master buildings matched by address
        deals            INTEGER,
        checked_at       TIMESTAMP
    )""",
]


def ensure_tables(con) -> None:
    for ddl in DDL:
        con.execute(ddl)


# ---- parsers ---------------------------------------------------------------------------------


@dataclass
class Dev:
    dev_id: str
    name: str
    phase: str
    addresses: list[str]
    district: str


def _nospace(text) -> str:
    return re.sub(r"\s+", "", norm_name(str(text or "")))


def parse_dev_list(payload: dict) -> list[Dev]:
    out = []
    for item in (payload.get("resultData") or {}).get("list") or []:
        area = item.get("planningArea1") or {}
        out.append(Dev(
            dev_id=str(item["id"]),
            name=norm_name(item.get("chnName") or item.get("engName") or ""),
            phase=norm_name(item.get("chnPhaseName") or ""),
            addresses=[a for a in (_nospace(x.get("chnAddress")) for x in
                                   item.get("addresses") or []) if a],
            district=norm_name(area.get("broadDistrictNameChn") or ""),
        ))
    return out


@dataclass
class DevDetail:
    dev_id: str
    lat: float | None
    lon: float | None
    register_id: str | None
    register_file: str | None
    register_date: date | None
    download_path: str | None


def parse_dev_detail(payload: dict) -> DevDetail:
    data = payload.get("resultData") or {}
    dev = data.get("dev") or {}
    dev_id = str(data.get("devId") or dev.get("id") or "")
    regs = [t for t in data.get("transactions") or [] if (t.get("file") or {}).get("fileName")]
    reg = max(regs, key=lambda t: t.get("updateDate") or "") if regs else None
    path = None
    if reg:
        path = (f"/api/SrpeWebService/download/all_development_map/trx/"
                f"{data.get('uniqueSessionId')}/{reg['id']}/{reg['file']['fileName']}/en"
                f"?devId={dev_id}")
    upd = (reg or {}).get("updateDate")
    return DevDetail(
        dev_id=dev_id, lat=dev.get("latitude"), lon=dev.get("longtitude"),
        register_id=str(reg["id"]) if reg else None,
        register_file=reg["file"]["fileName"] if reg else None,
        register_date=date.fromisoformat(upd[:10]) if upd else None,
        download_path=path,
    )


@dataclass
class RegisterDeal:
    pasp_date: date | None
    asp_date: date | None
    block: str
    floor: str
    flat: str
    has_parking: bool
    price_hkd: int
    related_party: bool

    @property
    def deal_date(self) -> date:
        """The agreement (ASP) date, or the preliminary (PASP) date when there is no ASP or
        the ASP date is not believable: before the PASP, or more than a year after it. A
        register listed one ASP as 26/09/2055 against a PASP of 13/09/2025 (the vendor's typo;
        found by `hkestate datacheck` 2026-10-21); an ASP normally follows within days."""
        if self.asp_date is None:
            return self.pasp_date  # type: ignore[return-value]
        if self.pasp_date is not None and not (
                0 <= (self.asp_date - self.pasp_date).days <= MAX_PASP_TO_ASP_DAYS):
            return self.pasp_date
        return self.asp_date

    @property
    def pasp_only(self) -> bool:
        return self.asp_date is None


# An ASP date further than this from its PASP is taken as a typo in the register.
MAX_PASP_TO_ASP_DAYS = 365

# Registers write dates as 17/12/2015 or 05-04-2016 depending on the vendor.
_DATE = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\s*$")


def _date(cell) -> date | None:
    m = _DATE.match(str(cell or ""))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _cell(row, i) -> str:
    return re.sub(r"\s+", " ", str(row[i] or "")).strip() if i < len(row) else ""


def _lines(row, i) -> list[str]:
    """The non-empty lines of a cell, each with its spaces tidied."""
    text = str(row[i] or "") if i < len(row) else ""
    return [s for s in (re.sub(r"\s+", " ", x).strip() for x in text.split("\n")) if s]


def _parse_row(row) -> list[RegisterDeal]:
    pasp, asp = _date(_cell(row, 0)), _date(_cell(row, 1))
    if not (pasp or asp) or _date(_cell(row, 2)):  # no date, or the sale was terminated
        return []
    if len(row) != _HOUSE_COLUMNS:
        # Several flats in one row show as several prices. The block cell alone can't tell:
        # most registers write it in two languages on two lines ('第2座\nTower 2'); counting
        # its lines rejected nearly every row (fixed 2026-10-23, after a re-parse deleted
        # 1,285 stored sales).
        n = len([s for s in _lines(row, 7) if _AMOUNT_LINE.match(s)])
        if n > 1:
            return _parse_multi_flat_row(row, n, pasp, asp)
    deal = _parse_single(row, pasp, asp)
    return [deal] if deal else []


# A line that is only an amount ('$7,281,000'); a note line that starts with digits is not.
_AMOUNT_LINE = re.compile(r"^\s*(?:HK)?\$?\s*\d[\d,]*(?:\.\d+)?\s*$")


def _parse_multi_flat_row(row, n: int, pasp, asp) -> list[RegisterDeal]:
    """One table row that lists several flats (block, floor, unit and price cells each hold
    one line per flat, e.g. block "3\\n3", floor "2\\n5", unit "A\\nA", price
    "$7,281,000\\n$14,452,000"). Read as one cell the prices joined into
    HK$728,100,014,452,000 (development 3085). Lines are paired by position; when the counts
    differ we cannot tell which price is whose, so the row raises (and is reported skipped).
    A block cell without one line per flat (one block named in two languages) applies to all."""
    floors, units = _lines(row, 4), _lines(row, 5)
    prices = [s for s in _lines(row, 7) if _AMOUNT_LINE.match(s)]
    if len(floors) != n or len(units) != n or len(prices) != n:
        raise ValueError(f"row lists {n} flats but its cells do not line up")
    blocks = _lines(row, 3)
    cols = [blocks if len(blocks) == n else [_cell(row, 3)] * n, floors, units]
    related = _lines(row, 10)
    deals = []
    for k in range(n):
        one = [""] * len(row)
        one[0], one[1] = row[0], row[1]
        one[3], one[4], one[5] = cols[0][k], cols[1][k], cols[2][k]
        one[6], one[7] = row[6], prices[k]
        if len(related) == n:
            one[10] = related[k]
        elif len(row) > 10:
            one[10] = row[10]
        deal = _parse_single(one, pasp, asp)
        if deal:
            deals.append(deal)
    return deals


def _parse_single(row, pasp, asp) -> RegisterDeal | None:
    if len(row) == _HOUSE_COLUMNS:
        # House sales: one description cell (house number/name), parking, price, revision,
        # payment terms, related party. No block/floor/unit, so the house is the "flat".
        house = _cell(row, 3)
        block, floor, flat, parking, price, related = house, "H", house, _cell(row, 4), 5, 8
        has_parking = parking not in ("", "-")
    else:
        block, floor, flat = _cell(row, 3), _cell(row, 4), _cell(row, 5)
        has_parking, price, related = bool(_cell(row, 6)), 7, 10
    # Only the first amount: a cell sometimes carries a long note whose digits must not be joined.
    amount = re.search(r"\d[\d,]*(?:\.\d+)?", _cell(row, price))
    if not floor or not flat or not amount:
        return None
    value = int(round(float(amount.group().replace(",", ""))))
    if not MIN_PRICE_HKD <= value <= MAX_PRICE_HKD:
        raise ValueError(f"price {value} is not a sensible sale price")
    return RegisterDeal(
        pasp, asp, block, floor, flat.upper(), has_parking, value,
        _cell(row, related).lower().startswith(("y", "是")))


# A parsed price outside this range is a misread cell, not a sale (the row is skipped).
MIN_PRICE_HKD = 100_000
MAX_PRICE_HKD = 10_000_000_000

# The house-sale register has 9 columns instead of the usual 11.
_HOUSE_COLUMNS = 9


def parse_register_rows(rows, skipped: list[str] | None = None) -> list[RegisterDeal]:
    """Sales from the Part 2 table rows. Columns: PASP date, ASP date, termination date,
    block, floor, unit, parking, price, revision, payment terms, related party.
    A row that cannot be parsed is left out; its raw text is appended to `skipped`."""
    deals = []
    for row in rows:
        try:
            deals += _parse_row(row)
        except (ValueError, OverflowError):
            if skipped is not None:
                skipped.append(" | ".join(_cell(row, i) for i in range(len(row))))
    return deals


def register_deals(pdf, skipped: list[tuple[int, str]] | None = None) -> list[RegisterDeal]:
    """All sales in an opened pdfplumber PDF (the table is repeated on every page).
    Unparseable rows are added to `skipped` as (page number, raw row text)."""
    deals = []
    for n, page in enumerate(pdf.pages, 1):
        for table in page.extract_tables():
            bad: list[str] = []
            deals += parse_register_rows(table, bad)
            if skipped is not None:
                skipped += [(n, text) for text in bad]
        if hasattr(page, "close"):
            page.close()  # pdfplumber keeps every page's layout otherwise (GBs on big registers)
    return deals


# ---- matching and storing ----------------------------------------------------------------------


def match_development(con, dev: Dev) -> list[str]:
    """Master buildings at the development's street addresses (exact street + number)."""
    found: list[str] = []
    for addr in dev.addresses:
        for (bid,) in con.execute(
            "SELECT building_id FROM master_building WHERE replace(address, ' ', '') = ? "
            "OR replace(address, ' ', '') LIKE '%/' || ?", [addr, addr]
        ).fetchall():
            if bid not in found:
                found.append(bid)
    return found


def _pick_building(con, bids: list[str], block: str) -> str | None:
    if len(bids) == 1:
        return bids[0]
    if not bids:
        return None
    marks = ",".join("?" for _ in bids)
    rows = con.execute(
        f"SELECT building_id, coalesce(block_name, '') || ' ' || coalesce(phase_name, '') "
        f"FROM master_building WHERE building_id IN ({marks})", bids).fetchall()
    hits = [b for b, text in rows if block and block in text]
    return hits[0] if len(hits) == 1 else None


def store_deals(con, dev_id: str, dev_name: str, bids: list[str], deals: list[RegisterDeal],
                url: str) -> int:
    now = datetime.now()
    rows = []
    for d in deals:
        bid = _pick_building(con, bids, d.block)
        estate = None
        if bid:
            r = con.execute("SELECT estate_code FROM master_building WHERE building_id = ?",
                            [bid]).fetchone()
            estate = f"cd-{r[0]}" if r and r[0] else None
        fl = parse_floor(d.floor)
        key = d.pasp_date or d.asp_date
        rows.append({
            "source": SOURCE, "source_id": f"{dev_id}:{d.block}:{fl.label}:{d.flat}:{key}",
            "fetched_at": now, "url": url, "estate_id": estate, "building_id": bid,
            "estate_name": dev_name, "building_name": d.block, "floor_label": fl.label,
            "flat": d.flat, "deal_date": d.deal_date, "instrument_date": d.deal_date,
            "deal_type": "first_hand", "price_hkd": d.price_hkd, "first_hand": True,
            "non_market": d.related_party,
            "raw_json": json.dumps({
                "pasp_date": str(d.pasp_date), "asp_date": str(d.asp_date),
                "pasp_only": d.pasp_only, "block": d.block, "has_parking": d.has_parking,
                "related_party": d.related_party}, ensure_ascii=False),
        })
    return db.insert_txn_raw(con, rows)


# ---- talking to the site -------------------------------------------------------------


class SrpeClient:
    """Bearer-token JSON client at the site's Crawl-delay. The token comes from one
    Playwright visit that ticks the disclaimer, exactly as a visitor would."""

    def __init__(self, delay_s: float = DEFAULT_DELAY_S, sleep=time.sleep, clock=time.monotonic,
                 allowed=None):
        # `allowed(url) -> bool` is the robots.txt check (Fetcher.allowed); None = no check
        self._allowed = allowed
        self.delay_s = delay_s
        self._sleep, self._clock = sleep, clock
        self._last = None
        self._token = ""
        self._http = httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT})
        self.requests = 0

    def close(self):
        self._http.close()

    def _check(self, url: str) -> None:
        """Refuse (before any request) a URL robots.txt disallows for our user agent."""
        if self._allowed is not None and not self._allowed(url):
            from hkestate.fetch import RobotsDisallowed

            raise RobotsDisallowed(url)

    def login(self) -> None:
        from playwright.sync_api import sync_playwright

        self._check(DISCLAIMER_URL)
        fetch.spend_request()  # the browser visit is one page load for the cap

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_context(user_agent=USER_AGENT).new_page()
                seen = {}
                page.on("request", lambda r: seen.setdefault("a", r.headers.get("authorization"))
                        if r.headers.get("authorization") and HOST in r.url else None)
                page.goto(DISCLAIMER_URL, wait_until="networkidle", timeout=90_000)
                page.check("input[type=checkbox]")
                page.click("button:has-text('Continue')")
                for _ in range(60):
                    if seen.get("a"):
                        break
                    page.wait_for_timeout(1000)  # lets the browser deliver its events
                self._token = seen.get("a") or ""
            finally:
                browser.close()
        if not self._token:
            raise RuntimeError("SRPE login gave no token")
        self._last = self._clock()

    def _wait(self) -> None:
        if self._last is not None:
            wait = self._last + self.delay_s + random.uniform(0, 2) - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        self._check(url)
        for attempt in (0, 1):
            self._wait()
            fetch.spend_request()  # the process-wide cap; raises RequestCapReached
            self.requests += 1
            r = self._http.request(method, url, headers={"Authorization": self._token}, **kw)
            if r.status_code == 401 and attempt == 0:
                self.login()  # token expired
                continue
            r.raise_for_status()
            return r
        raise AssertionError("unreachable")

    def post(self, endpoint: str, payload: dict) -> dict:
        body = {"language": "en", "actionId": f"{int(time.time() * 1000)}_0", **payload}
        return self._send("POST", f"{API}/{endpoint}", json=body).json()

    def download(self, path: str) -> bytes:
        return self._send("GET", f"https://{HOST}{path}").content


LIST_PAYLOAD = {
    "broadDistrictId": "A", "planningAreaId": "A", "planningAreaIdString": "All",
    "firstPrintYear": "A", "searchByYearOnly": False, "planningAreaIds": ["A"],
    "fromPath": "disclaimer_index_for_all_residential",
    "actionType": "Index For All Residential", "page": None, "limit": None,
}


# One SRPE *run* reads every development's detail once. Its tag is stored once in
# run_progress (command `srpe`, step `run`, detail "open <tag>" or "done <tag>") and is part
# of each detail step key (`detail:<dev id>:<tag>`). While the run is open, a resume on any
# later day reuses the tag and skips the details already read. Once a run has read every
# development, the next run gets a new tag, so it reads every detail again and sees registers
# published since. The tag is the run_log id of the run's first invocation. Older keys
# (`detail:<id>:<date>` and `detail:<id>`) never carry an `r` tag, so they are left alone.
RUN_STEP = "run"


def _run_tag(con, run_id: int) -> str:
    """The open SRPE run's tag, or a new one (stored) if none is open."""
    row = con.execute("SELECT detail FROM run_progress WHERE command = ? AND step_key = ?",
                      [COMMAND, RUN_STEP]).fetchone()
    if row and (row[0] or "").startswith("open "):
        return row[0].split()[1]
    tag = f"r{run_id}"
    db.mark_step(con, COMMAND, RUN_STEP, run_id, f"open {tag} {date.today().isoformat()}")
    return tag


def _detail_key(dev_id: str, tag: str) -> str:
    return f"detail:{dev_id}:{tag}"


def in_subset(con, dev: Dev, estates: set[str]) -> bool:
    """The development's buildings (by street address) include one of the subset's master
    estates (`2-` codes). DB only: used by `--estates` before any detail is read."""
    bids = match_development(con, dev)
    if not bids:
        return False
    marks = ",".join("?" for _ in bids)
    codes = {r[0] for r in con.execute(
        f"SELECT estate_code FROM master_building WHERE building_id IN ({marks})",
        bids).fetchall()}
    return bool(codes & estates)


def run_srpe(client: SrpeClient, db_path=None, log=print, limit: int | None = None,
             estates: set[str] | None = None) -> dict:
    """List developments, then read each one's newest register. Resumable: a development's
    detail is read once per SRPE run (see RUN_STEP; a resume on a later day skips it) and a
    register is parsed once per issue. `estates`: only developments at buildings of these
    master estates (`2-` codes; test rebuilds). `stats["left"]` counts the developments of
    this run's list still unread at the end; the run is closed when none is left."""
    import pdfplumber

    from hkestate.fetch import RobotsDisallowed

    with db.session(db_path) as con:
        db.init_db(con)
        cd_master.ensure_tables(con)
        ensure_tables(con)
        args = " ".join(filter(None, [f"limit={limit}" if limit else "",
                                      f"estates={','.join(sorted(estates))}"
                                      if estates is not None else ""]))
        run_id = db.start_run(con, COMMAND, args)
        tag = _run_tag(con, run_id)
    stats = {"developments": 0, "registers": 0, "deals": 0, "unmatched": 0,
             "skipped_rows": 0, "failed_registers": 0, "left": 0}
    status, message = "ok", ""
    pdf_dir = get_cache_dir() / HOST / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    try:
        client.login()
        devs = parse_dev_list(client.post("DistrictAreaSearch/getDistrictAreaSearchResult",
                                          LIST_PAYLOAD))
        log(f"{len(devs)} developments listed (run {tag})")
        if estates is not None:  # an empty subset keeps nothing (never the whole list)
            with db.session(db_path) as con:
                devs = [d for d in devs if in_subset(con, d, estates)]
            log(f"{len(devs)} developments in the estates subset")
        if limit:
            devs = devs[:limit]
        for i, dev in enumerate(devs, 1):
            with db.session(db_path) as con:
                if db.step_done(con, COMMAND, _detail_key(dev.dev_id, tag)):
                    continue
            detail = parse_dev_detail(client.post("Map/getMapDevResultById", {
                "devId": dev.dev_id, "actionType": "developments details",
                "timeStamp": int(time.time() * 1000)}))
            added, bids = 0, []
            with db.session(db_path) as con:
                bids = match_development(con, dev)
                # ":u" = read before its buildings were in the master list; redone once they are
                step = f"register:{dev.dev_id}:{detail.register_id}:{'m' if bids else 'u'}"
                done = detail.register_id is None or db.step_done(con, COMMAND, step)
            if not done:
                try:
                    pdf_path = pdf_dir / f"{dev.dev_id}_{detail.register_file}"
                    if not pdf_path.exists():
                        try:
                            pdf_path.write_bytes(client.download(detail.download_path))
                        except RobotsDisallowed:
                            log(f"skipped register: robots.txt disallows {detail.download_path}")
                            continue
                    bad_rows: list[tuple[int, str]] = []
                    with pdfplumber.open(pdf_path) as pdf:
                        deals = register_deals(pdf, bad_rows)
                    for page_no, text in bad_rows:
                        log(f"skipped row: development {dev.dev_id} {dev.name} page {page_no}: "
                            f"{text[:300]!r}")
                    stats["skipped_rows"] += len(bad_rows)
                    with db.session(db_path) as con:
                        added = store_deals(con, dev.dev_id, dev.name, bids, deals,
                                            f"https://{HOST}{detail.download_path.split('?')[0]}")
                        db.mark_step(con, COMMAND, step, run_id, f"{len(deals)} sales")
                    stats["registers"] += 1
                    stats["deals"] += added
                except Exception as e:  # one bad register must never end the whole run
                    stats["failed_registers"] += 1
                    log(f"FAILED register: development {dev.dev_id} {dev.name}: "
                        f"{type(e).__name__}: {str(e)[:300]}")
                    continue  # not marked done, so the next run tries it again
            stats["developments"] += 1
            stats["unmatched"] += not bids
            with db.session(db_path) as con:
                con.execute(
                    "INSERT OR REPLACE INTO srpe_development VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?)",
                    [dev.dev_id, dev.name, dev.phase, ";".join(dev.addresses), dev.district,
                     detail.lat, detail.lon, detail.register_id, detail.register_date,
                     ",".join(bids), added, datetime.now()])
                db.mark_step(con, COMMAND, _detail_key(dev.dev_id, tag), run_id, "ok")
            if i % 10 == 0:
                log(f"{i}/{len(devs)} {stats}")
        with db.session(db_path) as con:
            stats["left"] = sum(not db.step_done(con, COMMAND, _detail_key(d.dev_id, tag))
                                for d in devs)
            if stats["left"] == 0 and not limit:  # every development read: close the run
                db.mark_step(con, COMMAND, RUN_STEP, run_id,
                             f"done {tag} {date.today().isoformat()}")
    except BaseException as e:
        status = "interrupted" if isinstance(e, KeyboardInterrupt) else "failed"
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        with db.session(db_path) as con:
            db.finish_run(con, run_id, status, stats["deals"], message or json.dumps(stats))
    log(f"done: {stats}")
    return stats


def reparse_cached(db_path=None, log=print, dry_run: bool = False) -> dict:
    """Re-read every saved register PDF with today's parser (no web requests), store the
    result, and delete that development's stored rows the new parse no longer gives: the old
    multi-flat rows with joined prices (their source_id holds several flats, e.g.
    `3085:3 3:2 5:A A:...`), and sales since terminated. A development whose PDF now gives no
    deals at all is left untouched, so a PDF that fails to open never wipes its rows."""
    import pdfplumber

    pdf_dir = get_cache_dir() / HOST / "pdf"
    newest: dict[str, object] = {}  # dev_id -> its newest saved register
    for path in sorted(pdf_dir.glob("*_*"), key=lambda p: p.stat().st_mtime):
        newest[path.name.split("_", 1)[0]] = path
    with db.session(db_path) as con:
        known = {r[0]: (r[1], [b for b in (r[2] or "").split(",") if b]) for r in con.execute(
            "SELECT dev_id, name_zh, buildings FROM srpe_development").fetchall()}
    stats = {"registers": 0, "added": 0, "deleted": 0, "skipped_rows": 0, "empty": 0}
    for dev_id, path in sorted(newest.items()):
        if dev_id not in known:
            continue
        name, bids = known[dev_id]
        bad: list[tuple[int, str]] = []
        try:
            with pdfplumber.open(path) as pdf:
                deals = register_deals(pdf, bad)
        except Exception as e:  # one unreadable PDF must not stop the rest
            log(f"FAILED {path.name}: {type(e).__name__}: {str(e)[:200]}")
            continue
        stats["skipped_rows"] += len(bad)
        if not deals:
            stats["empty"] += 1
            continue
        stats["registers"] += 1
        with db.session(db_path) as con:
            keep = {f"{dev_id}:{d.block}:{parse_floor(d.floor).label}:{d.flat}:"
                    f"{d.pasp_date or d.asp_date}" for d in deals}
            stale = [sid for (sid,) in con.execute(
                "SELECT DISTINCT source_id FROM txn_raw WHERE source = ? AND source_id LIKE ?",
                [SOURCE, f"{dev_id}:%"]).fetchall() if sid not in keep]
            if dry_run:
                stats["deleted"] += len(stale)
                continue
            # the old URL carried a session id; keep whatever the stored rows already have
            url = con.execute("SELECT any_value(url) FROM txn_raw WHERE source = ? AND "
                              "source_id LIKE ?", [SOURCE, f"{dev_id}:%"]).fetchone()[0]
            stats["added"] += store_deals(con, dev_id, name, bids, deals, url or "")
            for i in range(0, len(stale), 500):
                part = stale[i:i + 500]
                con.execute(f"DELETE FROM txn_raw WHERE source = ? AND source_id IN "
                            f"({','.join('?' for _ in part)})", [SOURCE, *part])
            stats["deleted"] += len(stale)
        if stats["registers"] % 50 == 0:
            log(f"{stats['registers']} registers {stats}")
    log(f"reparse done{' (dry run, nothing written)' if dry_run else ''}: {stats}")
    return stats


def main(argv=None) -> int:
    import argparse

    from hkestate.cli import _logger

    p = argparse.ArgumentParser(prog="python -m hkestate.sources.srpe")
    p.add_argument("--db", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--reparse-cached", action="store_true",
                   help="re-read the saved register PDFs (no web requests) and replace each "
                        "development's rows with the new parse")
    p.add_argument("--dry-run", action="store_true", help="with --reparse-cached: count only")
    p.add_argument("--estates", default=None,
                   help="test subset: comma-separated estate names or Centadata 2- codes")
    args = p.parse_args(argv)
    if args.reparse_cached:
        reparse_cached(args.db, _logger("srpe"), args.dry_run)
        return 0
    from hkestate import link
    from hkestate.fetch import Fetcher

    estates = None
    if args.estates:
        with db.session(args.db) as con:
            estates = link.resolve_estates(con, args.estates.split(","))
    log = _logger("srpe")
    with Fetcher() as f:
        if not f.allowed(f"{API}/Map/getMapDevResultById"):
            raise SystemExit("robots.txt disallows the SRPE API")
        delay = max(DEFAULT_DELAY_S, f._min_interval.get(HOST, 0.0))  # robots Crawl-delay
        # every URL (disclaimer page, list, detail, register download) is checked before use
        client = SrpeClient(delay_s=delay, allowed=f.allowed)
        try:
            run_srpe(client, args.db, log, args.limit, estates=estates)
        finally:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
