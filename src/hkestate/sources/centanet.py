"""Centanet (hk.centanet.com) sale and rent listings.

Centanet's search pages show only the first 24 results (the rest come from the disallowed
`/centanetapi/api/`), so live listings are found through its detail sitemap, which lists every
live listing: `/findproperty/detail/<estate>-<phase>-<block>_<id>?theme=buy|rent`. The slug
starts with the estate name, so the pilot's listings can be picked out without a request
per listing.

Detail pages are server-rendered. We read the page's JSON-LD blocks (plain JSON) and a few
labelled HTML fields. The page also shows agents' names, licence numbers and phones: those
elements are never read. The embedded `window.__NUXT__` script is never run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, unquote

from selectolax.lexbor import LexborHTMLParser

from hkestate.listing_types import Listing
from hkestate.normalise import parse_price_hkd, parse_rooms

SOURCE = "centanet"
BASE = "https://hk.centanet.com"
SITEMAP_URL = BASE + "/sitemap-index/findproperty/sitemap_TC_findproperty_detail.xml"

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_DETAIL = re.compile(
    r"/findproperty/detail/(?P<slug>[^?#]+)_(?P<id>[A-Za-z0-9]+)\?theme=(?P<theme>buy|rent)"
)
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class SitemapEntry:
    listing_id: str
    kind: str  # sale or rent
    url: str
    group: str  # pilot estate group
    slug: str


def detail_url(slug: str, listing_id: str, kind: str) -> str:
    theme = "buy" if kind == "sale" else "rent"
    return f"{BASE}/findproperty/detail/{quote(slug)}_{listing_id}?theme={theme}"


def sitemap_entries(xml: str, prefixes: dict[str, str],
                    default_group: str | None = None) -> list[SitemapEntry]:
    """Listings whose slug names one of `prefixes` (estate name -> group) as its estate.

    The estate name must be the whole first slug part ('太古城-...' or '太古城'), so
    '太古城中心' does not count as 太古城. Every other listing goes to
    `default_group` (the territory) if one is given, else it is skipped. The sitemap is
    parsed with a regex on <loc> only:
    it is plain text from the site and nothing in it is executed.
    """
    out: list[SitemapEntry] = []
    for loc in _LOC.findall(xml):
        m = _DETAIL.search(unquote(loc))
        if not m:
            continue
        slug = m.group("slug")
        first = slug.split("-")[0]
        group = prefixes.get(first) or default_group
        if group is None:
            continue
        kind = "sale" if m.group("theme") == "buy" else "rent"
        out.append(SitemapEntry(m.group("id"), kind, detail_url(slug, m.group("id"), kind),
                                group, slug))
    return out


def slug_text(url: str | None) -> str | None:
    """The estate, phase and block named in a detail link, as text: '.../尚悅-3座_ABC123?...'
    -> '尚悅 3座'. Many pages' titles give only the estate; the link still names the block."""
    m = _DETAIL.search(unquote(url or ""))
    return m.group("slug").replace("-", " ") if m else None


def _to_date(text: str | None) -> date | None:
    m = _DATE.search(text or "")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _num(text: str | None) -> float | None:
    m = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def _ld_blocks(tree) -> list[dict]:
    blocks = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        blocks += data if isinstance(data, list) else [data]
    return blocks


def split_addr(addr: str) -> tuple[str | None, str | None]:
    """'高層 B室' -> ('高層', 'B'); '中層' -> ('中層', None); '25樓 C室' -> ('25樓', 'C')."""
    tokens = _squash(addr).split(" ")
    floor = flat = None
    for t in tokens:
        if t.endswith("室"):
            flat = t[:-1] or None
        elif t:
            floor = t
    return floor, flat


def parse_detail(html: str, url: str) -> Listing:
    tree = LexborHTMLParser(html)
    m = _DETAIL.search(unquote(url))
    listing_id = m.group("id") if m else url.rsplit("_", 1)[-1].split("?")[0]
    kind = "rent" if "theme=rent" in url else "sale"

    ld = _ld_blocks(tree)
    listing_ld = next((b for b in ld if b.get("@type") == "RealEstateListing"), {})
    crumbs = next((b for b in ld if b.get("@type") == "BreadcrumbList"), {})
    # 主頁 / 買樓 / region / district... / estate / [phase] / block: the estate is the first
    # crumb whose link is an estate (code 2- or 3-) list page; fall back to the title.
    estate = None
    for i in crumbs.get("itemListElement") or []:
        if re.search(r"_[23]-[A-Z]+$", str(i.get("item") or "")):
            estate = _squash(i.get("name"))
            break

    h1 = tree.css_first("h1.info-title")
    building_text = floor = flat = None
    if h1 is not None:
        addr = h1.css_first("span.addr")
        addr_text = addr.text() if addr else ""
        full = _squash(h1.text())
        building_text = _squash(full[: len(full) - len(_squash(addr_text))]) if addr_text else full
        floor, flat = split_addr(addr_text)

    entity = listing_ld.get("mainEntity") or {}
    offers = listing_ld.get("offers") or {}
    price = offers.get("price")
    if not price:
        node = tree.css_first("div.price-number")
        price = parse_price_hkd(node.text()) if node else None

    saleable = gross = None
    for blk in tree.css("div.property-area div.area-blk"):
        value = _num((blk.css_first("span.area") or blk).text())
        if blk.css_first("span.sy-tag") is not None:
            saleable = value
        elif blk.css_first("span.jz-tag") is not None:
            gross = value
    if saleable is None:
        saleable = (entity.get("floorSize") or {}).get("value")

    info: dict[str, str] = {}
    for div in tree.css("div.info-num > div"):
        ps = div.css("p")
        if len(ps) >= 2 and "info-tag" in (ps[0].attributes.get("class") or ""):
            info[_squash(ps[0].text())] = _squash(ps[1].text())

    rooms = parse_rooms(info.get("間隔"))
    if rooms is None and isinstance(entity.get("numberOfRooms"), int):
        rooms = entity["numberOfRooms"]
    age = _num(info.get("樓齡"))

    features = [_squash(s.text()) for s in tree.css("div.property-tag .tags-info span")]
    page_text = tree.body.text() if tree.body else ""
    updated = re.search(r"更新日期：\s*(\d{4}-\d{2}-\d{2})", page_text)

    block_key = None
    est = tree.css_first("div.estimate-box-container a")
    if est is not None:
        km = re.search(r"typeCode=(1-[A-Z]+)", est.attributes.get("href") or "")
        block_key = km.group(1) if km else None

    # Listing blurbs written for this flat (keyword flags only). The agent cards next to
    # them (names, licences, phones) are never read.
    blurbs = [_squash(p.text()) for p in tree.css("p.desc-content")]

    return Listing(
        source=SOURCE,
        listing_id=listing_id,
        kind=kind,
        url=url,
        estate_name=estate,
        building_text=building_text,
        floor_text=floor,
        flat=flat,
        price_hkd=int(price) if price else None,
        saleable_sqft=float(saleable) if saleable else None,
        gross_sqft=gross,
        rooms=rooms,
        facing=info.get("座向") or None,
        age_years=int(age) if age is not None else None,
        posted_date=_to_date(listing_ld.get("datePosted")),
        updated_date=_to_date(updated.group(1)) if updated else None,
        features=[f for f in features if f],
        source_building_key=block_key,
        text_for_flags=" ".join([*features, *blurbs]),
        extra={"title": _squash(listing_ld.get("name"))} if listing_ld.get("name") else {},
    )
