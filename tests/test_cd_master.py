"""Territory-wide Centadata master list: sitemap, estate facts, exclusion rule, loading."""

import json
from datetime import date

import pytest

from hkestate import db
from hkestate.sources import cd_master as cm
from hkestate.sources import centadata as cd

SITEMAP = """<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
 <url><loc>https://hk.centanet.com/estate/%E6%A1%82%E5%9B%AD/2-AABKWPYAPE</loc></url>
 <url><loc>https://hk.centanet.com/estate/%E6%84%89%E6%99%AF%E6%B9%BE/3-LIDHTHXXHT</loc></url>
 <url><loc>https://hk.centanet.com/estate/x/not-a-code</loc></url>
</urlset>"""


def nuxt_page(*, estate="測試苑", phase="", block="1座", code="2-AAAAAAAAAA",\n              block_code="1-BBBBBBBBBB",
              estate_type="Normal", is_house=False, hos=False, prh=False, listings=True,
              op_date="1998-05-01T00:00:00", units=60, floors=30, deals=(), blocks=(),
              phases=(), parent=None):
    """A synthetic page in the shape Centadata serves (JSON is valid for the literal reader)."""
    sale = [{"hos_flag": hos, "prh_flag": prh, "scope": {"db": "沙田區"}}] if listings else []
    data = {
        "typeCode": code,
        "BuildingInfo": {"estateType": estate_type, "isHouse": is_house, "estate": estate},
        "bigestTypecode": parent or code,
        "bigest": {"subLevels": [{"key": k, "value": v} for k, v in phases]},
        "phase": {"subLevels": [{"key": k, "value": v} for k, v in blocks]},
        "tableList": {
            "menuItem": {"name": block, "typeCode": block_code},
            "estate": estate, "phase": phase, "property": block, "address": "沙田/測試街1號",
            "xAxisGrps": [{"floors": [{"yAxis": f"{fl}樓\n({fl}/F)", "units": [
                {"transID": tid, "xAxis": flat + "室", "transactionPrice": price, "nArea": n,
                 "gArea": g, "insDate": ins, "regDate": reg}
                for tid, fl2, flat, price, n, g, ins, reg in deals if fl2 == fl]}
                for fl in sorted({d[1] for d in deals})]}],
        },
        "briefList": {"opDate": op_date, "unitCnt": units, "noOfFloors": floors,
                      "schoolNet": {"secondarySchoolScope": {"db": "沙田區"}},
                      "scope": {"hma": "沙田", "terr": "新界東"}},
        "salesearch": {"data": sale},
        "rentsearch": {"data": []},
    }
    js = json.dumps({"layout": "x", "data": [data], "fetch": {}, "error": None}, ensure_ascii=False)
    return f"<html><script>window.__NUXT__=(function(a){{return {js}}}(false));</script></html>"


def info(**kw):
    return cm.parse_estate_info(nuxt_page(**kw))


# ---- sitemap -----------------------------------------------------------------------------------


def test_sitemap_lists_phase_and_estate_codes_only():
    assert cm.sitemap_estates(SITEMAP) == [
        ("2-AABKWPYAPE", "桂园"),
        ("3-LIDHTHXXHT", "愉景湾"),
    ]


# ---- estate facts ------------------------------------------------------------------------------


def test_parse_estate_info():
    i = info(op_date="1998-05-01T00:00:00", parent="3-PARENT0000")
    assert i.code == "2-AAAAAAAAAA"
    assert i.name == "測試苑"
    assert i.completion == date(1998, 5, 1)
    assert (i.units, i.floors) == (60, 30)
    assert i.district == "沙田區" and i.hma == "沙田"
    assert i.parent_code == "3-PARENT0000"
    assert not i.hos and not i.prh


def test_listing_flags_are_read_from_sale_and_rent_listings():
    assert info(hos=True).hos
    assert info(prh=True).prh


def test_missing_op_date_is_none():
    assert info(op_date="1900-01-01T00:00:00").completion is None


# ---- the exclusion rule (decisions.md "Coverage filter") --------------------------------------


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"hos": True}, "hos"),
        ({"prh": True}, "tps_prh"),
        ({"is_house": True}, "house"),
        ({"estate": "觀海別墅"}, "villa"),
        ({"estate": "貝沙灣 5期 洋房"}, "villa"),
        ({"estate": "观海别墅"}, "villa"),  # simplified, as the sitemap spells it
        ({"estate": "白沙灣 丈量約217地段1200洋房3"}, "villa"),
        ({"estate": "青山公路汀九段 DD399 Lot452"}, "village_lot"),
        ({"estate": "大埔仔村屋"}, "village_house"),
        ({"estate": "Sai Kung Village House"}, "village_house"),
        ({"estate": "太古城"}, None),
        ({"estate": "美孚新邨"}, None),  # 邨 is an estate, not a village house
        ({"estate": "愉景灣"}, None),
        ({"estate": "嘉湖山莊", "listings": False}, None),  # no listings: nothing to flag
    ],
)
def test_exclusion_rule(kw, reason):
    assert cm.exclusion_reason(info(**kw)) == reason


def test_manual_overrides_win():
    i = info(hos=True)
    assert cm.exclusion_reason(i, overrides={i.code: "include"}) is None
    j = info(estate="太古城")
    assert cm.exclusion_reason(j, overrides={j.code: "exclude"}) == "manual"


# ---- blocks to visit ---------------------------------------------------------------------------


def test_block_codes_for_a_phase_page_and_a_single_building():
    phase = cd.parse_block_page(nuxt_page(
        block_code="1-AAA", phases=[("2-X", "1期"), ("2-Y", "2期")],
        blocks=[("1-AAA", "1座"), ("1-BBB", "2座")]))
    assert cm.block_codes(phase) == ["1-AAA", "1-BBB"]  # sibling phases (2-) are not visited
    single = cd.parse_block_page(nuxt_page(block_code="1-ONLY", phases=[("1-ONLY", "測試苑")]))
    assert cm.block_codes(single) == ["1-ONLY"]


# ---- loading -----------------------------------------------------------------------------------

DEALS = [
    ("T1", "30", "B", 5_380_000, 405, 520, "2019-05-17T00:00:00", "2019-06-10T00:00:00"),
    ("T2", "G", "C", 2_200_000, 390, 520, "2006-02-27T00:00:00", "2006-03-23T00:00:00"),
    ("T3", "25", "A", 6_000_000, 405, 520, "2024-01-02T00:00:00", "2024-01-30T00:00:00"),
]


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    cm.ensure_tables(c)
    yield c
    c.close()


def load(con, html, url="https://example/test"):
    i = cm.parse_estate_info(html)
    return i, cm.load_block(con, i, cd.parse_block_page(html), url)


def test_load_block_stores_estate_building_units_and_deals(con):
    i, n = load(con, nuxt_page(deals=DEALS, phase="1期", block="1座"))
    assert n == 3
    est = con.execute("SELECT estate_id, name_zh, source FROM estate").fetchall()
    assert est == [("cd-2-AAAAAAAAAA", "測試苑", "centadata")]
    b = con.execute(
        "SELECT building_id, estate_id, name_zh, completion_date, floors, flats FROM building"
    ).fetchall()
    assert b == [("cd-1-BBBBBBBBBB", "cd-2-AAAAAAAAAA", "1座", date(1998, 5, 1), 30, 60)]
    assert con.execute("SELECT count(*) FROM unit").fetchone()[0] == 3
    row = con.execute(
        "SELECT source, building_id, floor_label, flat, price_hkd, instrument_date, "
        "registration_date, deal_type FROM txn_raw WHERE source_id = 'T1'"
    ).fetchone()
    assert row == ("centadata", "cd-1-BBBBBBBBBB", "30", "B", 5_380_000, date(2019, 5, 17),
                   date(2019, 6, 10), "registry")
    # the building's own saleable/gross ratio is stored for gross-only conversion
    ratio = con.execute("SELECT saleable_to_gross FROM building").fetchone()[0]
    assert ratio == pytest.approx(405 / 520)


def test_load_block_twice_adds_nothing(con):
    html = nuxt_page(deals=DEALS)
    load(con, html)
    _, again = load(con, html)
    assert again == 0
    assert con.execute("SELECT count(*) FROM txn_raw").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM unit").fetchone()[0] == 3


def test_master_rows_record_exclusions(con):
    i = info(hos=True)
    cm.save_estate(con, i, excluded="hos", blocks=0, pilot=False)
    cm.save_estate(con, info(estate="太古城", code="2-OKOKOKOKOK"), excluded=None, blocks=1,
                   pilot=False)
    rows = con.execute(
        "SELECT code, excluded, exclude_reason FROM master_estate ORDER BY code"
    ).fetchall()
    assert rows == [("2-AAAAAAAAAA", True, "hos"), ("2-OKOKOKOKOK", False, None)]
    # an excluded estate leaves no estate/building rows behind
    assert con.execute("SELECT count(*) FROM estate").fetchone()[0] == 0
