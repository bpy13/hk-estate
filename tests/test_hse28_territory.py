"""Territory 28Hse backfill: estate list, largest-first order, resuming, linking."""

from pathlib import Path

import pytest

from hkestate import db, link
from hkestate import hse28_territory as ht
from hkestate.sources import hse28
from tests.test_backfill import FakeFetcher, site

SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex>
<sitemap><loc>https://www.28hse.com/sitemap_share/sitemap-property-1.xml</loc></sitemap>
<sitemap><loc>https://www.28hse.com/sitemap_share/sitemap-estate-1.xml</loc></sitemap>
</sitemapindex>"""
SITEMAP_ESTATE = """<urlset>
<url><loc>https://www.28hse.com/estate/detail/%E5%A4%AA%E5%8F%A4%E5%9F%8E-1140</loc></url>
<url><loc>https://www.28hse.com/estate/detail/%E6%B8%AC%E8%A9%A6%E8%8B%91-5555/transaction</loc></url>
<url><loc>https://www.28hse.com/estate/detail/%E6%B8%AC%E8%A9%A6%E8%8B%91-5555</loc></url>
<url><loc>https://www.28hse.com/buy/apartment/property-1</loc></url>
</urlset>"""


@pytest.fixture
def dbpath(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
        link.ensure_tables(con)
    return path


def test_parse_sitemap_reads_estates_and_child_sitemaps():
    children, estates = ht.parse_sitemap(SITEMAP_INDEX)
    assert len(children) == 2 and estates == []
    _, estates = ht.parse_sitemap(SITEMAP_ESTATE)
    assert [(e.name, e.id) for e in estates] == [("太古城", 1140), ("測試苑", 5555)]


def test_discover_reads_gzipped_child_sitemaps_whatever_they_are_called():
    """Real layout (checked 2026-10-16): the index names sitemap1..3.xml.gz, none with "estate"
    in the address; estate pages (and office estates, which are not wanted) sit among others."""
    import gzip
    index = ("<sitemapindex>" + "".join(
        f"<sitemap><loc>https://www.28hse.com/sitemap{i}.xml.gz</loc></sitemap>"
        for i in (1, 2)) + "</sitemapindex>")
    other = "<urlset><url><loc>https://www.28hse.com/news/x-1</loc></url></urlset>"
    mixed = SITEMAP_ESTATE.replace(
        "</urlset>",
        "<url><loc>https://www.28hse.com/estate/office/detail/%E8%BE%A6-9</loc></url></urlset>")
    pages = {ht.SITEMAP_INDEX: index,
             "https://www.28hse.com/sitemap1.xml.gz": gzip.compress(other.encode()),
             "https://www.28hse.com/sitemap2.xml.gz": gzip.compress(mixed.encode())}
    found = ht.discover(FakeFetcher(pages), log=lambda *a: None)
    assert {e.id for e in found} == {1140, 5555}


def test_add_estates_keeps_existing_rows(dbpath):
    with db.session(dbpath) as con:
        assert ht.add_estates(con, [hse28.EstateRef("甲苑", 1)], "csv") == 1
        con.execute("UPDATE hse28_estate SET size_flats = 5")
        assert ht.add_estates(con, [hse28.EstateRef("甲苑", 1), hse28.EstateRef("乙苑", 2)],
                              "sitemap") == 1
        assert con.execute("SELECT size_flats FROM hse28_estate WHERE id = 1").fetchone() == (5,)


def seed_sized(con):
    """Four estates: two matched (sizes 100 / 900), one unmatched, one excluded."""
    for code, name, units, ex in [("2-S", "小苑", 100, False), ("2-L", "大苑", 900, False),
                                  ("2-H", "居屋苑", 5000, True)]:
        con.execute("INSERT INTO master_estate (code, name_zh, units, excluded, pilot) "
                    "VALUES (?, ?, ?, ?, false)", [code, name, units, ex])
    named = [(1, "小苑"), (2, "大苑"), (3, "無名樓"), (4, "居屋苑"), (1140, "太古城")]
    ht.add_estates(con, [hse28.EstateRef(n, i) for i, n in named], "csv")
    link.map_estates(con)


def test_plan_is_largest_first_skips_excluded_and_pilot(dbpath):
    with db.session(dbpath) as con:
        seed_sized(con)
        assert [e.id for e in ht.plan(con)] == [2, 1, 3]  # matched by size, then unmatched
        assert [e.id for e in ht.plan(con, mapped_only=True)] == [2, 1]
        assert [e.id for e in ht.plan(con, order="id")] == [1, 2, 3]
        with pytest.raises(ValueError):
            ht.plan(con, order="nonsense")


def test_plan_can_order_by_centadata_deals(dbpath):
    with db.session(dbpath) as con:
        seed_sized(con)
        con.execute("INSERT INTO txn_raw (source, source_id, fetched_at, row_hash, estate_id) "
                    "VALUES ('centadata', 'a', now(), 'h', 'cd-2-S')")
        link.map_estates(con)
        assert [e.id for e in ht.plan(con, order="deals")][:2] == [1, 2]


def test_finished_estates_are_not_planned_again(dbpath):
    with db.session(dbpath) as con:
        seed_sized(con)
        db.mark_step(con, "backfill", "hse28:2:grid", 1)
        db.mark_step(con, "backfill", "hse28:2:list", 1)
        assert 2 in {e.id for e in ht.plan(con)}  # list done, per-flat pages not yet
        assert 2 not in {e.id for e in ht.plan(con, skip_flats=True)}
        db.mark_step(con, "backfill", "hse28:2:flats", 1)
        db.mark_step(con, "backfill", "hse28:3:gone", 1, "404")
        assert [e.id for e in ht.plan(con)] == [1]


def test_a_subset_plans_only_estates_matched_to_its_codes(dbpath):
    with db.session(dbpath) as con:
        assert ht.todo_count(con) is None  # nothing discovered yet: unknown, not "done"
        seed_sized(con)
        assert [e.id for e in ht.plan(con, estates={"2-S"})] == [1]
        assert ht.plan(con, estates=set()) == []
        assert ht.todo_count(con, {"2-S", "2-L"}) == 2
        assert ht.todo_count(con) == 3


# ---- a run on the fixture site -------------------------------------------------------------------


def seeded_run_db(dbpath):
    """One matched estate (28Hse id 1140 standing in for a territory estate) whose master
    list has the two blocks the fixture grid names."""
    with db.session(dbpath) as con:
        con.execute("INSERT INTO master_estate (code, name_zh, units, excluded, pilot) "
                    "VALUES ('2-T', '太古城', 5000, false, false)")
        for code, block in [("1-X", "建安閣"), ("1-Y", "高安閣")]:
            con.execute("INSERT INTO master_building (building_id, estate_code, block_code, "
                        "estate_name, phase_name, block_name) VALUES (?, '2-T', ?, '太古城', "
                        "'安盛台', ?)", [f"cd-{code}", code, block])
        ht.add_estates(con, [hse28.EstateRef("太古城", 1140)], "csv")


def test_run_loads_estate_links_buildings_and_resumes(dbpath):
    seeded_run_db(dbpath)
    kw = dict(db_path=dbpath, log=lambda *a: None, skip_ids=set())
    f = FakeFetcher(site())
    stats = ht.run_territory(fetcher=f, **kw)
    assert stats["estates"] == 1 and stats["errors"] == 0
    with db.session(dbpath) as con:
        assert con.execute("SELECT count(*) FROM txn_raw").fetchone()[0] == 6
        mapped = con.execute("SELECT count(*) FROM source_map WHERE kind = 'building' "
                             "AND source = 'hse28'").fetchone()[0]
        assert mapped >= 1
        assert con.execute("SELECT status FROM run_log WHERE command = 'backfill-territory'"
                           ).fetchone() == ("ok",)
    again = FakeFetcher(site())
    assert ht.run_territory(fetcher=again, **kw)["estates"] == 0
    assert again.requests == []  # everything was recorded as done


def test_run_stops_after_max_estates_and_gone_estates_are_marked(dbpath):
    with db.session(dbpath) as con:
        ht.add_estates(con, [hse28.EstateRef("甲苑", 11), hse28.EstateRef("乙苑", 12)], "csv")
    kw = dict(db_path=dbpath, log=lambda *a: None, skip_ids=set(), order="id")
    stats = ht.run_territory(fetcher=FakeFetcher({}), **kw)  # every page answers 404
    assert stats["gone"] == 2 and stats["estates"] == 0
    with db.session(dbpath) as con:
        assert ht.plan(con, skip_ids=set()) == []


def test_missing_csv_is_an_empty_list(tmp_path):
    assert ht.read_csv(Path(tmp_path) / "none.csv") == []


def test_csv_lists_estates(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("id,name\n12,甲苑\nx,壞\n", encoding="utf-8")
    assert [(e.id, e.name) for e in ht.read_csv(p)] == [(12, "甲苑")]
