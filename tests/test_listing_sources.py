"""Listing parsers (Centanet, 28Hse, Spacious) and listing storage, on synthetic fixtures.

The fixtures carry AGENT_NAME_PLACEHOLDER / PHONE_PLACEHOLDER / COMPANY_PLACEHOLDER where
the real pages show agent names, phones and agency names: none of them may reach the database.
"""

import json
import re
from datetime import date, datetime
from pathlib import Path

from hkestate import db, listings
from hkestate.sources import centanet, hse28_listings, spacious

FIX = Path(__file__).parent / "fixtures"
PLACEHOLDERS = ("AGENT_NAME_PLACEHOLDER", "PHONE_PLACEHOLDER", "COMPANY_PLACEHOLDER")


def read(*parts) -> str:
    return (FIX.joinpath(*parts)).read_text(encoding="utf-8")


# ---- Centanet ------------------------------------------------------------------------------


def test_centanet_sitemap_keeps_only_pilot_estates():
    prefixes = {"太古城": "taikoo-shing", "高雲臺": "goldwin-heights", "日出康城": "lohas-park"}
    entries = centanet.sitemap_entries(read("centanet", "sitemap_detail.xml"), prefixes)
    got = {(e.listing_id, e.kind, e.group) for e in entries}
    assert got == {
        ("ZZA001", "sale", "taikoo-shing"),
        ("ZZA002", "rent", "taikoo-shing"),
        ("ZZA004", "sale", "goldwin-heights"),
        ("ZZA005", "sale", "lohas-park"),
    }  # 太古城中心 (a different estate) and 碧堤半島 are left out
    first = next(e for e in entries if e.listing_id == "ZZA001")
    assert first.url.startswith("https://hk.centanet.com/findproperty/detail/")
    assert first.url.endswith("_ZZA001?theme=buy")


def test_centanet_detail_page():
    url = "https://hk.centanet.com/findproperty/detail/x_ZZA001?theme=buy"
    lst = centanet.parse_detail(read("centanet", "detail_sale.html"), url)
    assert lst.source == "centanet"
    assert lst.listing_id == "ZZA001"
    assert lst.kind == "sale"
    assert lst.estate_name == "太古城"
    assert lst.building_text == "太古城 海景花園(東) 美菊閣 (39座)"
    assert lst.floor_text == "高層"
    assert lst.flat == "B"
    assert lst.price_hkd == 12_800_000
    assert lst.saleable_sqft == 688
    assert lst.gross_sqft == 801
    assert lst.rooms == 3
    assert lst.age_years == 42
    assert lst.facing == "東南"
    assert lst.posted_date == date(2026, 10, 10)
    assert lst.updated_date == date(2026, 10, 15)
    assert "海景" in lst.features
    assert lst.source_building_key == "1-OVFSQRFARJ"  # Centadata block code
    assert "全新裝修" in lst.text_for_flags  # listing blurb: flags only, never stored
    for p in PLACEHOLDERS:
        assert p not in repr(lst)


# ---- 28Hse ---------------------------------------------------------------------------------


def test_hse28_search_page():
    page = hse28_listings.parse_search_page(read("hse28", "search_page.html"), "sale")
    assert page.next_url == "https://www.28hse.com/buy/apartment/a1/dg9/c1140/page-2"
    a, b = page.listings
    assert a.listing_id == "9000001"
    assert a.url == "https://www.28hse.com/buy/apartment/property-9000001"
    assert a.kind == "sale"
    assert a.estate_name == "太古城"
    assert a.source_estate_key == "1140"
    assert a.building_text == "安盛台 寧安閣"
    assert (a.floor_text, a.flat) == ("高層", "C")
    assert a.saleable_sqft == 580 and a.gross_sqft is None
    assert a.price_hkd == 9_900_000
    assert a.rooms == 3
    assert a.facing == "東南"
    assert "望海景" in a.features
    assert "非凶宅" in a.text_for_flags
    assert (b.building_text, b.floor_text, b.flat) == ("金殿台 漢宮閣", "低層", None)
    assert b.gross_sqft == 800 and b.saleable_sqft is None
    assert b.price_hkd == 11_600_000
    for lst in page.listings:
        for p in ("COMPANY_PLACEHOLDER",):
            assert p not in repr(lst)


def test_hse28_rent_price():
    html = read("hse28", "search_page.html").replace("售 $990 萬元", "租 $28,000 元")
    a = hse28_listings.parse_search_page(html, "rent").listings[0]
    assert a.kind == "rent" and a.price_hkd == 28_000


def test_hse28_listing_detail():
    url = "https://www.28hse.com/buy/apartment/property-9000001"
    d = hse28_listings.parse_detail(read("hse28", "listing_detail.html"), url, "sale")
    assert d.listing_id == "9000001"
    assert d.price_hkd == 9_900_000
    assert (d.saleable_sqft, d.gross_sqft) == (580, 675)
    assert d.building_text == "安盛台 寧安閣"
    assert (d.floor_text, d.flat) == ("高層", "C")
    assert d.floor_range == "20-30/30"
    assert d.facing == "東南"
    assert d.rooms == 3
    assert d.age_years == 42
    assert d.posted_date == date(2026, 10, 14)
    assert d.updated_date == date(2026, 10, 16)
    assert d.extra["mgmt_fee_hkd"] == 1800
    assert "望海景" in d.features
    for p in PLACEHOLDERS:
        assert p not in repr(d)


def test_hse28_unit_desc_shapes():
    f = hse28_listings.split_unit_desc
    assert f("2A座 高層  A室") == ("2A座", "高層", "A")
    assert f("C座 (凱葶閣) 中層  3室") == ("C座 (凱葶閣)", "中層", "3")
    assert f("One Innovale Cabanna E座 高層") == ("One Innovale Cabanna E座", "高層", None)
    assert f("10座 25樓 H室") == ("10座", "25樓", "H")
    assert f("A") == ("", None, "A")


def test_hse28_estate_listing_urls():
    url = hse28_listings.estate_search_url("a1/dg9", 1140, "sale")
    assert url == "https://www.28hse.com/buy/apartment/a1/dg9/c1140"
    assert hse28_listings.estate_search_url("a1/dg9", 1140, "rent").startswith(
        "https://www.28hse.com/rent/apartment/"
    )


# ---- Spacious -------------------------------------------------------------------------------


def test_spacious_search_page():
    base = "https://www.spacious.hk/zh-tw/x/n/48-x/買樓"
    page = spacious.parse_search_page(read("spacious", "search_page.html"), "sale", base)
    assert page.next_url == (
        "https://www.spacious.hk/zh-tw/%E9%A6%99%E6%B8%AF/48-%E5%A4%AA%E5%8F%A4%E5%9F%8E"
        "/for-sale/page/2"
    )
    a, b, c = page.listings
    assert a.listing_id == "19000001"
    assert a.url.startswith("https://www.spacious.hk/zh-tw/")
    assert a.building_text == "太古城 星輝台 海星閣 (48座)"
    assert a.source_building_key == "53943"
    assert (a.saleable_sqft, a.gross_sqft, a.rooms) == (580, 675, 3)
    assert a.price_hkd == 9_890_000
    assert a.floor_text is None  # Spacious shows no floor
    assert b.flat == "F" and b.rooms == 0 and b.saleable_sqft == 1015
    assert c.building_text == "康怡花園 A座"


# ---- storage ------------------------------------------------------------------------------

# Column names that would hold contact details. None may exist in any listing table.
PRIVATE = re.compile(r"agent|phone|tel\b|contact|licen[cs]e|whatsapp|wechat|company|lister")


def _db(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_db(con)
    return con


def test_no_agent_or_phone_columns(tmp_path):
    con = _db(tmp_path)
    for table in ("listing_raw", "listing", "listing_snapshot", "risk_flag"):
        cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
        assert cols, table
        bad = [c for c in cols if PRIVATE.search(c)]
        assert not bad, f"{table} has contact columns {bad}"


def test_store_listings_never_stores_contact_text(tmp_path):
    con = _db(tmp_path)
    url = "https://hk.centanet.com/findproperty/detail/x_ZZA001?theme=buy"
    rows = [
        centanet.parse_detail(read("centanet", "detail_sale.html"), url),
        *hse28_listings.parse_search_page(read("hse28", "search_page.html"), "sale").listings,
    ]
    added = listings.store_raw(con, rows, datetime(2026, 10, 16, 8), group="taikoo-shing")
    assert added == 3
    dump = json.dumps(con.execute("SELECT * FROM listing_raw").fetchall(), default=str,
                      ensure_ascii=False)
    for p in PLACEHOLDERS:
        assert p not in dump
    assert "非凶宅" not in dump  # free text is used for flags only
    keys = set()
    for (raw,) in con.execute("SELECT raw_json FROM listing_raw").fetchall():
        keys |= set(json.loads(raw))
    assert not [k for k in keys if PRIVATE.search(k)]


def test_store_listings_rerun_adds_nothing_and_merges_detail(tmp_path):
    con = _db(tmp_path)
    page = hse28_listings.parse_search_page(read("hse28", "search_page.html"), "sale")
    t1 = datetime(2026, 10, 16, 8)
    assert listings.store_raw(con, page.listings, t1, group="taikoo-shing") == 2
    assert listings.store_raw(con, page.listings, t1.replace(hour=9), group="taikoo-shing") == 0
    url = "https://www.28hse.com/buy/apartment/property-9000001"
    detail = hse28_listings.parse_detail(read("hse28", "listing_detail.html"), url, "sale")
    assert listings.store_raw(con, [detail], t1.replace(hour=10), group="taikoo-shing") == 1
    # the next day's search card lacks the gross area: merged with the detail, no new version
    assert listings.store_raw(con, page.listings[:1], t1.replace(day=17), group="taikoo-shing") == 0
    latest = con.execute(
        "SELECT gross_sqft, floor_range FROM listing_raw WHERE listing_id = '9000001' "
        "ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()
    assert latest == (675, "20-30/30")
