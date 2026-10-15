"""28Hse (www.28hse.com): the transaction master source.

Three page types, all server-rendered HTML (free data from 2018 on):
- estate deal list  `/estate/detail/<name>-<id>/transaction[/page-N]`: 10 deals per page,
  newest first, reaching back to about March 2021. Land Registry deals link to their flat
  (`unitid-N`); agency-reported ("market") deals link to a deal id and may show only a floor
  band (高層/中層/低層).
- block grid  `/history[/stateno-S][/blockno-B]`: phase and block menus plus one card per flat
  with its saleable area and number of deals since 2018.
- per-flat page  `/history/.../unitid-N`: the flat's areas and every deal since 2018.

Pages are untrusted data: they are parsed as text only. Nothing in them is executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser

from hkestate.normalise import parse_area, parse_price_hkd, parse_rooms

BASE = "https://www.28hse.com"
SOURCE = "hse28"

_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def canonical_url(url: str) -> str:
    """Percent-encode non-ASCII path characters so one page always has one cache key."""
    return quote(url, safe=":/?=&%-_.~#+")


@dataclass(frozen=True)
class EstateRef:
    name: str  # as 28Hse writes it in the URL
    id: int

    @property
    def base_url(self) -> str:
        return canonical_url(f"{BASE}/estate/detail/{self.name}-{self.id}")

    def list_url(self, page: int = 1) -> str:
        suffix = "/transaction" if page <= 1 else f"/transaction/page-{page}"
        return self.base_url + suffix

    def history_url(self, stateno: int | None = None, blockno: int | None = None) -> str:
        url = self.base_url + "/history"
        if stateno is not None:
            url += f"/stateno-{stateno}"
        if blockno is not None:
            url += f"/blockno-{blockno}"
        return url


def _to_date(text: str | None) -> date | None:
    m = _DATE.search(text or "")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _int_in(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text or "")
    return int(m.group(1)) if m else None


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def deal_type_of(label: str) -> str:
    if "註冊處" in label:
        return "registry"
    if "市場" in label:
        return "market"
    return "unknown"


def split_location(text: str, estate_name: str) -> tuple[str, str, str]:
    """'太古城 安盛台 建安閣 30樓 C室' -> ('安盛台 建安閣', '30樓', 'C'). The estate name prefix is
    dropped when present; the last two tokens are floor and flat."""
    t = _squash(text)
    if estate_name and t.startswith(estate_name + " "):
        t = t[len(estate_name) + 1 :]
    tokens = t.split(" ")
    flat = tokens[-1]
    flat = flat[:-1] if flat.endswith("室") else flat
    floor = tokens[-2] if len(tokens) >= 2 else ""
    location = " ".join(tokens[:-2])
    return location, floor, flat


# ---- deal list pages ----------------------------------------------------------------------


@dataclass
class ListRow:
    unit_key: str | None  # 28Hse unit id (Land Registry deals)
    deal_key: str | None  # 28Hse deal id (agency-reported deals)
    stateno: int | None
    blockno: int | None
    estate_name: str
    location: str  # phase and block text, e.g. "高山台 龍山閣"
    floor_text: str
    flat: str
    deal_date: date | None
    deal_type: str
    labels: list[str]
    rooms: int | None
    saleable_sqft: float | None
    price_hkd: int | None
    gain_pct: float | None
    href: str

    @property
    def with_parking(self) -> bool:
        return any("車位" in lab for lab in self.labels)

    @property
    def first_hand(self) -> bool:
        return any("一手" in lab for lab in self.labels)

    @property
    def source_id(self) -> str:
        if self.deal_key:
            return f"deal-{self.deal_key}"
        return f"u{self.unit_key}:{self.deal_date.isoformat() if self.deal_date else ''}"


@dataclass
class ListPage:
    rows: list[ListRow]
    next_url: str | None


def _parse_gain(text: str) -> float | None:
    m = re.search(r"([+-])\s*([\d.]+)\s*%", text or "")
    if not m:
        return None
    value = float(m.group(2))
    return -value if m.group(1) == "-" else value


def parse_list_page(html: str) -> ListPage:
    tree = LexborHTMLParser(html)
    rows = []
    for tr in tree.css("tr[unit-id]"):
        link = tr.css_first("div.header a")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        header = tr.css_first("div.header")
        estate_name = _squash(header.text(deep=False)) if header else ""
        estate_name = estate_name.split("|")[0].strip()
        location, floor, flat = split_location(link.text(), "")
        labels = [_squash(d.text()) for d in tr.css("div.labels div.label")]
        date_label = next((lab for lab in labels if _DATE.search(lab)), "")
        other = [lab for lab in labels if lab != date_label]
        tds = tr.css("td")
        area_text = _squash(tds[1].text()) if len(tds) > 1 else ""
        price_node = tr.css_first("div.price")
        unit_key = tr.attributes.get("unit-id") or None
        deal_m = re.search(r"/deal-(\d+)", href)
        rows.append(
            ListRow(
                unit_key=unit_key,
                deal_key=deal_m.group(1) if deal_m else None,
                stateno=_int_in(r"stateno-(\d+)", href),
                blockno=_int_in(r"blockno-(\d+)", href),
                estate_name=estate_name,
                location=location,
                floor_text=floor,
                flat=flat,
                deal_date=_to_date(date_label),
                deal_type=next(
                    (deal_type_of(lab) for lab in other if deal_type_of(lab) != "unknown"),
                    "unknown",
                ),
                labels=other,
                rooms=next((parse_rooms(lab) for lab in other if "房" in lab), None),
                saleable_sqft=parse_area(area_text)[1],
                price_hkd=parse_price_hkd(price_node.text()) if price_node else None,
                gain_pct=_parse_gain(tds[-1].text()) if tds else None,
                href=canonical_url(href),
            )
        )
    next_url = None
    nxt = tree.css_first('link[rel="next"]') or tree.css_first('a[rel="next"]')
    if nxt is not None and nxt.attributes.get("href"):
        next_url = canonical_url(nxt.attributes["href"])
    return ListPage(rows=rows, next_url=next_url)


# ---- block grid pages -----------------------------------------------------------------------


@dataclass
class GridUnit:
    unit_key: str
    stateno: int | None
    blockno: int | None
    floor_text: str
    flat: str
    saleable_sqft: float | None
    deal_count: int
    last_price_hkd: int | None
    last_date: date | None
    rooms: int | None
    url: str


@dataclass
class HistoryPage:
    phases: list[tuple[int, str]]
    blocks: list[tuple[int, str]]
    current_state: int | None
    current_block: int | None
    units: list[GridUnit] = field(default_factory=list)


def _menu(tree, selector: str) -> list[tuple[int, str]]:
    out = []
    for a in tree.css(selector):
        value = a.attributes.get("data-value")
        if value and value.isdigit():
            out.append((int(value), _squash(a.text())))
    return out


def _input_int(tree, name: str) -> int | None:
    node = tree.css_first(f'input[name="{name}"]')
    value = node.attributes.get("value") if node else None
    return int(value) if value and value.isdigit() else None


def parse_history_page(html: str) -> HistoryPage:
    tree = LexborHTMLParser(html)
    page = HistoryPage(
        phases=_menu(tree, "a.stateItem"),
        blocks=_menu(tree, "a.blockItem"),
        current_state=_input_int(tree, "deal_trend_by_unit_state_no"),
        current_block=_input_int(tree, "deal_trend_by_unit_block_no"),
    )
    for card in tree.css("div.deal_trend_unit_card_mobile"):
        key = card.attributes.get("unit-id")
        link = card.css_first("a.unitRecordUrl")
        if not key or link is None:
            continue
        href = link.attributes.get("href") or ""
        tokens = _squash(link.text()).split(" ")
        floor = tokens[0] if tokens else ""
        floor = floor[:-1] if floor.endswith("樓") else floor
        flat = tokens[-1] if len(tokens) > 1 else ""
        body = card.html or ""
        area = re.search(r"<div>\s*([\d,]+)\s*呎\s*</div>", body)
        price = re.search(r"<div>\s*(\$[\d,.]+\s*(?:萬|億)[^<]*)</div>", body)
        rooms = re.search(r"(\d+\s*房|開放式)", body)
        page.units.append(
            GridUnit(
                unit_key=key,
                stateno=_int_in(r"stateno-(\d+)", href),
                blockno=_int_in(r"blockno-(\d+)", href),
                floor_text=floor,
                flat=flat,
                saleable_sqft=float(area.group(1).replace(",", "")) if area else None,
                deal_count=_int_in(r"(\d+)\s*筆成交", body) or 0,
                last_price_hkd=parse_price_hkd(price.group(1)) if price else None,
                last_date=_to_date(re.search(r"日期:\s*([\d-]+)", body).group(1))
                if re.search(r"日期:\s*([\d-]+)", body)
                else None,
                rooms=parse_rooms(rooms.group(1)) if rooms else None,
                url=canonical_url(href),
            )
        )
    return page


# ---- per-flat pages ----------------------------------------------------------------------------


@dataclass
class UnitDeal:
    deal_date: date
    price_hkd: int | None
    deal_type: str
    deal_label: str


@dataclass
class UnitPage:
    title: str
    phase_name: str | None
    block_name: str | None
    saleable_sqft: float | None
    gross_sqft: float | None
    rooms: int | None
    deal_count: int | None
    deals: list[UnitDeal]


def parse_unit_page(html: str) -> UnitPage:
    tree = LexborHTMLParser(html)
    h1 = tree.css_first("h1")
    info: dict[str, str] = {}
    for tr in tree.css("table.compact tr"):
        tds = tr.css("td")
        if len(tds) == 2:
            info[_squash(tds[0].text()).rstrip(":：")] = _squash(tds[1].text())
    deals = []
    for h3 in tree.css("h3.date"):
        d = _to_date(h3.text())
        if d is None:
            continue
        # The deal table is the next sibling <div> after the h3 (and a divider).
        node = h3.next
        table = None
        while node is not None and table is None:
            if node.tag == "h3":
                break
            if node.tag == "div":
                table = node.css_first("table.myTablePair")
            node = node.next
        price = table.css_first("div.deal_price") if table else None
        dtype = table.css_first("div.deal_type") if table else None
        label = _squash(dtype.text()) if dtype else ""
        deals.append(
            UnitDeal(
                deal_date=d,
                price_hkd=parse_price_hkd(price.text()) if price else None,
                deal_type=deal_type_of(label),
                deal_label=label,
            )
        )
    count = info.get("成交宗數")
    return UnitPage(
        title=_squash(h1.text()) if h1 else "",
        phase_name=info.get("屋苑期數"),
        block_name=info.get("屋苑座數"),
        saleable_sqft=parse_area(info.get("實用面積"))[1],
        gross_sqft=parse_area(info.get("建築面積"))[1],
        rooms=parse_rooms(info.get("房間數量")),
        deal_count=int(count) if count and count.isdigit() else None,
        deals=deals,
    )
