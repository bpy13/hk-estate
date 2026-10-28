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


def test_unbelievable_asp_date_falls_back_to_the_pasp_date():
    # a register wrote 26/09/2055 for a sale whose PASP was 13/09/2025 (seen 2026-10-21)
    rows = [ROWS[0], ROWS[1],
            ["13/09/2025", "26/09/2055", "", "the MVP", "11", "A", "P1", "$35,353,900", "", "", ""],
            ["13/09/2025", "10/09/2025", "", "the MVP", "12", "A", "", "$30,000,000", "", "", ""],
            ["13/09/2025", "20/09/2025", "", "the MVP", "13", "A", "", "$31,000,000", "", "", ""]]
    typo, before, normal = srpe.parse_register_rows(rows)
    assert typo.asp_date == date(2055, 9, 26)  # kept as read, but not used as the deal date
    assert typo.deal_date == date(2025, 9, 13) and not typo.pasp_only
    assert before.deal_date == date(2025, 9, 13)  # an ASP before its PASP is a typo too
    assert normal.deal_date == date(2025, 9, 20)


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


def test_price_cell_with_a_long_note_uses_only_the_first_amount():
    row = ["01/02/2016", "", "", "L1", "5", "C", "", "$3,723,048.00 (see note 2: 2442 2024 1181)",
           "", "", ""]
    (deal,) = srpe.parse_register_rows([row])
    assert deal.price_hkd == 3_723_048


def test_row_with_two_flats_in_its_cells_gives_one_deal_per_flat():
    # Development 3085, PASP 2016-12-03: the PDF cells hold two lines each (block 3 floor 2 flat
    # A at $7,281,000; block 3 floor 5 flat A at $14,452,000). Joined, the prices read as
    # HK$728,100,014,452,000.
    row = ["03/12/2016", "14/12/2016", "", "3\n3", "2\n5", "A\nA", "",
           "$7,281,000\n$14,452,000", "", "Pricelist No. 7D\nPricelist No. 7D", ""]
    deals = srpe.parse_register_rows([row])
    assert [(d.block, d.floor, d.flat, d.price_hkd) for d in deals] == [
        ("3", "2", "A", 7_281_000), ("3", "5", "A", 14_452_000)]
    assert all(d.deal_date == date(2016, 12, 14) for d in deals)


def test_row_with_mismatched_line_counts_is_skipped_not_guessed():
    row = ["03/12/2016", "", "", "3\n3", "2\n5", "A", "", "$7,281,000\n$14,452,000", "", "", ""]
    skipped = []
    assert srpe.parse_register_rows([row], skipped) == []
    assert len(skipped) == 1


def test_block_named_in_two_languages_is_one_flat():
    # most registers (e.g. development 10505): the 2026-10-23 multi-flat change read the
    # two-line block cell as two flats and rejected the row
    row = ["10-11-2025", "17-11-2025", "", "第2座\nTower 2", "2", "B", "", "$15,030,000", "",
           "招標文件第3-A號 Tender Document No. 3-A\n(1) 支付條款A2", ""]
    skipped = []
    (deal,) = srpe.parse_register_rows([row], skipped)
    assert not skipped
    assert (deal.block, deal.floor, deal.flat, deal.price_hkd) == (
        "第2座 Tower 2", "2", "B", 15_030_000)
    # three flats in one row, block in two languages: the block applies to all
    three = ["03/12/2016", "", "", "第3座\nTower 3", "2\n5\n6", "A\nA\nA", "",
             "$7,281,000\n$14,452,000\n$9,000,000", "", "", ""]
    assert [(d.block, d.floor) for d in srpe.parse_register_rows([three])] == [
        ("第3座 Tower 3", "2"), ("第3座 Tower 3", "5"), ("第3座 Tower 3", "6")]
    # a note whose lines start with digits is not several prices
    note = ["02/12/2023", "08/12/2023", "", "1", "9", "A", "", "$8,731,800\n3 of Price List\n"
            "31/1/2024", "", "", ""]
    assert [d.price_hkd for d in srpe.parse_register_rows([note])] == [8_731_800]


def test_absurd_price_is_a_skipped_row_not_a_crash():
    # the 8805 case: digits of a note were joined into a 32-digit number
    junk = ["01/02/2016", "", "", "L1", "5", "C", "", "24420241181016001436271182348288",
            "", "", ""]
    cheap = ["01/02/2016", "", "", "L1", "6", "C", "", "$50.00", "", "", ""]
    skipped = []
    assert srpe.parse_register_rows([junk, cheap, ROWS[2]], skipped)[0].price_hkd == 3_723_048
    assert len(skipped) == 2


def test_reparse_cached_replaces_joined_rows_and_reads_no_web(tmp_path, monkeypatch):
    import pdfplumber

    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "t.duckdb"
    pdf_dir = tmp_path / "cache" / srpe.HOST / "pdf"
    pdf_dir.mkdir(parents=True)
    (pdf_dir / "3085_rt.pdf").write_bytes(b"%PDF")
    (pdf_dir / "9999_rt.pdf").write_bytes(b"%PDF")  # empty parse: its rows must stay
    two_flats = ["03/12/2016", "14/12/2016", "", "3\n3", "2\n5", "A\nA", "",
                 "$7,281,000\n$14,452,000", "", "", ""]
    with db.session(dbp) as con:
        db.init_db(con)
        cm.ensure_tables(con)
        srpe.ensure_tables(con)
        for dev in ("3085", "9999"):
            con.execute("INSERT INTO srpe_development (dev_id, name_zh, buildings) "
                        "VALUES (?, 'X', '')", [dev])
        db.insert_txn_raw(con, [
            {"source": "srpe", "source_id": f"{sid}", "fetched_at": "2026-10-16 00:00:00",
             "url": "u", "deal_date": date(2016, 12, 14), "price_hkd": price,
             "first_hand": True}
            for sid, price in [("3085:3 3:2 5:A A:2016-12-03", 728_100_014_452_000),
                               ("9999:1:1:A:2016-01-01", 5_000_000)]])

    class Pdf:
        def __init__(self, path):
            self.rows = [two_flats] if "3085" in str(path) else []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pdfplumber, "open", Pdf)
    monkeypatch.setattr(srpe, "register_deals",
                        lambda pdf, skipped=None: srpe.parse_register_rows(pdf.rows))
    assert srpe.reparse_cached(dbp, dry_run=True)["deleted"] == 1
    stats = srpe.reparse_cached(dbp)
    assert stats["added"] == 2 and stats["deleted"] == 1 and stats["empty"] == 1
    with db.session(dbp) as con:
        rows = con.execute("SELECT source_id, price_hkd FROM txn_raw ORDER BY source_id"
                           ).fetchall()
    assert rows == [("3085:3:2:A:2016-12-03", 7_281_000), ("3085:3:5:A:2016-12-03", 14_452_000),
                    ("9999:1:1:A:2016-01-01", 5_000_000)]


def test_one_bad_register_does_not_end_the_run(tmp_path, monkeypatch):
    import pdfplumber

    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "t.duckdb"
    with db.session(dbp) as con:
        db.init_db(con)

    class Pdf:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pdfplumber, "open", lambda p: Pdf())

    def fake_deals(pdf, skipped=None):
        if fake_deals.calls == 0:
            fake_deals.calls += 1
            raise RuntimeError("boom")
        return srpe.parse_register_rows(ROWS)

    fake_deals.calls = 0
    monkeypatch.setattr(srpe, "register_deals", fake_deals)

    class Client:
        requests = 0

        def login(self):
            pass

        def download(self, path):
            return b"%PDF"

        def post(self, endpoint, payload):
            if endpoint.startswith("DistrictAreaSearch"):
                return LIST_JSON
            dev = payload["devId"]
            return {"resultData": {**DETAIL_JSON["resultData"], "devId": dev}}

    logs = []
    stats = srpe.run_srpe(Client(), dbp, logs.append)
    assert stats["failed_registers"] == 1 and stats["registers"] == 1
    assert any("boom" in m for m in logs)


class CountingClient:
    """Answers the list and the detail of each development; counts the detail calls."""

    requests = 0

    def __init__(self):
        self.details = 0

    def login(self):
        pass

    def download(self, path):
        return b"%PDF"

    def post(self, endpoint, payload):
        if endpoint.startswith("DistrictAreaSearch"):
            return LIST_JSON
        self.details += 1
        return {"resultData": {"devId": payload["devId"], "transactions": []}}


def _run_srpe(dbp, client, **kw):
    return srpe.run_srpe(client, dbp, lambda m: None, **kw)


def _a_week_later(dbp):
    """Make the saved progress look a week old (the key must not depend on the day)."""
    with db.session(dbp) as con:
        con.execute("UPDATE run_progress SET finished_at = finished_at - INTERVAL 7 DAY")


class StopsAtSecondDetail(CountingClient):
    """Reads the first development's detail, then the run dies (e.g. the PC restarts)."""

    def post(self, endpoint, payload):
        if not endpoint.startswith("DistrictAreaSearch") and self.details == 1:
            raise RuntimeError("killed")
        return super().post(endpoint, payload)


def _empty_db(tmp_path):
    dbp = tmp_path / "t.duckdb"
    with db.session(dbp) as con:
        db.init_db(con)
    return dbp


def test_an_unfinished_run_resumed_on_a_later_day_skips_finished_details(tmp_path):
    dbp = _empty_db(tmp_path)
    with pytest.raises(RuntimeError):
        _run_srpe(dbp, StopsAtSecondDetail())
    _a_week_later(dbp)
    later = CountingClient()
    stats = _run_srpe(dbp, later)  # the same SRPE run, resumed
    assert later.details == 1  # only development 10 was left
    assert stats["left"] == 0
    with db.session(dbp) as con:
        keys = [r[0] for r in con.execute(
            "SELECT step_key FROM run_progress WHERE command = 'srpe' "
            "AND step_key LIKE 'detail:%'").fetchall()]
    # both details carry the tag of the one run they belong to
    assert sorted(k.split(":")[1] for k in keys) == ["10", "2947"]
    assert len({k.split(":")[2] for k in keys}) == 1


def test_a_new_run_after_a_completed_one_reads_every_detail_again(tmp_path):
    dbp = _empty_db(tmp_path)
    first = CountingClient()
    _run_srpe(dbp, first)
    assert first.details == 2
    again = CountingClient()
    _run_srpe(dbp, again)  # even on the same day
    assert again.details == 2  # newly published registers are seen


def test_a_limited_run_stays_open(tmp_path):
    dbp = _empty_db(tmp_path)
    first = CountingClient()
    _run_srpe(dbp, first, limit=1)
    assert first.details == 1
    rest = CountingClient()
    _run_srpe(dbp, rest)
    assert rest.details == 1  # the run resumes with development 10


def test_a_subset_run_leaves_the_run_open(tmp_path):
    # a test rebuild's subset reads only some developments; closing the run would make the
    # next full run read every detail again (pre-release review 2026-10-28)
    dbp = _empty_db(tmp_path)
    _run_srpe(dbp, CountingClient(), estates=set())
    with db.session(dbp) as con:
        msg = con.execute("SELECT detail FROM run_progress WHERE command = 'srpe' "
                          "AND step_key = ?", [srpe.RUN_STEP]).fetchone()[0]
    assert msg.startswith("open ")


def test_older_detail_keys_are_harmless(tmp_path):
    dbp = _empty_db(tmp_path)
    with db.session(dbp) as con:
        db.mark_step(con, "srpe", "detail:2947:2026-10-19", 1, "ok")  # per-day key (before 10-28)
        db.mark_step(con, "srpe", "detail:10", 1, "ok")  # undated key (Task 5)
    client = CountingClient()
    _run_srpe(dbp, client)
    assert client.details == 2  # a new run reads both; nothing fails on the old rows


def test_srpe_estates_filter_keeps_developments_in_the_subset(tmp_path):
    dbp = _empty_db(tmp_path)
    with db.session(dbp) as con:
        cm.ensure_tables(con)
        con.execute("INSERT INTO master_building (building_id, estate_code, address) "
                    "VALUES ('cd-1-A', '2-AAA', '九龍城/木翠街3號')")
    client = CountingClient()
    stats = _run_srpe(dbp, client, estates={"2-AAA"})
    assert client.details == 1 and stats["left"] == 0  # only 2947 (木翠街3號) is in 2-AAA


def test_srpe_requests_count_towards_the_process_wide_cap(monkeypatch):
    import httpx

    import hkestate.fetch as fetch_mod

    c = srpe.SrpeClient(delay_s=0, sleep=lambda s: None)
    c._http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    fetch_mod.set_request_cap(1)
    c._token = "t"
    c.post("x", {})
    assert fetch_mod.requests_counted() == 1
    with pytest.raises(fetch_mod.RequestCapReached):
        c.post("x", {})
    c.close()
