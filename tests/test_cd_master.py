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


def nuxt_page(*, estate="測試苑", phase="", block="1座", code="2-AAAAAAAAAA",
              block_code="1-BBBBBBBBBB",
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
        # the estate-level flag alone no longer excludes: blocks are judged one by one
        ({"is_house": True}, None),
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


class Pages:
    """Serves one estate page with two blocks; records each request."""

    def __init__(self):
        self.asked = []

    def get(self, url, page_type):
        self.asked.append(url)
        if url == cm.SITEMAP_URL:
            return SITEMAP
        return nuxt_page(block_code="1-B1", blocks=[("1-B1", "1座"), ("1-B2", "2座")])


def test_process_estate_pauses_before_every_page(tmp_path):
    with db.session(tmp_path / "t.duckdb") as c:
        db.init_db(c)
        cm.ensure_tables(c)
    f, order = Pages(), []
    real_get = f.get
    f.get = lambda url, pt: order.append("get") or real_get(url, pt)
    cm.process_estate(f, tmp_path / "t.duckdb", "2-AAAAAAAAAA", "測試苑", pilot=False,
                      overrides={}, pause=lambda: order.append("pause"))
    assert order == ["pause", "get", "pause", "get"]  # the estate page, then block 1-B2


def test_master_pass_runs_alongside_centanet_listings(tmp_path, monkeypatch):
    # since 2026-10-20 no pause on the `centanet` lock: each job keeps its own pace
    from hkestate import locks
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cm, "pilot_codes", lambda f, log: set())
    monkeypatch.setattr(cm, "load_overrides", lambda: {})
    monkeypatch.setattr(locks, "held_by", lambda name: 1234 if name == "centanet" else None)
    f, waits = Pages(), []
    cm.run_master(f, db_path=tmp_path / "t.duckdb", log=lambda m: None, wait=waits.append)
    assert waits == [] and len(f.asked) >= 2


def test_master_pass_cools_down_and_retries_the_same_estate_on_429(tmp_path, monkeypatch):
    from hkestate.fetch import FetchError
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cm, "pilot_codes", lambda f, log: set())
    monkeypatch.setattr(cm, "load_overrides", lambda: {})
    f, waits = Pages(), []
    real_get = f.get
    fails = iter([True, True])

    def get(url, pt):
        if url != cm.SITEMAP_URL and next(fails, False):
            f.asked.append(url)
            raise FetchError(url, 429)
        return real_get(url, pt)

    f.get = get
    stats = cm.run_master(f, db_path=tmp_path / "t.duckdb", log=lambda m: None,
                          wait=waits.append)
    assert waits == [600, 1200] and stats["errors"] == 2 and stats["estates"] >= 1
    pages = [u for u in f.asked if u != cm.SITEMAP_URL]
    assert pages[0] == pages[1] == pages[2]  # the same estate page until it worked


# ---- house blocks inside house-flagged estates (plans/2026-10-23-house-flag-estates.md) -------


@pytest.mark.parametrize(
    "name, floors, own_flag, expected",
    [
        ("1座", 30, None, False),             # entry block of a tower: its flag is the estate's
        ("2座", 25, False, False),            # tower block page, own flag false
        ("洋房15", 1, True, True),            # villa word
        ("2座 (House B,block 1)", 1, True, True),
        ("3座", 2, False, True),              # low-rise: the flag misses it (錦繡花園 2座)
        ("1座", 3, None, True),               # floors 1-3 is a house, entry block or not
        ("4座", 4, False, False),
        ("5座", None, True, True),            # no floors figure: a non-entry block's own flag
        ("5座", None, None, False),           # ... but the entry block's flag is the estate's
        ("Penthouse Tower", 30, False, False),  # duplexes and penthouses are kept
    ],
)
def test_house_block_rule(name, floors, own_flag, expected):
    assert cm.is_house_block(name, floors, own_flag) is expected


class EstatePages:
    """Serves an estate page (its entry block) and block pages by code; records requests."""

    def __init__(self, pages):
        self.pages = pages  # block or estate code -> html
        self.asked = []

    def get(self, url, page_type):
        self.asked.append(url)
        if url == cm.SITEMAP_URL:
            return SITEMAP_HOUSE
        for code, html in self.pages.items():
            if url.endswith(code):
                return html
        raise AssertionError(f"unexpected request {url}")


SITEMAP_HOUSE = """<urlset>
 <url><loc>https://hk.centanet.com/estate/%E6%B7%B7%E5%90%88/2-AAAAAAAAAA</loc></url>
 <url><loc>https://hk.centanet.com/estate/%E5%88%A5%E5%A2%85/2-CCCCCCCCCC</loc></url>
 <url><loc>https://hk.centanet.com/estate/%E6%AD%A3%E5%B8%B8/2-DDDDDDDDDD</loc></url>
</urlset>"""

MENU = [("1-TOWERAAAAA", "1座"), ("1-TOWERBBBBB", "2座"), ("1-HOUSEAAAAA", "3座"),
        ("1-VILLAAAAAA", "洋房5")]


def deal(tid):
    return [(tid, "10", "A", 5_000_000, 400, 520, "2020-01-02T00:00:00", "2020-02-02T00:00:00")]


def mixed_estate_pages():
    """A house-flagged estate: two towers, a two-storey house the flag misses, a villa."""
    common = dict(code="2-AAAAAAAAAA", is_house=True, blocks=MENU)
    return {
        "2-AAAAAAAAAA": nuxt_page(block_code="1-TOWERAAAAA", floors=30, deals=deal("TA"), **common),
        "1-TOWERBBBBB": nuxt_page(block_code="1-TOWERBBBBB", floors=25,
                                  deals=deal("TB"), **{**common, "is_house": False}),
        "1-HOUSEAAAAA": nuxt_page(block_code="1-HOUSEAAAAA", floors=2, units=4,
                                  deals=deal("HA"), **{**common, "is_house": False}),
        # the villa's page is never requested: its menu name has a house word
    }


def run_estate(tmp_path, pages):
    path = tmp_path / "t.duckdb"
    with db.session(path) as c:
        db.init_db(c)
        cm.ensure_tables(c)
    f = EstatePages(pages)
    result = cm.process_estate(f, path, "2-AAAAAAAAAA", "混合", pilot=False, overrides={})
    return path, f, result


def test_mixed_estate_loads_towers_and_skips_house_blocks(tmp_path):  # A1
    path, f, result = run_estate(tmp_path, mixed_estate_pages())
    assert result == (None, 2, 2, 2)  # reason, blocks loaded, deals, house blocks skipped
    with db.session(path) as c:
        blocks = {r[0] for r in c.execute("SELECT block_code FROM master_building").fetchall()}
        assert blocks == {"1-TOWERAAAAA", "1-TOWERBBBBB"}
        assert c.execute("SELECT count(*) FROM txn_raw WHERE source_id = 'HA'").fetchone()[0] == 0
        row = c.execute("SELECT excluded, exclude_reason, blocks FROM master_estate").fetchone()
        assert row == (False, None, 2)
    assert not any(u.endswith("1-VILLAAAAAA") for u in f.asked)  # skipped without a request


def test_all_house_estate_loads_nothing(tmp_path):  # A2
    common = dict(code="2-AAAAAAAAAA", is_house=True, floors=2, units=4,
                  blocks=[("1-HOUSEAAAAA", "1座"), ("1-HOUSEBBBBB", "2座")])
    pages = {
        "2-AAAAAAAAAA": nuxt_page(block_code="1-HOUSEAAAAA", deals=deal("H1"), **common),
        "1-HOUSEBBBBB": nuxt_page(block_code="1-HOUSEBBBBB", deals=deal("H2"),
                                  **{**common, "is_house": False}),
    }
    path, _, result = run_estate(tmp_path, pages)
    assert result == ("house", 0, 0, 2)
    with db.session(path) as c:
        assert c.execute("SELECT count(*) FROM master_building").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM txn_raw").fetchone()[0] == 0
        assert c.execute("SELECT exclude_reason FROM master_estate").fetchone()[0] == "house"


def seed_master(path):
    """Three master estates: one excluded as house, one as hos, one kept."""
    with db.session(path) as c:
        db.init_db(c)
        cm.ensure_tables(c)
        cm.save_estate(c, info(code="2-AAAAAAAAAA"), excluded="house", blocks=0, pilot=False)
        cm.save_estate(c, info(code="2-CCCCCCCCCC"), excluded="hos", blocks=0, pilot=False)
        cm.save_estate(c, info(code="2-DDDDDDDDDD"), excluded=None, blocks=3, pilot=False)


def test_recheck_house_takes_only_house_excluded_estates_and_resumes(tmp_path, monkeypatch):  # A4
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cm, "pilot_codes", lambda f, log: set())
    monkeypatch.setattr(cm, "load_overrides", lambda: {})
    path = tmp_path / "t.duckdb"
    seed_master(path)
    f = EstatePages(mixed_estate_pages())
    stats = cm.run_master(f, db_path=path, log=lambda m: None, recheck_house=True)
    assert stats["estates"] == 1 and stats["blocks"] == 2 and stats["house_blocks"] == 2
    pages = [u for u in f.asked if u != cm.SITEMAP_URL]
    assert pages and all("2-CCCCCCCCCC" not in u and "2-DDDDDDDDDD" not in u for u in pages)
    with db.session(path) as c:
        steps = {r[0] for r in c.execute(
            "SELECT step_key FROM run_progress WHERE command = 'master'").fetchall()}
        assert steps == {"house:2-AAAAAAAAAA"}  # the normal master steps are untouched
        # the estate was re-saved as kept; the other two keep their reasons
        reasons = dict(c.execute("SELECT code, exclude_reason FROM master_estate").fetchall())
        assert reasons == {"2-AAAAAAAAAA": None, "2-CCCCCCCCCC": "hos", "2-DDDDDDDDDD": None}
    again = EstatePages(mixed_estate_pages())
    stats = cm.run_master(again, db_path=path, log=lambda m: None, recheck_house=True)
    assert stats["estates"] == 0
    assert [u for u in again.asked if u != cm.SITEMAP_URL] == []  # nothing fetched twice


def test_previous_deal_change_is_stored_with_the_latest_deal(con):
    deals = [("T9", "30", "B", 5_500_000, 405, 520, "2019-05-17T00:00:00", "2019-06-10T00:00:00")]
    html = nuxt_page(deals=deals).replace('"transactionPrice": 5500000',
                                          '"transactionPrice": 5500000, "gainPercent": 10.0')
    load(con, html)
    assert con.execute("SELECT gain_pct FROM txn_raw WHERE source_id = 'T9'").fetchone()[0] == 10.0
    # rows loaded before this column was filled are repaired from their raw_json
    con.execute("UPDATE txn_raw SET gain_pct = NULL")
    assert cm.backfill_gain_pct(con) == 1
    assert con.execute("SELECT gain_pct FROM txn_raw").fetchone()[0] == 10.0
    assert cm.prev_price(5_500_000, 10.0) == 5_000_000
    assert cm.prev_price(5_500_000, None) is None
