"""Centadata (Centaline's deal database) block pages, used as a cross-check of 28Hse.

Pages live at hk.centanet.com/findproperty/centadata-details/<name>_<code>: `2-`/`3-` codes
are estates or phases (they open their first block), `1-` codes are blocks. Each page holds
the estate's phase menu, the phase's block menu and, for one block, every flat with its
latest recorded deal (back to the 1990s): price, saleable and gross area, agreement date
(`insDate`) and registration date (`regDate`).

The data sits in the page as a minified JavaScript call, `window.__NUXT__=(function(a,b,..)
{return {...}}(v1,v2,..))`. We never run it: `read_nuxt` reads it with a small parser that
accepts only literal data (strings, numbers, true/false/null, objects, arrays) and the
function's parameter names, and refuses anything else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import quote

from hkestate.normalise import parse_floor

BASE = "https://hk.centanet.com/findproperty/centadata-details/"
SOURCE = "centadata"
WINDOW_START = date(2018, 1, 1)  # 28Hse's free data starts here
DUP_PRICE_TOL = 0.01
DUP_DAYS = 90


class NotData(Exception):
    """The embedded script contains something other than literal data."""


def block_url(name: str, code: str) -> str:
    return BASE + quote(f"{name}_{code}")


# ---- literal reader ------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<str>"(?:[^"\\]|\\.)*")
      | (?P<num>-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
      | (?P<punct>[{}\[\]:,])
      | (?P<not>![01])
      | (?P<ident>[A-Za-z_$][\w$]*)
    )""",
    re.VERBOSE,
)

_UNSET = object()


class _Reader:
    def __init__(self, text: str, refs: dict | None):
        self.text = text
        self.pos = 0
        self.refs = refs

    def _next(self):
        m = _TOKEN.match(self.text, self.pos)
        if not m:
            raise NotData(f"unexpected text at {self.pos}: {self.text[self.pos:self.pos + 30]!r}")
        self.pos = m.end()
        kind = m.lastgroup
        return kind, m.group(kind)

    def _peek(self):
        saved = self.pos
        try:
            return self._next()
        finally:
            self.pos = saved

    def value(self):
        kind, tok = self._next()
        if kind == "str":
            return json.loads(tok)
        if kind == "num":
            v = float(tok)
            return int(v) if v.is_integer() and not re.search(r"[.eE]", tok) else v
        if kind == "not":
            return tok == "!0"
        if kind == "ident":
            if tok == "true":
                return True
            if tok == "false":
                return False
            if tok == "null":
                return None
            if tok == "void":
                k2, t2 = self._next()
                if (k2, t2) != ("num", "0"):
                    raise NotData("void with an expression")
                return None
            if self.text[self.pos:].lstrip().startswith("("):
                raise NotData("function call")
            if self.refs is None or tok not in self.refs:
                raise NotData(f"unknown name {tok!r}")
            return self.refs[tok]
        if tok == "{":
            obj = {}
            if self._peek() == ("punct", "}"):
                self._next()
                return obj
            while True:
                k_kind, key = self._next()
                if k_kind == "str":
                    key = json.loads(key)
                elif k_kind not in ("ident", "num"):
                    raise NotData(f"bad key {key!r}")
                if self._next() != ("punct", ":"):
                    raise NotData("expected ':'")
                obj[key] = self.value()
                sep = self._next()
                if sep == ("punct", "}"):
                    return obj
                if sep != ("punct", ","):
                    raise NotData(f"expected ',' or '}}', got {sep[1]!r}")
        if tok == "[":
            arr = []
            if self._peek() == ("punct", "]"):
                self._next()
                return arr
            while True:
                arr.append(self.value())
                sep = self._next()
                if sep == ("punct", "]"):
                    return arr
                if sep != ("punct", ","):
                    raise NotData("expected ',' or ']'")
        raise NotData(f"unexpected token {tok!r}")


class _Lazy:
    """A parameter reference, resolved once the argument values are known."""

    def __init__(self, name):
        self.name = name


def _resolve(v, values):
    if isinstance(v, _Lazy):
        return values[v.name]
    if isinstance(v, dict):
        return {k: _resolve(x, values) for k, x in v.items()}
    if isinstance(v, list):
        return [_resolve(x, values) for x in v]
    return v


def parse_js_value(text: str, refs: dict | None = None):
    r = _Reader(text, refs if refs is not None else {})
    v = r.value()
    if text[r.pos:].strip():
        raise NotData("trailing text")
    return v


def read_nuxt(html: str) -> dict:
    """The page data object from `window.__NUXT__=(function(params){return OBJ}(args))`."""
    start = html.find("window.__NUXT__=(function(")
    if start < 0:
        raise NotData("no __NUXT__ data on the page")
    pos = start + len("window.__NUXT__=(function(")
    close = html.index(")", pos)
    params = [p.strip() for p in html[pos:close].split(",") if p.strip()]
    if not all(re.fullmatch(r"[A-Za-z_$][\w$]*", p) for p in params):
        raise NotData("unexpected parameter list")
    body_start = html.index("{", close)
    rest = html[body_start + 1:].lstrip()
    if not rest.startswith("return"):
        raise NotData("function body is not a single return")
    lazy = {p: _Lazy(p) for p in params}
    reader = _Reader(rest[len("return"):], lazy)
    obj = reader.value()
    tail = rest[len("return") + reader.pos:].lstrip()
    if not tail.startswith("}"):
        raise NotData("function body has more than a return")
    tail = tail[1:].lstrip()
    if not tail.startswith("("):
        raise NotData("expected argument list")
    args_reader = _Reader(tail, {})
    args_reader.pos = 1
    args = []
    while True:
        rest_args = tail[args_reader.pos:].lstrip()
        if rest_args.startswith(")"):
            break
        args.append(args_reader.value())
        rest_args = tail[args_reader.pos:]
        stripped = rest_args.lstrip()
        args_reader.pos += len(rest_args) - len(stripped) + 1
        if stripped[:1] == ")":
            break
        if stripped[:1] != ",":
            raise NotData("bad argument list")
    values = {p: (args[i] if i < len(args) else None) for i, p in enumerate(params)}
    return _resolve(obj, values)


# ---- block page ---------------------------------------------------------------------------------


@dataclass
class CdDeal:
    trans_id: str
    floor_text: str
    flat: str
    price_hkd: int | None
    saleable_sqft: float | None
    gross_sqft: float | None
    instrument_date: date | None
    registration_date: date | None
    raw: dict = field(default_factory=dict, compare=False)


@dataclass
class BlockPage:
    code: str
    estate: str
    phase: str
    block: str
    address: str
    phases: list[tuple[str, str]]
    blocks: list[tuple[str, str]]
    deals: list[CdDeal]


def _d(text) -> date | None:
    if not text:
        return None
    return datetime.fromisoformat(str(text)[:10]).date()


def _area(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def parse_block_page(html: str) -> BlockPage:
    data = read_nuxt(html)["data"][0]
    table = data.get("tableList") or {}
    deals = []
    for grp in table.get("xAxisGrps") or []:
        for floor in grp.get("floors") or []:
            for u in floor.get("units") or []:
                price = u.get("transactionPrice")
                if not price or not u.get("transID"):
                    continue
                # floors read "44樓\n(44/F)", flats "A室"
                floor_text = str(floor.get("yAxis") or "").split("\n")[0].strip()
                floor_text = floor_text[:-1] if floor_text.endswith("樓") else floor_text
                flat = str(u.get("xAxis") or "").strip()
                flat = flat[:-1] if flat.endswith("室") else flat
                deals.append(
                    CdDeal(
                        trans_id=str(u["transID"]),
                        floor_text=floor_text,
                        flat=flat,
                        price_hkd=int(round(price)),
                        saleable_sqft=_area(u.get("nArea")),
                        gross_sqft=_area(u.get("gArea")),
                        instrument_date=_d(u.get("insDate")),
                        registration_date=_d(u.get("regDate")),
                        raw={k: v for k, v in u.items() if k != "mediaInfo"},
                    )
                )
    levels = lambda part: [  # noqa: E731
        (s["key"], s["value"]) for s in ((data.get(part) or {}).get("subLevels") or [])
    ]
    return BlockPage(
        code=str((table.get("menuItem") or {}).get("typeCode") or ""),
        estate=str(table.get("estate") or ""),
        phase=str(table.get("phase") or ""),
        block=str(table.get("property") or ""),
        address=str(table.get("address") or ""),
        phases=levels("bigest"),
        blocks=levels("phase"),
        deals=deals,
    )


def split_menu(menu: list[tuple[str, str]]) -> tuple[list, list]:
    """Estate menu entries: `2-` codes are phases (visit for their blocks), `1-` are blocks."""
    return [m for m in menu if m[0].startswith("2-")], [m for m in menu if m[0].startswith("1-")]


# ---- cross-check ------------------------------------------------------------------------


def match_deal(d: CdDeal, hse: list[tuple[str, date, int]]) -> tuple[str, str] | None:
    """The 28Hse deal (source_id, deal_date, price) on the same flat that is the same sale:
    price within 1% and date within 90 days of the agreement or registration date.
    Returns (source_id, which Centadata date 28Hse's date equals: instrument, registration
    or near)."""
    best = None
    for sid, day, price in hse:
        if not price or not d.price_hkd or abs(price - d.price_hkd) > DUP_PRICE_TOL * price:
            continue
        gaps = [abs((day - x).days) for x in (d.instrument_date, d.registration_date) if x]
        if not gaps or min(gaps) > DUP_DAYS:
            continue
        kind = (
            "instrument" if day == d.instrument_date
            else "registration" if day == d.registration_date
            else "near"
        )
        if best is None or min(gaps) < best[2]:
            best = (sid, kind, min(gaps))
    return (best[0], best[1]) if best else None


@dataclass
class BlockResult:
    block: str
    building_id: str | None
    deals: int = 0
    in_window: int = 0
    matched: int = 0
    date_kinds: dict = field(default_factory=dict)
    added: int = 0
    unmatched_in_window: list = field(default_factory=list)


def in_window(d: CdDeal) -> bool:
    days = [x for x in (d.instrument_date, d.registration_date) if x]
    return bool(days) and max(days) >= WINDOW_START


def crosscheck_block(con, blk: BlockPage, buildings: dict[str, tuple[str, str]], url: str):
    """Match one Centadata block's deals to 28Hse deals; store Centadata-only deals."""
    from hkestate import db
    from hkestate.backfill import match_building

    bid = match_building(f"{blk.phase} {blk.block}", buildings)
    res = BlockResult(block=f"{blk.estate} {blk.phase} {blk.block}", building_id=bid)
    res.deals = len(blk.deals)
    if bid is None:
        return res
    hse_rows = con.execute(
        "SELECT floor_label, flat, source_id, deal_date, price_hkd FROM txn_latest "
        "WHERE source = 'hse28' AND building_id = ?",
        [bid],
    ).fetchall()
    by_unit: dict[tuple, list] = {}
    for fl, flat, sid, day, price in hse_rows:
        by_unit.setdefault((fl, flat), []).append((sid, day, price))
    new_rows = []
    now = datetime.now()
    for d in blk.deals:
        fl = parse_floor(d.floor_text).label
        m = match_deal(d, by_unit.get((fl, d.flat), []))
        if in_window(d):
            res.in_window += 1
            if m:
                res.matched += 1
                res.date_kinds[m[1]] = res.date_kinds.get(m[1], 0) + 1
            else:
                res.unmatched_in_window.append((fl, d.flat, d.instrument_date, d.price_hkd))
        if m:
            continue
        new_rows.append(
            {
                "source": SOURCE,
                "source_id": d.trans_id,
                "fetched_at": now,
                "url": url,
                "estate_id": bid.split("-s")[0].split("-b")[0],
                "building_id": bid,
                "estate_name": blk.estate,
                "phase_name": blk.phase,
                "building_name": blk.block,
                "floor_label": fl,
                "flat": d.flat,
                "deal_date": d.instrument_date or d.registration_date,
                "instrument_date": d.instrument_date,
                "registration_date": d.registration_date,
                "deal_type": "registry",
                "price_hkd": d.price_hkd,
                "saleable_sqft": d.saleable_sqft,
                "gross_sqft": d.gross_sqft,
                "raw_json": json.dumps(d.raw, ensure_ascii=False, default=str),
            }
        )
    res.added = db.insert_txn_raw(con, new_rows)
    return res


# ---- crawl and report -------------------------------------------------------------------

# Entry pages for the pilot estates (any phase code of the estate opens its menus).
PILOT_ENTRIES = {
    "taikoo-shing": ("太古城", "2-OVDUURFSCJ"),
    "mei-foo": ("美孚新邨", "2-UDDCFRDSDR"),
    "city-one": ("沙田第一城", "3-XSHNIHSXHT"),
    "lohas-park": ("日出康城", "2-BESPWPPHAK"),
    "goldwin-heights": ("高雲臺", "2-SKSSGPYXPE"),
}


def group_buildings(con, group: str) -> dict[str, tuple[str, str]]:
    """28Hse buildings of an estate group: building_id -> (estate + phase text, block)."""
    rows = con.execute(
        """
        SELECT b.building_id, e.name_zh || ' ' || coalesce(p.name_zh, ''), b.name_zh
        FROM building b JOIN estate e USING (estate_id) LEFT JOIN phase p USING (phase_id)
        WHERE e.group_name = ?
        """,
        [group],
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def crawl_estate(fetcher, name: str, entry_code: str, log=print):
    """Yield (url, BlockPage) for every block of the estate, each once."""
    seen: set[str] = set()

    def visit(code):
        url = block_url(name, code)
        page = parse_block_page(fetcher.get(url, "transaction"))
        return url, page

    url, entry = visit(entry_code)
    phases, blocks = split_menu(entry.phases)
    queue = list(blocks) + list(split_menu(entry.blocks)[1])
    seen.add(entry.code)
    yield url, entry
    for code, _ in phases:
        url, page = visit(code)
        queue += split_menu(page.blocks)[1]
        if page.code not in seen:
            seen.add(page.code)
            yield url, page
    for code, _ in queue:
        if code in seen:
            continue
        seen.add(code)
        try:
            yield visit(code)
        except Exception as e:  # a missing block page is reported, not fatal
            log(f"  block {code} failed: {type(e).__name__} {e}")


def run_crosscheck(groups: list[str], fetcher, db_path=None, log=print) -> list[dict]:
    """Cross-check each estate group against Centadata; return one summary per group."""
    from hkestate import db

    summaries = []
    for group in groups:
        name, code = PILOT_ENTRIES[group]
        with db.session(db_path) as con:
            buildings = group_buildings(con, group)
        s = {"group": group, "blocks": 0, "blocks_matched": 0, "deals": 0, "in_window": 0,
             "matched": 0, "added": 0, "date_kinds": {}, "unmatched_blocks": [],
             "unmatched_deals": []}
        for url, blk in crawl_estate(fetcher, name, code, log):
            with db.session(db_path) as con:
                res = crosscheck_block(con, blk, buildings, url)
            s["blocks"] += 1
            s["deals"] += res.deals
            if res.building_id is None:
                s["unmatched_blocks"].append(res.block)
                continue
            s["blocks_matched"] += 1
            s["in_window"] += res.in_window
            s["matched"] += res.matched
            s["added"] += res.added
            for k, v in res.date_kinds.items():
                s["date_kinds"][k] = s["date_kinds"].get(k, 0) + v
            s["unmatched_deals"] += [(res.block, *u) for u in res.unmatched_in_window]
        rate = s["matched"] / s["in_window"] if s["in_window"] else 0
        log(f"{group}: {s['blocks_matched']}/{s['blocks']} blocks mapped, "
            f"{s['matched']}/{s['in_window']} Centadata deals since 2018 found in 28Hse "
            f"({rate:.1%}), dates {s['date_kinds']}, {s['added']} Centadata-only rows added")
        summaries.append(s)
    return summaries


def write_report(summaries: list[dict], path) -> None:
    lines = [
        "# Centadata cross-check",
        "",
        f"Run {datetime.now():%Y-%m-%d %H:%M}. Each Centadata block page shows the latest deal",
        "per flat. 'In window' = deals dated 2018 or later (28Hse's free range). A deal",
        "matches when 28Hse has the same flat at a price within 1% and a date within 90 days.",
        "",
        "| Estate | Blocks mapped | Centadata deals | In window | Found in 28Hse | Match rate"
        " | 28Hse date = | Centadata-only rows added |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        rate = s["matched"] / s["in_window"] if s["in_window"] else 0
        lines.append(
            f"| {s['group']} | {s['blocks_matched']}/{s['blocks']} | {s['deals']} | "
            f"{s['in_window']} | {s['matched']} | {rate:.1%} | {s['date_kinds']} | {s['added']} |"
        )
    for s in summaries:
        if s["unmatched_blocks"]:
            lines += ["", f"## {s['group']}: blocks not mapped to a 28Hse building", ""]
            lines += [f"- {b}" for b in s["unmatched_blocks"]]
        if s["unmatched_deals"]:
            lines += ["", f"## {s['group']}: in-window Centadata deals not in 28Hse", ""]
            lines += [f"- {b} {fl} {flat} {d} HK${p:,}" for b, fl, flat, d, p in
                      s["unmatched_deals"][:200]]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
