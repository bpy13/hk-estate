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
    ["05-04-2016", "12-04-2016", "", "Tower H2", "6", "C", "", "4,854,000.00", "", "", ""],
    ["", "", "", "", "", "", "", "", "", "", ""],
    ["10/10/2016", "", "", "", "", "", "", "$1.00", "", "", ""],  # no unit: ignored
]


def test_register_rows():
    deals = srpe.parse_register_rows(ROWS)
    assert len(deals) == 3  # the terminated sale and the unit-less row are dropped
    a, b, dashed = deals
    assert (dashed.block, dashed.price_hkd, dashed.asp_date) == (
        "Tower H2", 4_854_000, date(2016, 4, 12))
    assert (a.block, a.floor, a.flat, a.price_hkd) == ("L1", "5", "C", 3_723_048)
    assert (a.pasp_date, a.asp_date) == (date(2015, 12, 17), date(2015, 12, 24))
    assert a.deal_date == date(2015, 12, 24) and not a.pasp_only and not a.related_party
    assert b.deal_date == date(2016, 2, 1) and b.pasp_only and b.related_party
    assert b.has_parking


# House-sale registers have 9 columns (one description cell, no block/floor/unit split), so the
# price sits in column 5 and column 7 holds payment terms full of other amounts and dates.
HOUSE_ROWS = [
    ["(A)", "(B)", "(C)", "(D)", "", "(E)", "(F)", "(G)", "(H)"],
    ["-", "09-09-2020", "-", "1 EXAMPLE ROAD\n1 EXAMPLE ROAD", "-", "$2,566,000,000.00", "-",
     "Deposit HK$100,000,000.00 payable on 16 September 2020\n90%: 30 December 2020",
     "No"],
]


def test_house_register_layout_is_read_by_column_count():
    (deal,) = srpe.parse_register_rows(HOUSE_ROWS)
    assert deal.price_hkd == 2_566_000_000 and deal.asp_date == date(2020, 9, 9)
    assert deal.block.startswith("1 EXAMPLE ROAD") and not deal.related_party


def test_unparseable_row_is_skipped_and_reported_not_fatal():
    bad = ["01/02/2016", "", "", "L1", "5", "C", "", "$1.2.3", "", "", ""]
    skipped = []
    deals = srpe.parse_register_rows([bad, ROWS[2]], skipped)
    assert len(deals) == 1 and deals[0].price_hkd == 3_723_048
    assert len(skipped) == 1 and "$1.2.3" in skipped[0]


def test_register_deals_reports_skipped_rows_with_page_number():
    class Page:
        def extract_tables(self):
            return [[["01/02/2016", "", "", "L1", "5", "C", "", "$1.2.3", "", "", ""]]]

    class Pdf:
        pages = [Page()]

    skipped = []
    assert srpe.register_deals(Pdf(), skipped) == []
    assert skipped and skipped[0][0] == 1 and "$1.2.3" in skipped[0][1]


def test_pdf_rows_are_read_from_every_page_table(tmp_path):
    # the reader only needs page.extract_tables(); a stand-in keeps the test free of PDFs
    class Page:
        def __init__(self, tables):
            self._t = tables

        def extract_tables(self):
            return self._t

    class Pdf:
        pages = [Page([[["x"]]]), Page([ROWS])]

    assert len(srpe.register_deals(Pdf())) == 3


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
    assert n == 3
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


def test_client_checks_robots_before_login_and_every_request():
    from hkestate.fetch import RobotsDisallowed

    asked = []

    def deny(url):
        asked.append(url)
        return False

    c = srpe.SrpeClient(allowed=deny)

    def boom(*a, **k):
        raise AssertionError("a disallowed path was requested")

    c._http.request = boom
    with pytest.raises(RobotsDisallowed):
        c.login()  # refused before any browser starts
    with pytest.raises(RobotsDisallowed):
        c.post("DistrictAreaSearch/getDistrictAreaSearchResult", {})
    with pytest.raises(RobotsDisallowed):
        c.download("/api/SrpeWebService/download/all_development_map/trx/x/1/f.pdf/en")
    assert asked[0] == srpe.DISCLAIMER_URL
    assert "DistrictAreaSearch" in asked[1] and "/download/" in asked[2]
    c.close()
