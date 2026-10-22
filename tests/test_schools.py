"""Primary school nets: school list parsers, storage and the building -> net assignment."""

from pathlib import Path

import pytest

from hkestate import db
from hkestate.sources import cd_master as cm
from hkestate.sources import schools

FIX = Path(__file__).parent / "fixtures" / "schools"


@pytest.fixture
def pages():
    return (FIX / "lists_pages.txt").read_text(encoding="utf-8").split("=====PAGE\n")


@pytest.fixture
def csv_bytes():
    return (FIX / "sch_loc.csv").read_bytes()


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    cm.ensure_tables(c)
    schools.ensure_tables(c)
    yield c
    c.close()


def test_net_lists_cover_every_header_layout(pages):
    nets = schools.parse_net_lists(pages)
    assert nets == {
        "132853": "11", "510882": "11", "511382": "11",  # "NN ENGLISH NAME" header
        "170054": "12",
        "170151": "12",  # on a continuation page without its own header
        "510084": "18",  # the number alone on a line, after a long wrapped name
    }


def test_index_page_is_not_a_net_header(pages):
    # the contents page has "POA School Net Areas Covered" and numbers: no school rows follow
    assert schools.parse_net_lists(pages[:1]) == {}


def test_locations_keep_primary_schools_only(csv_bytes):
    rows = schools.parse_locations(csv_bytes)
    by_no = {r.school_no: r for r in rows}
    assert "888888000123" not in by_no  # secondary
    assert len(rows) == 7
    r = by_no["132853000123"]
    assert (r.name_zh, r.lat, r.lon, r.finance) == ("聖嘉祿學校", 22.284, 114.129, "AIDED")


def test_location_without_coordinates_is_skipped(csv_bytes):
    bad = csv_bytes.decode("utf-8-sig").replace("22.28400000,22.28400000", "N.A.,N.A.", 1)
    assert len(schools.parse_locations(bad.encode("utf-8"))) == 6


def test_join_nets_by_first_six_digits(pages, csv_bytes):
    rows = schools.attach_nets(schools.parse_locations(csv_bytes), schools.parse_net_lists(pages))
    nets = {r.school_no[:6]: r.net for r in rows}
    assert nets["132853"] == "11"
    assert nets["999999"] is None  # direct subsidy school: outside the Primary One system


def _stored(con, pages, csv_bytes):
    rows = schools.attach_nets(schools.parse_locations(csv_bytes), schools.parse_net_lists(pages))
    return schools.store(con, rows)


def test_store_replaces_previous_rows(con, pages, csv_bytes):
    assert _stored(con, pages, csv_bytes) == 7
    assert _stored(con, pages, csv_bytes) == 7
    assert con.execute("SELECT count(*) FROM primary_school").fetchone()[0] == 7


def test_assign_nets_most_common_of_three_nearest(con, pages, csv_bytes):
    _stored(con, pages, csv_bytes)
    con.execute("INSERT INTO master_building (building_id, lat, lon) VALUES "
                "('kt', 22.2845, 114.1300),"   # Kennedy Town: three net-11 schools
                "('wc', 22.2720, 114.1720),"   # Wan Chai: nets 12, 12, 18 -> 12
                "('none', NULL, NULL)")
    assert schools.assign_nets(con) == 2
    got = dict(con.execute("SELECT building_id, school_net FROM master_building").fetchall())
    assert got == {"kt": "11", "wc": "12", "none": None}


def test_assign_nets_ignores_schools_without_a_net_and_breaks_ties_by_distance(con):
    con.execute("INSERT INTO primary_school (school_no, name_zh, lat, lon, net) VALUES "
                "('1', 'a', 22.300, 114.170, '31'), ('2', 'b', 22.301, 114.170, '32'),"
                "('3', 'c', 22.3005, 114.170, '33'), ('4', 'dss', 22.30001, 114.170, NULL)")
    con.execute("INSERT INTO master_building (building_id, lat, lon) VALUES ('x', 22.3, 114.17)")
    schools.assign_nets(con)
    # three different nets: one vote each, the nearest school's net wins; the DSS school
    # (no net) is not a candidate even though it is closest
    assert con.execute("SELECT school_net FROM master_building").fetchone()[0] == "31"


def test_assign_nets_changes_nothing_without_school_data(con):
    con.execute("INSERT INTO master_building (building_id, lat, lon, school_net) "
                "VALUES ('x', 22.3, 114.17, '99')")
    assert schools.assign_nets(con) == 0
    assert con.execute("SELECT school_net FROM master_building").fetchone()[0] == "99"


def test_load_uses_fetcher_and_stores(tmp_path, pages, csv_bytes, monkeypatch):
    class FakeFetcher:
        def get_bytes(self, url, page_type):
            return csv_bytes if url.endswith(".csv") else b"%PDF fake"

    monkeypatch.setattr(schools, "pdf_pages", lambda content: pages)
    path = tmp_path / "t.duckdb"
    with db.session(path) as c:
        db.init_db(c)
        cm.ensure_tables(c)
        c.execute("INSERT INTO master_building (building_id, lat, lon) "
                  "VALUES ('kt', 22.2845, 114.13)")
    messages = []
    assert schools.load(FakeFetcher(), path, log=messages.append) == 1
    with db.session(path) as c:
        assert c.execute("SELECT school_net FROM master_building").fetchone()[0] == "11"
    assert messages


def test_load_refuses_a_list_with_too_few_schools(tmp_path, csv_bytes, monkeypatch):
    class FakeFetcher:
        def get_bytes(self, url, page_type):
            return csv_bytes if url.endswith(".csv") else b"x"

    monkeypatch.setattr(schools, "pdf_pages", lambda content: ["nothing useful"])
    with pytest.raises(schools.LayoutChanged):
        schools.load(FakeFetcher(), tmp_path / "t.duckdb", log=lambda m: None)
