"""Coordinates (Address Lookup Service), MTR distance, district and building age."""

from datetime import date

import pytest

from hkestate import db, geo
from hkestate.sources import cd_master as cm

ALS = """<?xml version="1.0" encoding="UTF-8"?><AddressLookupResult><SuggestedAddress><Address>
<PremisesAddress><EngPremisesAddress><BuildingName>GOLDWIN HEIGHTS</BuildingName>
<EngDistrict><DcDistrict>CENTRAL &amp; WESTERN DISTRICT</DcDistrict></EngDistrict>
</EngPremisesAddress>
<ChiPremisesAddress><ChiDistrict><DcDistrict>中西區</DcDistrict></ChiDistrict>
<BuildingName>高雲臺</BuildingName></ChiPremisesAddress>
<GeospatialInformation><Latitude>22.28151</Latitude><Longitude>114.15001</Longitude>
</GeospatialInformation></PremisesAddress></Address>
<ValidationInformation><Score>88.75</Score></ValidationInformation></SuggestedAddress>
</AddressLookupResult>"""


def test_parse_als():
    r = geo.parse_als(ALS)
    assert (r.lat, r.lon) == (22.28151, 114.15001)
    assert r.name_zh == "高雲臺" and r.district_zh == "中西區" and r.score == 88.75


def test_parse_als_no_match():
    assert geo.parse_als("<AddressLookupResult></AddressLookupResult>") is None


@pytest.mark.parametrize("address, expected", [
    ("沙田/銀城街1號", "銀城街1號"),
    ("深水埗/百老匯街1-3號", "百老匯街1號"),
    ("荃灣/青山公路汀九段370號", "青山公路汀九段370號"),
    ("西摩道2號", "西摩道2號"),
    ("", None),
])
def test_als_query(address, expected):
    assert geo.als_query(address) == expected


def test_haversine_and_nearest():
    # about 450 m apart
    d = geo.haversine_m(22.32202, 114.17259, 22.3193, 114.1693)
    assert 400 < d < 600
    stations = [("A", 22.3, 114.1), ("B", 22.32, 114.17)]
    name, m = geo.nearest_station(22.3205, 114.1725, stations)
    assert name == "B" and m < 300


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    cm.ensure_tables(c)
    geo.ensure_tables(c)
    yield c
    c.close()


def test_building_age_from_completion_else_first_deal(con):
    con.execute("INSERT INTO master_building (building_id, completion_date) VALUES "
                "('a', DATE '1994-07-04'), ('b', NULL), ('c', DATE '1900-01-01')")
    con.execute("INSERT INTO txn_raw (source, source_id, fetched_at, row_hash, building_id, "
                "deal_date) VALUES ('centadata', 't1', now(), 'h', 'b', DATE '2003-05-01'), "
                "('centadata', 't2', now(), 'h2', 'b', DATE '2010-05-01')")
    geo.set_building_ages(con, today=date(2026, 10, 16))
    rows = dict((r[0], r[1:]) for r in con.execute(
        "SELECT building_id, built_year, built_basis, age_years FROM master_building").fetchall())
    assert rows["a"] == (1994, "completion", 32)
    assert rows["b"] == (2003, "first_deal", 23)
    assert rows["c"] == (None, None, None)


def test_mtr_distance_needs_every_station(con):
    con.execute("INSERT INTO mtr_station VALUES ('MOK', '旺角', 'Mong Kok', 22.3193, 114.1693)")
    con.execute("INSERT INTO mtr_station VALUES ('XXX', '無坐標', 'No Coords', NULL, NULL)")
    con.execute("INSERT INTO master_building (building_id, lat, lon) VALUES ('a', 22.32, 114.17)")
    # a station without coordinates could be the true nearest one: refuse to guess
    assert geo.assign_mtr(con) == 0
    con.execute("UPDATE mtr_station SET lat = 22.5, lon = 114.5 WHERE code = 'XXX'")
    assert geo.assign_mtr(con) == 1
    st, m = con.execute("SELECT mtr_station, mtr_m FROM master_building").fetchone()
    assert st == "旺角" and 0 < m < 300


WIKI_LIST = """<table>
<a href="https://en.wikipedia.org/wiki/Admiralty_station_(MTR)">Admiralty</a>
<a href="https://en.wikipedia.org/wiki/Tsim_Sha_Tsui_station">Tsim Sha Tsui</a>
<a href="https://en.wikipedia.org/wiki/File:Admiralty_Station_2018.jpg">photo</a>
<a href="https://en.wikipedia.org/wiki/Hung_Hom_station">Hung Hom</a>
</table>"""


def wiki_page(lat, lon):
    return (f'<span class="geo-dms"><span class="latitude">22°16′44″N</span></span>'
            f'<span class="geo">{lat}; {lon}</span>')


def test_store_lines_keeps_line_rows_and_replaces_them(con):
    head = ('"Line Code","Direction","Station Code","Station ID","Chinese Name",'
            '"English Name","Sequence"\n')
    text = "﻿" + head + '"TWL","UT","ADM","1","金鐘","Admiralty",1.00\n' \
        '"TWL","UT","TST","2","尖沙咀","Tsim Sha Tsui",2.00\n' \
        '"TWL","UT","","3","x","x",3.00\n'
    assert geo.store_lines(con, text) == 2
    assert geo.store_lines(con, text) == 2  # a reload replaces, never duplicates
    rows = con.execute("SELECT line_code, direction, station_code, sequence "
                       "FROM mtr_line_station ORDER BY sequence").fetchall()
    assert rows == [("TWL", "UT", "ADM", 1.0), ("TWL", "UT", "TST", 2.0)]


def test_wiki_station_links_skip_files_and_use_comparable_names():
    links = geo.wiki_station_links(WIKI_LIST)
    assert sorted(links) == ["admiralty", "hunghom", "tsimshatsui"]


def test_wiki_coords_inside_hong_kong_only():
    assert geo.wiki_coords(wiki_page("22.27929", "114.16463")) == (22.27929, 114.16463)
    assert geo.wiki_coords('<span class="geo-dec">22.3°N 114.2°E</span>') == (22.3, 114.2)
    assert geo.wiki_coords(wiki_page("51.5", "0.1")) is None  # not Hong Kong
    assert geo.wiki_coords("<p>no coordinates</p>") is None


def test_wikipedia_coords_fetches_only_wanted_stations():
    pages = {
        geo.WIKI_LIST: WIKI_LIST,
        "https://en.wikipedia.org/wiki/Admiralty_station_(MTR)": wiki_page("22.2793", "114.1646"),
        "https://en.wikipedia.org/wiki/Hung_Hom_station": wiki_page("22.3030", "114.1818"),
    }
    seen = []

    class F:
        def get(self, url, page_type):
            seen.append(url)
            return pages[url]

    got = geo.wikipedia_coords(F(), {"admiralty", "nowhere"})
    assert got == {"admiralty": (22.2793, 114.1646)}
    assert len(seen) == 2  # the list and one station


def _bld(con, bid, estate=None, district=None, geo_district=None, lat=None, lon=None):
    con.execute(
        "INSERT INTO master_building (building_id, estate_code, district, geo_district, lat, lon) "
        "VALUES (?, ?, ?, ?, ?, ?)", [bid, estate, district, geo_district, lat, lon])


def _district(con, bid):
    return con.execute("SELECT district FROM master_building WHERE building_id = ?",
                       [bid]).fetchone()[0]


def test_fill_districts_from_als_answer(con):
    _bld(con, "a", geo_district="沙田區")
    _bld(con, "b", geo_district="Sha Tin")  # not a Chinese district name: not used
    assert geo.fill_districts(con) == 1
    assert _district(con, "a") == "沙田區"
    assert _district(con, "b") is None


def test_fill_districts_from_most_common_in_estate(con):
    _bld(con, "x1", "E1", "東區")
    _bld(con, "x2", "E1", "東區")
    _bld(con, "x3", "E1", "灣仔區")
    _bld(con, "x4", "E1")
    _bld(con, "y1", "E2", "沙田區")
    _bld(con, "z1", None)  # no estate: nothing to copy
    assert geo.fill_districts(con) == 1
    assert _district(con, "x4") == "東區"
    assert _district(con, "z1") is None


def test_fill_districts_from_nearest_building_within_1km(con):
    _bld(con, "near", None, "油尖旺區", lat=22.3200, lon=114.1700)
    _bld(con, "far", None, "觀塘區", lat=22.3300, lon=114.2000)
    _bld(con, "a", None, lat=22.3205, lon=114.1702)   # about 60 m from 'near'
    _bld(con, "b", None, lat=22.3400, lon=114.2500)   # nothing within 1 km
    _bld(con, "c", None)                               # not geocoded
    assert geo.fill_districts(con) == 1
    assert _district(con, "a") == "油尖旺區"
    assert _district(con, "b") is None and _district(con, "c") is None


def _status(con, bid, status):
    con.execute("UPDATE master_building SET geo_status = ? WHERE building_id = ?", [status, bid])


def test_mark_mismatches_only_ok_rows_with_both_districts_differing(con):
    _bld(con, "bad", None, "東區", "灣仔區", lat=22.3, lon=114.2)
    _bld(con, "same", None, "東區", "東區", lat=22.3, lon=114.2)
    _bld(con, "nodist", None, None, "灣仔區", lat=22.3, lon=114.2)
    _bld(con, "noals", None, "東區", None, lat=22.3, lon=114.2)
    _bld(con, "nomatch", None, "東區", "灣仔區")
    for b in ("bad", "same", "nodist", "noals"):
        _status(con, b, "ok")
    _status(con, "nomatch", "no_match")
    assert geo.mark_mismatches(con) == 1
    got = dict(con.execute("SELECT building_id, geo_status FROM master_building").fetchall())
    assert got == {"bad": "mismatch", "same": "ok", "nodist": "ok", "noals": "ok",
                   "nomatch": "no_match"}
    assert geo.mark_mismatches(con) == 0


def test_mismatch_gets_no_mtr_and_no_district_donation(con):
    con.execute("INSERT INTO mtr_station VALUES ('MOK', '旺角', 'Mong Kok', 22.3193, 114.1693)")
    _bld(con, "bad", None, "東區", "灣仔區", lat=22.32, lon=114.17)
    _bld(con, "good", None, "油尖旺區", "油尖旺區", lat=22.3200, lon=114.1700)
    _bld(con, "needs", None, None, lat=22.3205, lon=114.1702)
    _status(con, "good", "ok")
    _status(con, "needs", "ok")
    _status(con, "bad", "ok")
    con.execute("UPDATE master_building SET mtr_m = 5, mtr_station = 'x' WHERE building_id = 'bad'")
    geo.mark_mismatches(con)
    assert geo.assign_mtr(con) == 2
    assert con.execute("SELECT mtr_m, mtr_station FROM master_building "
                       "WHERE building_id = 'bad'").fetchone() == (None, None)
    # the wrong-district building must not lend its district either
    con.execute("DELETE FROM master_building WHERE building_id = 'good'")
    geo.fill_districts(con)
    assert _district(con, "needs") is None  # only 'bad' is near, and its coordinates are distrusted


def test_fill_districts_order_and_never_overwrites(con):
    _bld(con, "e1", "E1", "東區")
    _bld(con, "keep", "E1", "南區", geo_district="沙田區")   # has one: untouched
    _bld(con, "als", "E1", None, geo_district="沙田區")       # ALS beats the estate
    assert geo.fill_districts(con) == 1
    assert _district(con, "keep") == "南區" and _district(con, "als") == "沙田區"
    assert geo.fill_districts(con) == 0
