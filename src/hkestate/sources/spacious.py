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


# ---- building pages (territory) ----------------------------------------------------------------
#
# A neighbourhood's search pages repeat after the first (paging beyond page 1 returns the same
# cards), so a neighbourhood shows only its first ~14 listings. A building's own sale page
# (`/zh-tw/香港/n/<id>-<name>/b/<id>--<name>/買樓`) shows that building's listings, so the
# territory run reads those. The site's sitemap (declared in its robots.txt) lists them all
# (about 33,000 residential buildings).

SITEMAP_INDEX = ("http://spacious-rails-production.s3.us-east-1.amazonaws.com/sitemaps/"
                 "www.spacious.hk/sitemap.xml.gz")
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_BUILDING_SALE = re.compile(
    r"^/zh-tw/%E9%A6%99%E6%B8%AF/n/(?P<nid>\d+)-[^/]+(?:/[^/]+)?/b/(?P<bid>\d+)--[^/]+"
    r"/%E8%B2%B7%E6%A8%93$"
)  # /zh-tw/香港/n/<id>-<name>[/<sub-area>]/b/<id>--<name>/買樓
DDL = [
    """
    CREATE TABLE IF NOT EXISTS spacious_building (
        building_id  INTEGER PRIMARY KEY,   -- Spacious's building id
        neighbourhood_id INTEGER,
        url          VARCHAR,               -- the building's sale page
        added_on     DATE,
        last_read    DATE,
        last_count   INTEGER                -- sale listings on the page when last read
    )""",
    """
    CREATE TABLE IF NOT EXISTS spacious_building_read (
        building_id  INTEGER NOT NULL,
        read_date    DATE NOT NULL,
        PRIMARY KEY (building_id, read_date)
    )""",
]


@dataclass(frozen=True)
class BuildingPage:
    neighbourhood_id: int
    building_id: int
    url: str


def gunzip_text(data: bytes) -> str:
    import gzip

    try:
        data = gzip.decompress(data)
    except OSError:
        pass  # already plain text
    return data.decode("utf-8", errors="replace")


def sitemap_children(xml: str) -> list[str]:
    return [u for u in _LOC.findall(xml) if u.endswith(".xml.gz") or u.endswith(".xml")]


def building_sale_pages(xml: str) -> list[BuildingPage]:
    """Residential sale pages of buildings in a sitemap (Traditional Chinese). Commercial
    pages (`/zh-tw/工商舖/...`), rentals, transactions and other languages are skipped."""
    from urllib.parse import quote, urlsplit

    out = []
    for loc in _LOC.findall(xml):
        path = quote(unquote(urlsplit(loc).path), safe="/%")
        m = _BUILDING_SALE.match(path)
        if m:
            out.append(BuildingPage(int(m.group("nid")), int(m.group("bid")),
                                    BASE + path))
    return out


def ensure_tables(con) -> None:
    for ddl in DDL:
        con.execute(ddl)


def add_buildings(con, pages: list[BuildingPage], day) -> int:
    have = {r[0] for r in con.execute("SELECT building_id FROM spacious_building").fetchall()}
    new = [(p.building_id, p.neighbourhood_id, p.url, day) for p in pages
           if p.building_id not in have]
    if new:
        con.executemany("INSERT INTO spacious_building (building_id, neighbourhood_id, url, "
                        "added_on) VALUES (?, ?, ?, ?)", new)
    return len(new)


def plan_reads(con, budget: int) -> list[tuple[int, str]]:
    """Buildings to read this run: those that had listings first (oldest read first), then
    never-read ones, then the longest unread."""
    rows = con.execute(
        """
        SELECT building_id, url FROM spacious_building
        ORDER BY (coalesce(last_count, 0) > 0) DESC,   -- had listings: every run
                 (last_read IS NULL) DESC,             -- then never read
                 last_read, building_id
        LIMIT ?""", [budget]).fetchall()
    return rows


def record_read(con, building_id: int, day, count: int) -> None:
    con.execute("UPDATE spacious_building SET last_read = ?, last_count = ? "
                "WHERE building_id = ?", [day, count, building_id])
    con.execute("INSERT OR IGNORE INTO spacious_building_read VALUES (?, ?)",
                [building_id, day])


MISSED_READS_TO_DELIST = 3


def mark_delisted(con, day) -> int:
    """Delist territory Spacious listings missing from 3 reads of their building page since
    they were last seen (a page not read that day does not count)."""
    rows = con.execute(
        """
        SELECT l.listing_id FROM listing l
        JOIN (SELECT listing_id, arg_max(source_building_key, fetched_at) AS bk
              FROM listing_raw WHERE source = 'spacious' GROUP BY 1) r USING (listing_id)
        WHERE l.source = 'spacious' AND l.group_name = 'territory' AND l.status = 'active'
          AND (SELECT count(*) FROM spacious_building_read d
               WHERE d.building_id = try_cast(r.bk AS INTEGER)
                 AND d.read_date > l.last_seen AND d.read_date <= ?) >= ?
        """, [day, MISSED_READS_TO_DELIST]).fetchall()
    for (lid,) in rows:
        con.execute("UPDATE listing SET status = 'delisted', delisted_on = ? "
                    "WHERE source = 'spacious' AND listing_id = ?", [day, lid])
        con.execute(
            "INSERT OR REPLACE INTO listing_snapshot (source, listing_id, snapshot_date, "
            "building_id, floor_band, flat, price_hkd, saleable_sqft, status) "
            "SELECT source, listing_id, ?, building_id, floor_band, flat, price_hkd, "
            "saleable_sqft, 'delisted' FROM listing WHERE source = 'spacious' "
            "AND listing_id = ?", [day, lid])
    return len(rows)
