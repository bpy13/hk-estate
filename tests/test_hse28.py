"""28Hse parsers, tested on trimmed synthetic fixtures in tests/fixtures/hse28/."""

from datetime import date
from pathlib import Path

import pytest

from hkestate.sources import hse28

FIX = Path(__file__).parent / "fixtures" / "hse28"


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


# ---- URLs ---------------------------------------------------------------------------------


def test_estate_urls_are_percent_encoded_and_stable():
    e = hse28.EstateRef("太古城", 1140)
    assert e.base_url == "https://www.28hse.com/estate/detail/%E5%A4%AA%E5%8F%A4%E5%9F%8E-1140"
    assert e.list_url(1) == e.base_url + "/transaction"
    assert e.list_url(3) == e.base_url + "/transaction/page-3"
    assert e.history_url() == e.base_url + "/history"
    assert e.history_url(stateno=2) == e.base_url + "/history/stateno-2"
    assert e.history_url(stateno=2, blockno=5) == e.base_url + "/history/stateno-2/blockno-5"
    assert e.history_url(blockno=1) == e.base_url + "/history/blockno-1"


def test_canonical_url_encodes_raw_chinese_hrefs():
    raw = "https://www.28hse.com/estate/detail/太古城-1140/history/stateno-1/blockno-18"
    assert hse28.canonical_url(raw) == (
        "https://www.28hse.com/estate/detail/%E5%A4%AA%E5%8F%A4%E5%9F%8E-1140"
        "/history/stateno-1/blockno-18"
    )
    # already encoded stays the same
    assert hse28.canonical_url(hse28.canonical_url(raw)) == hse28.canonical_url(raw)


# ---- deal list pages ----------------------------------------------------------------------


def test_list_page_rows():
    page = hse28.parse_list_page(read("list_page.html"))
    assert len(page.rows) == 3  # the fake row inside <script> is ignored
    r = page.rows[0]
    assert r.unit_key == "180707"
    assert (r.stateno, r.blockno) == (6, 58)
    assert r.location == "高山台 龍山閣"
    assert (r.floor_text, r.flat) == ("19樓", "A")
    assert r.deal_date == date(2026, 10, 14)
    assert r.deal_type == "registry"
    assert r.rooms == 2
    assert r.saleable_sqft == 530
    assert r.price_hkd == 8_750_000
    assert r.gain_pct == pytest.approx(169.2)
    assert r.source_id == "u180707:2026-10-14"
    assert r.with_parking is False


def test_list_page_parking_ground_floor_and_negative_gain():
    r = hse28.parse_list_page(read("list_page.html")).rows[1]
    assert r.floor_text == "地下"
    assert r.with_parking is True
    assert r.price_hkd == 20_435_000
    assert r.saleable_sqft == 1067
    assert r.gain_pct == pytest.approx(-22.7)
    assert r.rooms is None


def test_list_page_market_deal_has_deal_id_and_floor_band():
    r = hse28.parse_list_page(read("list_page.html")).rows[2]
    assert r.unit_key is None
    assert r.deal_key == "52882206"
    assert r.source_id == "deal-52882206"
    assert r.deal_type == "market"
    assert (r.stateno, r.blockno) == (None, None)
    assert r.location == "星輝台 衛星閣"
    assert (r.floor_text, r.flat) == ("低層", "D")
    assert r.gain_pct is None


def test_list_page_next_link():
    page = hse28.parse_list_page(read("list_page.html"))
    assert page.next_url.endswith("-1140/transaction/page-3")


def test_single_block_estate_last_page():
    page = hse28.parse_list_page(read("list_single_block_last.html"))
    assert page.next_url is None
    r = page.rows[0]
    assert (r.stateno, r.blockno, r.unit_key) == (None, 1, "48989")
    assert r.location == "高雲台"
    assert (r.floor_text, r.flat) == ("23樓", "A")
    assert r.deal_date == date(2021, 3, 12)


# ---- block grid pages -----------------------------------------------------------------------


def test_history_page_phases_and_blocks():
    page = hse28.parse_history_page(read("history_block.html"))
    assert page.phases == [(1, "安盛台"), (2, "金殿台")]
    assert page.blocks == [(18, "建安閣"), (31, "高安閣")]
    assert (page.current_state, page.current_block) == (1, 18)


def test_history_page_grid_units():
    units = hse28.parse_history_page(read("history_block.html")).units
    assert [u.unit_key for u in units] == ["172139", "172141", "172150"]
    a, c, g = units
    assert (a.floor_text, a.flat, a.saleable_sqft, a.deal_count) == ("30", "A", 580, 0)
    assert (c.deal_count, c.last_price_hkd, c.last_date, c.rooms) == (
        2,
        13_800_000,
        date(2023, 11, 9),
        3,
    )
    assert (g.floor_text, g.flat, g.saleable_sqft) == ("地下", "B", None)
    assert (a.stateno, a.blockno) == (1, 18)


# ---- per-flat pages ----------------------------------------------------------------------------


def test_unit_page():
    page = hse28.parse_unit_page(read("unit_page.html"))
    assert page.title == "太古城 安盛台 建安閣 30樓 C室"
    assert (page.phase_name, page.block_name) == ("安盛台", "建安閣")
    assert (page.saleable_sqft, page.gross_sqft, page.rooms, page.deal_count) == (703, 840, 3, 2)
    assert [(d.deal_date, d.price_hkd, d.deal_type) for d in page.deals] == [
        (date(2023, 11, 9), 13_800_000, "registry"),
        (date(2019, 4, 2), 12_500_000, "registry"),
    ]


def test_split_title_tail():
    assert hse28.split_location("太古城 安盛台 建安閣 30樓 C室", "太古城") == (
        "安盛台 建安閣",
        "30樓",
        "C",
    )
    assert hse28.split_location("高雲台 23樓 A室", "高雲臺") == ("高雲台", "23樓", "A")
    assert hse28.split_location("3期 翠湖居 6座 低層 D室", "") == ("3期 翠湖居 6座", "低層", "D")
