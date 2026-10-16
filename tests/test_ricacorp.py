"""Ricacorp estate pages: parsing, previous-deal pairs, placement, dedupe, the crawl."""

from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

from hkestate import db
from hkestate.fetch import Fetcher
from hkestate.sources import ricacorp as rc

FIX = Path(__file__).parent / "fixtures" / "ricacorp"
PAGE = (FIX / "estate_page.html").read_text(encoding="utf-8")
EMPTY = (FIX / "estate_page_empty.html").read_text(encoding="utf-8")

BASE = "https://www.ricacorp.com"
SLUG = "測試苑-bigest-沙田-hma-hk"
SITEMAP = f"""<urlset>
<url><loc>{BASE}/zh-hk/property/transaction/estate/%E6%B8%AC%E8%A9%A6%E8%8B%91-bigest-%E6%B2%99%E7%94%B0-hma-hk</loc></url>
<url><loc>{BASE}/zh-hk/property/transaction/estate/%E7%A9%BA%E8%8B%91-single-%E6%B2%99%E7%94%B0-hma-hk</loc></url>
<url><loc>{BASE}/zh-hk/property/transaction/building/x/uuid/y-sale-hk</loc></url>
<url><loc>{BASE}/api/zh-hk/property/transaction/estate/z-estate-a-hma-hk</loc></url>
<url><loc>{BASE}/zh-hk/property/list/landregistry</loc></url>
</urlset>"""


# ---- parsing --------------------------------------------------


def test_estate_pages_keeps_only_estate_pages_never_api():
    pages = rc.estate_pages(SITEMAP)
    assert [(p.name, p.kind) for p in pages] == [("測試苑", "bigest"), ("空苑", "single")]
    assert all("/api/" not in p.url and "/building/" not in p.url for p in pages)


def test_parse_estate_page_reads_rows():
    deals = rc.parse_estate_page(PAGE, SLUG)
    assert len(deals) == 4
    d = deals[0]
    assert d.deal_id == "26101401570075"
    assert (d.price_hkd, d.area_sqft, d.reg_date) == (8_750_000, 530.0, date(2026, 10, 14))
    assert (d.hold_days, d.gain_pct, d.source_tag) == (6491, 169.0, "registry")
    assert deals[1].gain_pct == -23.0 and deals[1].price_hkd == 10_430_000
    assert (deals[2].hold_days, deals[2].gain_pct) == (None, None)  # a first sale: '-'
    assert deals[3].source_tag == "market"
    assert len(deals[3].deal_id) == 16  # no link: hashed from the row


def test_empty_page_has_no_deals_and_agent_text_is_never_read():
    assert rc.parse_estate_page(EMPTY, "x") == []
    assert all("12345678" not in repr(d) and "某某" not in repr(d)
               for d in rc.parse_estate_page(PAGE, SLUG))


def test_parse_gain():
    assert rc.parse_gain("arrow_upward 13%") == 13.0
    assert rc.parse_gain("arrow_downward 4%") == -4.0
    assert rc.parse_gain("-") is None
    assert rc.parse_gain("arrow_upward 0%") == 0.0


def test_split_address():
    assert rc.split_address("測試苑 一期 甲閣 (1座) 19樓 A室", "測試苑") == (
        "一期 甲閣 (1座)", "19樓", "A")
    assert rc.split_address("安樂樓 5樓 B室", "安樂樓") == ("", "5樓", "B")


def test_previous_deal_from_days_held_and_gain():
    d = rc.parse_estate_page(PAGE, SLUG)[0]
    prev_date, prev_price = rc.previous_deal(d)
    assert prev_date == date(2009, 1, 5)
    assert prev_price == round(8_750_000 / 2.69)
    assert rc.previous_deal(rc.parse_estate_page(PAGE, SLUG)[2]) is None


# ---- storing and placing --------------------------------------------------


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    rc.ensure_tables(c)
    from hkestate import link

    link.ensure_tables(c)
    yield c
    c.close()


def seed_master(con):
    con.execute("INSERT INTO master_estate (code, name_zh, units, excluded, pilot) "
                "VALUES ('2-T', '測試苑', 800, false, false)")
    for code, phase, block in [("1-A", "一期", "甲閣"), ("1-B", "二期", "乙閣")]:
        con.execute("INSERT INTO master_building (building_id, estate_code, block_code, "
                    "estate_name, phase_name, block_name) VALUES (?, '2-T', ?, '測試苑', ?, ?)",
                    [f"cd-{code}", code, phase, block])


def store(con):
    page = rc.estate_pages(SITEMAP)[0]
    deals = rc.parse_estate_page(PAGE, page.slug)
    return rc.save_deals(con, page, deals, date(2026, 10, 16))


def test_save_deals_is_idempotent_and_keeps_first_seen(con):
    assert store(con) == 4
    assert store(con) == 0
    assert con.execute("SELECT count(*) FROM ricacorp_deal").fetchone()[0] == 4
    row = con.execute("SELECT floor_label, flat FROM ricacorp_deal "
                      "WHERE deal_id = '26101401570075'").fetchone()
    assert row == ("19", "A")


def test_unplaced_until_the_master_list_has_the_estate(con):
    store(con)
    stats = rc.place_deals(con, log=lambda *a: None)
    assert stats["placed"] == 0 and stats["unplaced"] == 4
    assert con.execute("SELECT count(*) FROM txn_raw WHERE source = 'ricacorp'"
                       ).fetchone()[0] == 0
    seed_master(con)
    stats = rc.place_deals(con, log=lambda *a: None)
    assert stats["placed"] == 4  # the market row has no link id but still places by block


def test_placed_deals_become_rows_and_pairs(con):
    seed_master(con)
    store(con)
    stats = rc.place_deals(con, log=lambda *a: None)
    assert stats["current_added"] == 4
    assert stats["prev_added"] == 3 and stats["pairs"] == 3  # the first sale has none
    cur = con.execute("SELECT building_id, floor_label, flat, price_hkd, deal_date, deal_type "
                      "FROM txn_raw WHERE source_id = '26101401570075'").fetchone()
    assert cur == ("cd-1-A", "19", "A", 8_750_000, date(2026, 10, 14), "registry")
    prev = con.execute("SELECT price_hkd, deal_date, floor_label, flat, building_id FROM txn_raw "
                       "WHERE source_id = '26101401570075:prev'").fetchone()
    assert prev == (round(8_750_000 / 2.69), date(2009, 1, 5), "19", "A", "cd-1-A")
    pair = con.execute("SELECT unit_id, hold_days, gain_pct, prev_derived FROM deal_pair "
                       "WHERE cur_id = '26101401570075'").fetchone()
    assert pair == ("cd-1-A|19|A", 6491, 169.0, True)
    # both ends are on the same unit in the clean deal view
    units = {r[0] for r in con.execute(
        "SELECT unit_id FROM txn_all WHERE source = 'ricacorp' AND floor_label = '19'").fetchall()}
    assert units == {"cd-1-A|19|A"}


def test_second_placement_adds_nothing(con):
    seed_master(con)
    store(con)
    rc.place_deals(con, log=lambda *a: None)
    n = con.execute("SELECT count(*) FROM txn_raw").fetchone()[0]
    stats = rc.place_deals(con, log=lambda *a: None)
    assert con.execute("SELECT count(*) FROM txn_raw").fetchone()[0] == n
    assert stats["pairs"] == 0


def test_deals_already_held_by_28hse_or_centadata_are_not_stored_again(con):
    seed_master(con)
    # 28Hse knows the 2026 sale (in a mapped 28Hse building) and Centadata the 2009 one
    con.execute("INSERT INTO source_map VALUES ('hse28', 'building', 'h28-9-b1', 'cd-1-A', '', "
                "'name', now())")
    for src, sid, bid, day, price in [
        ("hse28", "u1:2026-10-14", "h28-9-b1", date(2026, 10, 14), 8_750_000),
        ("centadata", "cd1", "cd-1-A", date(2009, 1, 20), 3_260_000),  # within 1% of derived
    ]:
        con.execute("INSERT INTO txn_raw (source, source_id, fetched_at, row_hash, building_id, "
                    "floor_label, flat, deal_date, price_hkd) VALUES (?, ?, ?, ?, ?, '19', 'A',"
                    " ?, ?)", [src, sid, datetime(2026, 10, 16), sid, bid, day, price])
    store(con)
    stats = rc.place_deals(con, log=lambda *a: None)
    assert stats["current_existing"] == 1 and stats["prev_existing"] == 1
    assert con.execute("SELECT count(*) FROM txn_raw WHERE source = 'ricacorp' "
                       "AND source_id LIKE '26101401570075%'").fetchone()[0] == 0
    pair = con.execute("SELECT cur_source, prev_source, prev_price_hkd, prev_derived "
                       "FROM deal_pair WHERE cur_id = 'u1:2026-10-14'").fetchone()
    assert pair == ("hse28", "centadata", 3_260_000, False)


def test_ambiguous_block_stays_unplaced(con):
    con.execute("INSERT INTO master_estate (code, name_zh, units, excluded, pilot) "
                "VALUES ('2-T', '測試苑', 800, false, false)")
    for code in ("1-A", "1-B"):  # two buildings with the same block name and no phase
        con.execute("INSERT INTO master_building (building_id, estate_code, block_code, "
                    "estate_name, phase_name, block_name) VALUES (?, '2-T', ?, '測試苑', '', "
                    "'甲閣')", [f"cd-{code}", code])
    store(con)
    stats = rc.place_deals(con, log=lambda *a: None)
    assert stats["placed"] == 0


# ---- the crawl --------------------------------------------------


def make_fetcher(tmp_path):
    seen = []

    def handler(request):
        seen.append(str(request.url.path))
        p = request.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")
        if p.endswith("sitemap-0.xml"):
            return httpx.Response(200, text=SITEMAP)
        if "%E7%A9%BA" in str(request.url) or "空苑" in p:
            return httpx.Response(200, text=EMPTY)
        return httpx.Response(200, text=PAGE)

    f = Fetcher(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler),
                sleep=lambda s: None, jitter=lambda: 0.0)
    return f, seen


def test_crawl_stores_rows_prunes_cache_resumes_and_never_calls_api(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
    f, seen = make_fetcher(tmp_path)
    stats = rc.run_ricacorp(f, path, log=lambda *a: None, today=date(2026, 10, 16))
    assert stats["pages"] == 2 and stats["deals"] == 4 and stats["new"] == 4
    assert not any("/api/" in s for s in seen)
    pages = [p for p in (tmp_path / "cache").rglob("*.html.gz")]
    # only the sitemap is kept; the two estate pages were deleted after parsing
    assert len(pages) == 1
    again, seen2 = make_fetcher(tmp_path)
    stats = rc.run_ricacorp(again, path, log=lambda *a: None, today=date(2026, 10, 16))
    assert stats["pages"] == 0  # this week's pass is complete
    assert not any("estate" in s for s in seen2)
    # next week: a new pass re-reads the pages; the stored deals are not duplicated
    nxt, _ = make_fetcher(tmp_path)
    stats = rc.run_ricacorp(nxt, path, log=lambda *a: None, today=date(2026, 10, 23))
    assert stats["pages"] == 2 and stats["new"] == 0
    with db.session(path) as con:
        assert con.execute("SELECT count(*) FROM ricacorp_deal").fetchone()[0] == 4
        assert con.execute("SELECT status FROM run_log WHERE command = 'ricacorp' "
                           "ORDER BY run_id LIMIT 1").fetchone() == ("ok",)


def test_crawl_stops_after_max_hours(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
    f, _ = make_fetcher(tmp_path)
    ticks = iter([0, 0, 99999, 99999, 99999])
    stats = rc.run_ricacorp(f, path, log=lambda *a: None, max_hours=1,
                            clock=lambda: next(ticks), today=date(2026, 10, 16))
    assert stats["pages"] == 1
    with db.session(path) as con:
        assert con.execute("SELECT status FROM run_log WHERE command = 'ricacorp'"
                           ).fetchone() == ("partial",)


def test_timeout_on_one_page_is_counted_and_the_pass_goes_on(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)

    def handler(request):
        p = request.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")
        if p.endswith("sitemap-0.xml"):
            return httpx.Response(200, text=SITEMAP)
        if "%E7%A9%BA" in str(request.url):
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, text=PAGE)

    f = Fetcher(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler),
                sleep=lambda s: None, jitter=lambda: 0.0)
    stats = rc.run_ricacorp(f, path, log=lambda *a: None, today=date(2026, 10, 16))
    assert stats["pages"] == 1 and stats["errors"] == 1
