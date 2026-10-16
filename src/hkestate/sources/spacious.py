"""Spacious (www.spacious.hk) sale and rent listings, a secondary source.

We read the Traditional Chinese neighbourhood search pages
(`/zh-tw/香港/n/<id>-<name>/買樓`, next pages `/zh-tw/香港/<id>-<name>/for-sale/page/N`),
which robots.txt allows; the territory-wide listing pages beyond page 100 and `/api` are
disallowed and never used. Each card gives the building name in Chinese, saleable and gross
area, rooms and price. Spacious shows no floor, and a flat letter only when it is in the
listing's address (`.../15076453-flat-f-3-beds-2-baths`), so its listings match to a
building only.

Cards have a "contact" button but no names or phones in the HTML we read; agent details on
detail pages are never fetched. Pages are untrusted data, parsed as text only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin

from selectolax.lexbor import LexborHTMLParser

from hkestate.listing_types import Listing
from hkestate.normalise import parse_area, parse_price_hkd, parse_rooms

SOURCE = "spacious"
BASE = "https://www.spacious.hk"


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@dataclass
class SearchPage:
    listings: list[Listing]
    next_url: str | None


def parse_search_page(html: str, kind: str, page_url: str) -> SearchPage:
    tree = LexborHTMLParser(html)
    out: list[Listing] = []
    for card in tree.css("div.cell--listings--row--for-searching--default[data-listing-id]"):
        lid = card.attributes.get("data-listing-id") or ""
        path = card.attributes.get("data-full-listing-page-path") or ""
        if not lid:
            continue
        link = card.css_first("a.cell--listings--row--for-searching__building-link")
        building = _squash(link.text()) if link else None
        bkey = None
        if link is not None:
            m = re.search(r"/b/(\d+)--", link.attributes.get("href") or "")
            bkey = m.group(1) if m else None
        saleable = gross = None
        for node in card.css("div.cell--listings--summary--icons--table--default--item-text--item"):
            basis, value = parse_area(node.text())  # '實用 580 呎' / '建築 675 呎'
            if basis == "saleable":
                saleable = value
            elif basis == "gross":
                gross = value
        rooms = None
        for node in card.css("div.cell--listings--summary--icons--table--default--item-text"):
            t = _squash(node.text())
            if re.fullmatch(r"\d+\s*房|開放式", t):
                rooms = parse_rooms(t)
        price_node = card.css_first(
            "div.cell--listings--row--for-searching--default__section-detail--panel__price"
        )
        flat_m = re.search(r"/\d+-flat-([a-z0-9]{1,3})-", unquote(path), re.IGNORECASE)
        out.append(
            Listing(
                source=SOURCE,
                listing_id=lid,
                kind=kind,
                url=urljoin(BASE, path) if path else page_url,
                building_text=building,
                flat=flat_m.group(1).upper() if flat_m else None,
                price_hkd=parse_price_hkd(price_node.text()) if price_node else None,
                saleable_sqft=saleable,
                gross_sqft=gross,
                rooms=rooms,
                source_building_key=bkey,
                text_for_flags=building or "",
            )
        )
    # pagination links carry rel="next nofollow"
    next_url = None
    for a in tree.css("link[rel], a[rel]"):
        if "next" in (a.attributes.get("rel") or "").split() and a.attributes.get("href"):
            next_url = urljoin(BASE, a.attributes["href"])
            break
    return SearchPage(out, next_url)
