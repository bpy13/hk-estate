"""SRPE first-hand sales: development list, register-of-transactions rows, matching, dedupe."""

from datetime import date

import pytest

from hkestate import db
from hkestate.sources import cd_master as cm
from hkestate.sources import srpe

LIST_JSON = {"code": 0, "resultData": {"total": 2, "list": [
    {"id": "2947", "chnName": "煥然壹居", "engName": None, "chnPhaseName": None,
     "addresses": [{"chnAddress": "木翠街 3號", "engAddress": "3 MUK CHUI STREET"}],
     "planningArea1": {"broadDistrictNameChn": "九龍"}, "active": "N"},
    {"id": "10", "chnName": "某某山", "chnPhaseName": "第2期",
     "addresses": [{"chnAddress": "康城路 8號"}, {"chnAddress": "康城路 10號"}],
     "planningArea1": None},
]}}

DETAIL_JSON = {"code": 0, "resultData": {
    "devId": "2947", "uniqueSessionId": "SESS123",
    "dev": {"latitude": 22.331205, "longtitude": 114.2021949},
    "transactions": [{"id": "91486", "file": {"fileName": "96855250905002RT.pdf"},
                      "updateDate": "2025-09-05"}]}}


def test_parse_dev_list():
    devs = srpe.parse_dev_list(LIST_JSON)
    assert [d.dev_id for d in devs] == ["2947", "10"]
    assert devs[0].name == "煥然壹居" and devs[0].addresses == ["木翠街3號"]
    assert devs[1].phase == "第2期" and devs[1].addresses == ["康城路8號", "康城路10號"]


def test_parse_dev_detail_gives_the_register_download():
    d = srpe.parse_dev_detail(DETAIL_JSON)
    assert (d.lat, d.lon) == (22.331205, 114.2021949)
    assert d.register_id == "91486" and d.register_file == "96855250905002RT.pdf"
    assert d.download_path == (
        "/api/SrpeWebService/download/all_development_map/trx/SESS123/91486/"
        "96855250905002RT.pdf/en?devId=2947")


def test_parse_dev_detail_without_a_register():
    d = srpe.parse_dev_detail({"resultData": {"devId": "1", "transactions": []}})
    assert d.register_id is None and d.download_path is None


HEAD = ["(A)", "(B)", "(C)", "(D)", None, None, None, "(E)", "(F)", "(G)", "(H)"]
ROWS = [
    HEAD,
    ["PASP", "ASP", "term", "Block", None, None, None, "Price", "rev", "terms", "related"],
    ["17/12/2015", "24/12/2015", "", "L1", "5", "C", "", "$3,723,048.00", "", "(a) terms", ""],
    ["01/02/2016", "", "", "L1", "12", "A", "P12", "$5,000,000.00", "", "", "Yes"],  # PASP only
    ["05/03/2016", "20/03/2016", "01/06/2016", "L1", "7", "B", "", "$4,000,000.00", "", "", ""],
    ["", "", "", "", "", "", "", "", "", "", ""],
    ["10/10/2016", "", "", "", "", "", "", "$1.00", "", "", ""],  # no unit: ignored
]


def test_register_rows():
    deals = srpe.parse_register_rows(ROWS)
    assert len(deals) == 2  # the terminated sale and the unit-less row are dropped
    a, b = deals
    assert (a.block, a.floor, a.flat, a.price_hkd) == ("L1", "5", "C", 3_723_048)
    assert (a.pasp_date, a.asp_date) == (date(2015, 12, 17), date(2015, 12, 24))
    assert a.deal_date == date(2015, 12, 24) and not a.pasp_only and not a.related_party
    assert b.deal_date == date(2016, 2, 1) and b.pasp_only and b.related_party
    assert b.has_parking


def test_pdf_rows_are_read_from_every_page_table(tmp_path):
    # the reader only needs page.extract_tables(); a stand-in keeps the test free of PDFs
    class Page:
        def __init__(self, tables):
            self._t = tables

        def extract_tables(self):
            return self._t

    class Pdf:
        pages = [Page([[["x"]]]), Page([ROWS])]

    assert len(srpe.register_deals(Pdf())) == 2


# ---- matching to master buildings and dedupe ---------------------------------------------------


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    cm.ensure_tables(c)
    srpe.ensure_tables(c)
    yield c
    c.close()


def building(con, bid, address, block="1座", phase=""):
    con.execute("INSERT INTO master_building (building_id, address, block_name, phase_name) "
                "VALUES (?, ?, ?, ?)", [bid, address, block, phase])


def test_match_buildings_by_address(con):
    building(con, "cd-1-A", "九龍城/木翠街3號")
    building(con, "cd-1-B", "九龍城/木翠街5號")
    dev = srpe.parse_dev_list(LIST_JSON)[0]
    assert srpe.match_development(con, dev) == ["cd-1-A"]


def test_match_buildings_unknown_address(con):
    building(con, "cd-1-A", "九龍城/木翠街5號")
    assert srpe.match_development(con, srpe.parse_dev_list(LIST_JSON)[0]) == []


def test_store_register_flags_first_hand_and_dedupes_against_other_sources(con):
    deals = srpe.parse_register_rows(ROWS)
    n = srpe.store_deals(con, "2947", "煥然壹居", ["cd-1-A"], deals, "https://example/rt")
    assert n == 2
    row = con.execute("SELECT source, first_hand, deal_type, building_id, floor_label, flat "
                      "FROM txn_raw WHERE source = 'srpe' ORDER BY deal_date").fetchall()
    assert row[0] == ("srpe", True, "first_hand", "cd-1-A", "5", "C")
    assert srpe.store_deals(con, "2947", "煥然壹居", ["cd-1-A"], deals, "u") == 0  # rerun: none
    # the same flat reported by Centadata a little later at about the same price
    db.insert_txn_raw(con, [{
        "source": "centadata", "source_id": "T1", "fetched_at": "2026-10-16 00:00:00",
        "building_id": "cd-1-A", "floor_label": "5", "flat": "C", "deal_date": date(2016, 1, 5),
        "deal_type": "registry", "price_hkd": 3_730_000}])
    flags = dict(con.execute(
        "SELECT source, excl_duplicate FROM txn_all WHERE flat = 'C' AND floor_label = '5'"
    ).fetchall())
    assert flags == {"centadata": True, "srpe": False}
    # the first-hand deal itself stays out of the clean view, and so does its duplicate
    assert con.execute("SELECT count(*) FROM txn WHERE flat = 'C'").fetchone()[0] == 0
