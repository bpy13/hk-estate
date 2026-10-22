"""Territory listings: matching to master buildings, Centanet/Spacious page lists, finishing."""

from datetime import date, datetime

import pytest

from hkestate import db, link, listing_run, match
from hkestate.listing_types import Listing
from hkestate.sources import centanet, spacious

TODAY = date(2026, 10, 16)


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    link.ensure_tables(c)
    spacious.ensure_tables(c)
    yield c
    c.close()


def seed(con):
    con.execute("INSERT INTO master_estate (code, name_zh, units, excluded, pilot) VALUES "
                "('2-S', '海景苑', 800, false, false), ('2-H', '居屋苑', 900, true, false)")
    for code, block, floors in [("1-A", "甲座", 30), ("1-B", "乙座", 30)]:
        con.execute("INSERT INTO master_building (building_id, estate_code, block_code, "
                    "estate_name, phase_name, block_name, floors) VALUES (?, '2-S', ?, '海景苑',"
                    " '', ?, ?)", [f"cd-{code}", code, block, floors])


def test_block_code_names_the_building(con):
    seed(con)
    m = match.TerritoryMatcher(con)
    assert m.match("海景苑 甲座", block_key="1-B") == ("cd-1-B", "block_code")
    assert m.match("x", block_key="1-ZZZ") == (None, "none")  # unknown block, unknown text


def test_estate_name_then_block_name(con):
    seed(con)
    m = match.TerritoryMatcher(con)
    assert m.match("海景苑 乙座", estate="海景苑") == ("cd-1-B", "exact")
    assert m.match("海景苑 甲座")[0] == "cd-1-A"  # estate found from the start of the text
    assert m.match("居屋苑 1座")[0] is None  # excluded estates are not matched
    assert m.top_floor("cd-1-A") == 30


def test_dedupe_buckets_give_the_same_groups():
    rows = [
        {"source": "centanet", "listing_id": "1", "kind": "sale", "building_id": "b",
         "floor_band": "high", "saleable_sqft": 500},
        {"source": "hse28", "listing_id": "2", "kind": "sale", "building_id": "b",
         "floor_band": "high", "saleable_sqft": 505},
        {"source": "spacious", "listing_id": "3", "kind": "sale", "building_id": "b",
         "floor_band": "unknown", "saleable_sqft": 502},
        {"source": "centanet", "listing_id": "4", "kind": "sale", "building_id": "c",
         "floor_band": "high", "saleable_sqft": 500},
    ]
    g = match.dedupe(rows)
    assert g[("centanet", "1")] == g[("hse28", "2")] == g[("spacious", "3")]
    assert g[("centanet", "4")] != g[("centanet", "1")]


# ---- page lists ---------------------------------------------------------------------------------


def test_centanet_sitemap_sends_other_estates_to_the_default_group():
    xml = ("<urlset><url><loc>https://hk.centanet.com/findproperty/detail/"
           "%E5%A4%AA%E5%8F%A4%E5%9F%8E-%E7%94%B2_ABC123?theme=buy</loc></url>"
           "<url><loc>https://hk.centanet.com/findproperty/detail/"
           "%E6%B5%B7%E6%99%AF%E8%8B%91-%E7%94%B2%E5%BA%A7_DEF456?theme=rent</loc></url></urlset>")
    only_pilot = centanet.sitemap_entries(xml, {"太古城": "taikoo-shing"})
    assert [e.group for e in only_pilot] == ["taikoo-shing"]
    both = centanet.sitemap_entries(xml, {"太古城": "taikoo-shing"}, "territory")
    assert [(e.listing_id, e.group, e.kind) for e in both] == [
        ("ABC123", "taikoo-shing", "sale"), ("DEF456", "territory", "rent")]


SP_SITEMAP = """<urlset>
<url><loc>https://www.spacious.hk/zh-tw/%E9%A6%99%E6%B8%AF/n/77-%E6%B2%99%E7%94%B0/b/106466--%E6%BF%B1%E6%99%AF%E8%8A%B1%E5%9C%92/%E8%B2%B7%E6%A8%93</loc></url>
<url><loc>https://www.spacious.hk/zh-tw/%E9%A6%99%E6%B8%AF/n/77-%E6%B2%99%E7%94%B0/b/106466--%E6%BF%B1%E6%99%AF%E8%8A%B1%E5%9C%92/%E7%A7%9F%E5%B1%8B</loc></url>
<url><loc>https://www.spacious.hk/zh-tw/%E5%B7%A5%E5%95%86%E8%88%96/c/%E9%A6%99%E6%B8%AF/n/36-x/b/67--y/%E5%87%BA%E7%A7%9F</loc></url>
<url><loc>https://www.spacious.hk/zh-cn/%E9%A6%99%E6%B8%AF/n/77-x/b/9--y/%E4%B9%B0%E6%A5%BC</loc></url>
<url><loc>https://www.spacious.hk/zh-tw/%E9%A6%99%E6%B8%AF/n/111-%E9%BB%83/%E6%8E%83/b/6074--z/%E8%B2%B7%E6%A8%93</loc></url>
</urlset>"""


def test_building_sale_pages_keep_residential_traditional_sale_pages_only():
    pages = spacious.building_sale_pages(SP_SITEMAP)
    assert [(p.neighbourhood_id, p.building_id) for p in pages] == [(77, 106466), (111, 6074)]
    assert all(p.url.startswith("https://www.spacious.hk/zh-tw/") for p in pages)


def test_plan_reads_puts_active_buildings_first_then_unread(con):
    spacious.add_buildings(con, [spacious.BuildingPage(1, i, f"u{i}") for i in (1, 2, 3, 4)],
                           TODAY)
    spacious.record_read(con, 3, date(2026, 10, 15), 4)  # had listings
    spacious.record_read(con, 2, date(2026, 10, 10), 0)  # read, empty
    order = [b for b, _ in spacious.plan_reads(con, 10)]
    assert order == [3, 1, 4, 2]
    assert [b for b, _ in spacious.plan_reads(con, 2)] == [3, 1]


def test_spacious_listing_is_delisted_after_three_reads_without_it(con):
    spacious.add_buildings(con, [spacious.BuildingPage(1, 7, "u")], TODAY)
    con.execute("INSERT INTO listing (source, listing_id, group_name, status, last_seen) "
                "VALUES ('spacious', 'L1', 'territory', 'active', ?)", [date(2026, 10, 10)])
    con.execute("INSERT INTO listing_raw (source, listing_id, fetched_at, source_building_key) "
                "VALUES ('spacious', 'L1', now(), '7')")
    for d in (11, 12):
        spacious.record_read(con, 7, date(2026, 10, d), 0)
    assert spacious.mark_delisted(con, TODAY) == 0
    spacious.record_read(con, 7, date(2026, 10, 13), 0)
    assert spacious.mark_delisted(con, TODAY) == 1
    assert con.execute("SELECT status FROM listing WHERE listing_id = 'L1'").fetchone() == (
        "delisted",)


# ---- finishing and matching in the database -----------------------------------------------------


def test_territory_run_records_matches_and_snapshots(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
        link.ensure_tables(con)
        seed(con)
    run = listing_run.ListingRun(db_path=path, log=lambda *a: None, today=TODAY,
                                 now=datetime(2026, 10, 16, 9))
    items = [
        Listing(source="centanet", listing_id="C1", kind="sale", url="u1", estate_name="海景苑",
                building_text="甲座", floor_text="25樓", flat="A", price_hkd=9_000_000,
                saleable_sqft=500.0, source_building_key="1-A"),
        Listing(source="centanet", listing_id="C2", kind="sale", url="u2",
                estate_name="不明苑", building_text="1座", price_hkd=5_000_000,
                saleable_sqft=400.0),
    ]
    run._store("territory", items, "detail")
    res = listing_run.SourceResult("centanet", "territory", seen=["C1", "C2"], parsed=items,
                                   detail_read={"C1", "C2"})
    run.results.append(res)
    summary = run.finish()
    assert summary["sources"]["centanet"]["territory"]["new"] == 2
    m = summary["match"]["territory"]
    assert (m["listings"], m["matched"]) == (2, 1)
    with db.session(path) as con:
        row = con.execute("SELECT building_id, match_method, floor_band FROM listing "
                          "WHERE listing_id = 'C1'").fetchone()
        assert row == ("cd-1-A", "block_code", "high")
        assert con.execute("SELECT building_id FROM listing_snapshot WHERE listing_id = 'C1'"
                           ).fetchone() == ("cd-1-A",)
        assert con.execute("SELECT count(*) FROM listing_run_day WHERE scope = 'territory'"
                           ).fetchone()[0] == 1
    # the master list grows: the unmatched listing is picked up on a later run
    with db.session(path) as con:
        con.execute("INSERT INTO master_estate (code, name_zh, units, excluded, pilot) "
                    "VALUES ('2-N', '不明苑', 100, false, false)")
        con.execute("INSERT INTO master_building (building_id, estate_code, block_code, "
                    "estate_name, block_name, floors) VALUES ('cd-1-N', '2-N', '1-N', '不明苑', "
                    "'1座', 20)")
    listing_run.match_territory(path, TODAY, log=lambda *a: None)
    with db.session(path) as con:
        assert con.execute("SELECT building_id FROM listing WHERE listing_id = 'C2'"
                           ).fetchone() == ("cd-1-N",)


def test_rematch_drops_old_contains_and_single_matches_the_rules_now_reject(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
        link.ensure_tables(con)
        seed(con)
        con.execute("INSERT INTO master_estate (code, name_zh, units, excluded, pilot) "
                    "VALUES ('2-X', '孤島苑', 100, false, false)")
        con.execute("INSERT INTO master_building (building_id, estate_code, block_code, "
                    "estate_name, block_name, floors) VALUES ('cd-1-X', '2-X', '1-X', '孤島苑', "
                    "'孤島苑', 20)")
        # (listing, text, building it holds from an older, looser rule set, how it got it)
        for lid, text, bid, how in [
            ("K1", "海景苑灣 甲座", "cd-1-A", "contains"),  # a longer estate name: no match
            ("K2", "孤島苑 榮盛閣", "cd-1-X", "single"),  # a block the estate lacks
            ("K3", "海景苑灣 甲座", "cd-1-A", "manual"),  # kept: manual
            ("K4", "海景苑灣 甲座", "cd-1-A", "exact"),  # kept: exact
            ("K5", "海景苑 甲座", "cd-1-A", "contains"),  # still fine: unchanged
        ]:
            con.execute("INSERT INTO listing (source, listing_id, kind, group_name, "
                        "building_text, building_id, match_method) "
                        "VALUES ('centanet', ?, 'sale', 'territory', ?, ?, ?)",
                        [lid, text, bid, how])
    listing_run.match_territory(path, TODAY, log=lambda *a: None)
    with db.session(path) as con:
        got = dict((lid, (b, m)) for lid, b, m in con.execute(
            "SELECT listing_id, building_id, match_method FROM listing").fetchall())
    assert got["K1"] == (None, "none")
    assert got["K2"] == (None, "check_match")
    assert got["K3"] == ("cd-1-A", "manual")
    assert got["K4"] == ("cd-1-A", "exact")
    assert got["K5"][0] == "cd-1-A"


def test_territory_only_run_leaves_pilot_listings_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))  # the `centanet` lock file
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
    xml = ("<urlset><url><loc>https://hk.centanet.com/findproperty/detail/"
           "%E5%A4%AA%E5%8F%A4%E5%9F%8E-%E7%94%B2_ABC123?theme=buy</loc></url></urlset>")

    class F:
        requests = []

        def get(self, url, page_type):
            self.requests.append(url)
            return xml

        def close(self):
            pass

    run = listing_run.ListingRun(db_path=path, log=lambda *a: None, fetcher=F(), today=TODAY)
    run.centanet([listing_run.TERRITORY])
    assert F.requests == [centanet.SITEMAP_URL]  # the pilot listing was not fetched or taken
    assert run.results[0].seen == []
