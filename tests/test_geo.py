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
