"""28Hse (www.28hse.com) sale and rent listings.

Search pages, 15 listings each, can be filtered to one estate:
`/buy/apartment/<area>/<district>/c<estate id>[/page-N]` (`/rent/...` for rentals). The
estate id is the same one the transaction pages use, and <area>/<district> come from the
site's own district menu (e.g. 太古城 a1/dg9). Each card has the block, floor band, flat,
area, price, rooms, facing and feature tags, so the daily run reads search pages only and
opens a detail page (`/buy/apartment/property-<id>`) just for new listings or a changed price.

Titles are free text written by agents and often contain names or phone numbers: they are
read only for keyword flags and never stored. Agency names and the contact block are never
read. Pages are untrusted data, parsed as text only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from selectolax.lexbor import LexborHTMLParser

from hkestate.listing_types import Listing
from hkestate.normalise import parse_area, parse_price_hkd, parse_rooms
from hkestate.sources.hse28 import BASE, canonical_url

SOURCE = "hse28"

_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_BANDS = ("高層", "中層", "低層")


def estate_search_url(area_path: str, estate_id: int, kind: str, page: int = 1) -> str:
    """Search page of one estate, e.g. ('a1/dg9', 1140, 'sale') for 太古城."""
    section = "buy" if kind == "sale" else "rent"
    url = f"{BASE}/{section}/apartment/{area_path}/c{estate_id}"
    return url if page <= 1 else f"{url}/page-{page}"


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _to_date(text: str | None) -> date | None:
    m = _DATE.search(text or "")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def split_unit_desc(text: str) -> tuple[str, str | None, str | None]:
    """'2A座 高層  A室' -> ('2A座', '高層', 'A'). Returns (building text, floor, flat); the
    floor is a band (高層/中層/低層) or 'N樓', and either may be missing."""
    tokens = _squash(text).split(" ") if _squash(text) else []
    flat = floor = None
    if tokens and tokens[-1].endswith("室"):
        flat = tokens.pop()[:-1] or None
    elif len(tokens) == 1 and re.fullmatch(r"[A-Z0-9]{1,3}", tokens[0]):
        flat = tokens.pop()  # only a flat letter is shown
    if tokens and (tokens[-1] in _BANDS or re.fullmatch(r"\d+樓|[GL]/?F|地下", tokens[-1])):
        floor = tokens.pop()
    return " ".join(tokens), floor, flat


def _rooms_and_facing(labels: list[str]) -> tuple[int | None, str | None, list[str]]:
    rooms = facing = None
    rest = []
    for lab in labels:
        if rooms is None and re.search(r"\d+\s*房|開放式", lab):
            rooms = parse_rooms(lab)
        elif re.fullmatch(r"\d+\s*浴室", lab):
            continue
        elif facing is None and re.fullmatch(r"向[東南西北]{1,2}", lab):
            facing = lab[1:]
        else:
            rest.append(lab)
    return rooms, facing, rest


@dataclass
class SearchPage:
    listings: list[Listing]
    next_url: str | None


def parse_search_page(html: str, kind: str) -> SearchPage:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for item in tree.css("div.property_item"):
        link = item.css_first("div.header a.detail_page") or item.css_first("a.detail_page")
        if link is None:
            continue
        lid = link.attributes.get("attr1") or ""
        if not lid:
            m = re.search(r"property-(\d+)", link.attributes.get("href") or "")
            lid = m.group(1) if m else ""
        if not lid:
            continue
        title = _squash(link.text())
        estate = estate_key = None
        for a in item.css("div.district_area a"):
            m = re.search(r"/c(\d+)$", a.attributes.get("href") or "")
            if m:
                estate, estate_key = _squash(a.text()), m.group(1)
        unit = item.css_first("span.unit_desc")
        building, floor, flat = split_unit_desc(unit.text() if unit else "")
        saleable = gross = None
        for div in item.css("div.areaUnitPrice div"):
            basis, value = parse_area(div.text())  # '實用面積: 580 呎 @17,069 元'
            if basis == "saleable":
                saleable = value
            elif basis == "gross":
                gross = value
        price_node = item.css_first("div.extra div.label.red") or item.css_first("div.red.label")
        price = parse_price_hkd(price_node.text()) if price_node else None
        labels = [_squash(d.text()) for d in item.css("div.tagLabels div.label")]
        labels = [x for part in labels for x in [s.strip() for s in part.split(",")] if x]
        rooms, facing, features = _rooms_and_facing(labels)
        out.append(
            Listing(
                source=SOURCE,
                listing_id=lid,
                kind=kind,
                url=canonical_url(link.attributes.get("href") or ""),
                estate_name=estate,
                building_text=building or None,
                floor_text=floor,
                flat=flat,
                price_hkd=price,
                saleable_sqft=saleable,
                gross_sqft=gross,
                rooms=rooms,
                facing=facing,
                features=features,
                source_estate_key=estate_key,
                text_for_flags=" ".join([title, *features]),
            )
        )
    next_url = None
    nxt = tree.css_first('link[rel="next"]') or tree.css_first('a[rel="next"]')
    if nxt is not None and nxt.attributes.get("href"):
        next_url = canonical_url(nxt.attributes["href"])
    return SearchPage(out, next_url)


def parse_detail(html: str, url: str, kind: str) -> Listing:
    tree = LexborHTMLParser(html)
    m = re.search(r"property-(\d+)", url)
    pairs: dict[str, list[str]] = {}
    for tr in tree.css("table.tablePair tr"):
        left = tr.css_first("td.table_left")
        right = tr.css_first("td.table_right")
        if left is None or right is None:
            continue
        values = [_squash(d.text()) for d in right.css("div.pairValue, div.pairSubValue")]
        pairs[_squash(left.text())] = values
    price_text = (pairs.get("售價") or pairs.get("租金") or pairs.get("租價") or [""])[0]
    unit_text = next((v for vs in pairs.values() for v in vs if v.startswith("座數及單位")), "")
    unit_text = unit_text.split(":", 1)[-1].split("：", 1)[-1]
    # '安盛台 寧安閣 高層 (20-30/30樓) C室': the bracket is the band's floors / top floor
    rng = re.search(r"\((\d+-\d+/\d+)樓\)", unit_text)
    building, floor, flat = split_unit_desc(re.sub(r"\(\d+-\d+/\d+樓\)", " ", unit_text))
    age_text = next((v for vs in pairs.values() for v in vs if v.startswith("屋苑樓齡")), "")
    age = re.search(r"(\d+)\s*年", age_text)
    fee_text = (pairs.get("每月管理費") or [""])[0]
    fee = re.search(r"\$\s*([\d,]+)", fee_text)
    date_node = tree.css_first("div.propertyDate")
    date_text = date_node.text() if date_node else ""
    posted = re.search(r"刊登:\s*(\d{4}-\d{2}-\d{2})", date_text)
    updated = re.search(r"更新:\s*(\d{4}-\d{2}-\d{2})", date_text)
    features = [_squash(d.text()) for d in tree.css("div.ui.labels div.basic.label")]
    desc = tree.css_first('meta[name="description"]')
    floor_band = (pairs.get("單位樓層") or [None])[0]
    return Listing(
        source=SOURCE,
        listing_id=m.group(1) if m else url,
        kind=kind,
        url=url,
        building_text=building or None,
        floor_text=floor or floor_band,
        floor_range=rng.group(1) if rng else None,
        flat=flat,
        price_hkd=parse_price_hkd(price_text),
        saleable_sqft=parse_area((pairs.get("實用面積") or [""])[0])[1],
        gross_sqft=parse_area((pairs.get("建築面積") or [""])[0])[1],
        rooms=parse_rooms(" ".join(pairs.get("房間及浴室") or [])),
        facing=(pairs.get("座向(客廳)") or [None])[0],
        age_years=int(age.group(1)) if age else None,
        posted_date=_to_date(posted.group(1)) if posted else None,
        updated_date=_to_date(updated.group(1)) if updated else None,
        features=[f for f in features if f],
        extra={"mgmt_fee_hkd": int(fee.group(1).replace(",", ""))} if fee else {},
        text_for_flags=" ".join(
            [(desc.attributes.get("content") or "") if desc else "", *features]
        ),
    )
