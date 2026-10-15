"""Centadata block pages and the cross-check against 28Hse deals."""

from datetime import date, datetime
from pathlib import Path

import pytest

from hkestate import db
from hkestate.sources import centadata as cd

FIX = Path(__file__).parent / "fixtures" / "centadata"


def page():
    return (FIX / "block_page.html").read_text(encoding="utf-8")


# ---- the safe literal reader -------------------------------------------------------------------


def test_js_literal_values():
    assert cd.parse_js_value('{a:1,"b":[true,false,null,void 0,!0,!1],c:"x\\u002Fy\\"z"}') == {
        "a": 1,
        "b": [True, False, None, None, True, False],
        "c": 'x/y"z',
    }
    assert cd.parse_js_value("-1.5e2") == -150.0
    assert cd.parse_js_value("[.5, 2]") == [0.5, 2]


def test_js_reader_refuses_code():
    with pytest.raises(cd.NotData):
        cd.parse_js_value("{a:alert(1)}", refs={})
    with pytest.raises(cd.NotData):
        cd.parse_js_value("{a:x}", refs={})  # unknown name


def test_read_nuxt_resolves_function_arguments():
    data = cd.read_nuxt(page())
    table = data["data"][0]["tableList"]
    assert table["estate"] == "沙田第一城"
    assert table["address"] == "沙田/銀城街1號"
    unit = table["xAxisGrps"][0]["floors"][0]["units"][1]
    assert unit["nArea"] == 405 and unit["gArea"] == 520
    assert unit["transUnitState"] == "1"


# ---- block page --------------------------------------------------------------------------------


def test_parse_block_page():
    blk = cd.parse_block_page(page())
    assert (blk.estate, blk.phase, blk.block) == ("沙田第一城", "1期", "1座")
    assert blk.phases == [("2-XSHNIHSXHT", "1期"), ("2-XSHNIHSXMT", "2期")]
    assert blk.blocks == [("1-GYPDGPYXPS", "1座"), ("1-GYPBGPYXES", "2座")]
    assert len(blk.deals) == 2  # flat 30A has no recorded deal
    d = blk.deals[0]
    assert (d.floor_text, d.flat, d.price_hkd) == ("30", "B", 5_380_000)
    assert (d.saleable_sqft, d.gross_sqft) == (405, 520)
    assert (d.instrument_date, d.registration_date) == (date(2019, 5, 17), date(2019, 6, 10))
    assert d.trans_id == "19061001900036"
    g = blk.deals[1]
    assert (g.floor_text, g.flat, g.price_hkd, g.saleable_sqft) == ("地下", "C", 2_200_000, 390)


def test_entry_page_menus_split_into_phases_and_blocks():
    # single-phase estates list their blocks (1- codes) in the estate menu
    menu = [("1-AAA", "1座"), ("2-BBB", "2期"), ("1-CCC", "2座")]
    assert cd.split_menu(menu) == ([("2-BBB", "2期")], [("1-AAA", "1座"), ("1-CCC", "2座")])


def test_block_url():
    assert cd.block_url("沙田第一城", "1-GYPDGPYXPS") == (
        "https://hk.centanet.com/findproperty/centadata-details/"
        "%E6%B2%99%E7%94%B0%E7%AC%AC%E4%B8%80%E5%9F%8E_1-GYPDGPYXPS"
    )


# ---- matching -----------------------------------------------------------------------------------


def deal(price, ins, reg, floor="30", flat="B"):
    return cd.CdDeal("t" + ins.isoformat(), floor, flat, price, 405, 520, ins, reg)


def test_match_uses_either_date_and_price_tolerance():
    hse = [
        ("u1:2019-06-10", date(2019, 6, 10), 5_380_000),  # same as registration date
        ("u1:2024-01-03", date(2024, 1, 3), 6_000_000),
    ]
    assert cd.match_deal(deal(5_380_000, date(2019, 5, 17), date(2019, 6, 10)), hse) == (
        "u1:2019-06-10",
        "registration",
    )
    assert cd.match_deal(deal(6_030_000, date(2024, 1, 3), date(2024, 1, 20)), hse) == (
        "u1:2024-01-03",
        "instrument",
    )
    assert cd.match_deal(deal(6_500_000, date(2024, 1, 3), date(2024, 1, 20)), hse) is None
    assert cd.match_deal(deal(5_380_000, date(2015, 5, 17), date(2015, 6, 10)), hse) is None


def test_crosscheck_adds_only_centadata_only_deals(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
        con.execute(
            "INSERT INTO building (building_id, estate_id, phase_id, name_zh) "
            "VALUES ('h28-1-s1-b1', 'h28-1', 'h28-1-s1', '1座')"
        )
        con.execute("INSERT INTO phase VALUES ('h28-1-s1', 'h28-1', '第1期', '1')")
        db.insert_txn_raw(
            con,
            [
                {
                    "source": "hse28",
                    "source_id": "u9:2019-06-10",
                    "fetched_at": datetime(2026, 10, 16),
                    "building_id": "h28-1-s1-b1",
                    "floor_label": "30",
                    "flat": "B",
                    "deal_date": date(2019, 6, 10),
                    "deal_type": "registry",
                    "price_hkd": 5_380_000,
                    "saleable_sqft": 405.0,
                }
            ],
        )
        result = cd.crosscheck_block(
            con, cd.parse_block_page(page()), {"h28-1-s1-b1": ("第1期", "1座")}, "https://x"
        )
        assert result.building_id == "h28-1-s1-b1"
        assert (result.deals, result.in_window, result.matched) == (2, 1, 1)
        rows = con.execute(
            "SELECT source, floor_label, flat, deal_date, instrument_date, registration_date "
            "FROM txn_raw WHERE source = 'centadata'"
        ).fetchall()
        assert rows == [
            ("centadata", "G", "C", date(2006, 2, 27), date(2006, 2, 27), date(2006, 3, 23))
        ]
        # rerun adds nothing
        cd.crosscheck_block(
            con, cd.parse_block_page(page()), {"h28-1-s1-b1": ("第1期", "1座")}, "https://x"
        )
        assert con.execute("SELECT count(*) FROM txn_raw").fetchone()[0] == 2


def test_crawl_visits_each_block_once():
    class Fake:
        def __init__(self):
            self.urls = []

        def get(self, url, page_type):
            self.urls.append(url)
            return page()

    f = Fake()
    pages = list(cd.crawl_estate(f, "沙田第一城", "3-XSHNIHSXHT"))
    # entry page (block 1-GYPDGPYXPS), two phase pages whose first block was already seen
    # (not yielded again), then the one block not yet seen
    assert len(pages) == 2
    assert len(f.urls) == 4
    assert f.urls[-1].endswith("_1-GYPBGPYXES")


def test_crosscheck_block_spanning_several_buildings(tmp_path):
    # Centadata's "百老匯街1-3號" covers 28Hse's buildings 1號, 2號 and 3號.
    path = tmp_path / "t.duckdb"
    buildings = {f"b{n}": ("第一期", f"百老匯街{n}號") for n in (1, 2, 3, 4)}
    with db.session(path) as con:
        db.init_db(con)
        for bid in buildings:
            con.execute(
                "INSERT INTO building (building_id, estate_id, name_zh) VALUES (?, 'e', 'x')",
                [bid],
            )
        con.execute(
            "INSERT INTO unit (unit_id, building_id, floor_label, flat) "
            "VALUES ('b3|G|C', 'b3', 'G', 'C')"
        )
        db.insert_txn_raw(
            con,
            [{"source": "hse28", "source_id": "u1:2019-06-10",
              "fetched_at": datetime(2026, 10, 16), "building_id": "b2", "floor_label": "30",
              "flat": "B", "deal_date": date(2019, 6, 10), "deal_type": "registry",
              "price_hkd": 5_380_000}],
        )
        blk = cd.parse_block_page(page())
        blk.block = "百老匯街1-3號"
        res = cd.crosscheck_block(con, blk, buildings, "https://x")
        assert (res.in_window, res.matched, res.added) == (1, 1, 1)
        assert con.execute(
            "SELECT building_id FROM txn_raw WHERE source = 'centadata'"
        ).fetchall() == [("b3",)]


def test_flat_label_carrying_the_street_number_picks_the_building(tmp_path):
    # Centadata's Mei Foo flats read "1號A": building 百老匯街1號, flat A.
    path = tmp_path / "t.duckdb"
    buildings = {f"b{n}": ("第一期", f"百老匯街{n}號") for n in (1, 2, 3, 11)}
    with db.session(path) as con:
        db.init_db(con)
        db.insert_txn_raw(
            con,
            [{"source": "hse28", "source_id": "u1:2019-06-10",
              "fetched_at": datetime(2026, 10, 16), "building_id": "b2", "floor_label": "30",
              "flat": "B", "deal_date": date(2019, 6, 10), "deal_type": "registry",
              "price_hkd": 5_380_000}],
        )
        blk = cd.parse_block_page(page())
        blk.block = "百老匯街1-3號"
        blk.deals[0].flat = "2號B"
        blk.deals[1].flat = "1號C"
        res = cd.crosscheck_block(con, blk, buildings, "https://x")
        assert (res.in_window, res.matched, res.added, res.unplaced) == (1, 1, 1, 0)
        assert con.execute(
            "SELECT building_id, flat FROM txn_raw WHERE source = 'centadata'"
        ).fetchall() == [("b1", "C")]
