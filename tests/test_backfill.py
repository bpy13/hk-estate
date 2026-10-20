"""Backfill: loads an estate from 28Hse pages, adds nothing on a rerun, resumes after an
interruption. Offline: a fake fetcher serves the synthetic fixture pages by URL."""

from pathlib import Path

import pytest

from hkestate import backfill, db
from hkestate.fetch import FetchError
from hkestate.sources import hse28

FIX = Path(__file__).parent / "fixtures" / "hse28"
EST = hse28.EstateRef("太古城", 1140)


def page(name):
    return (FIX / name).read_text(encoding="utf-8")


def site():
    unit_url = hse28.canonical_url(
        "https://www.28hse.com/estate/detail/太古城-1140/history/stateno-1/blockno-18/unitid-172141"
    )
    return {
        EST.history_url(): page("history_block.html"),
        EST.history_url(stateno=1): page("history_block.html"),
        EST.history_url(stateno=2): page("history_block.html"),
        EST.history_url(stateno=1, blockno=18): page("history_block.html"),
        EST.history_url(stateno=1, blockno=31): page("history_block.html"),
        EST.history_url(stateno=2, blockno=18): page("history_block.html"),
        EST.history_url(stateno=2, blockno=31): page("history_block.html"),
        EST.list_url(1): page("list_page.html"),
        EST.list_url(3): page("list_single_block_last.html"),
        unit_url: page("unit_page.html"),
    }


class FakeFetcher:
    def __init__(self, pages, fail_after=None, busy=()):
        self.pages = pages
        self.requests = []
        self.fail_after = fail_after
        self.busy = dict.fromkeys(busy, 1)  # url -> times to answer 429 first

    def get(self, url, page_type):
        if self.fail_after is not None and len(self.requests) >= self.fail_after:
            raise KeyboardInterrupt  # simulates the run being stopped
        self.requests.append(url)
        if self.busy.get(url):
            self.busy[url] -= 1
            raise FetchError(url, 429)
        if url not in self.pages:
            raise FetchError(url, 404)
        return self.pages[url]

    def get_bytes(self, url, page_type):
        page = self.get(url, page_type)
        return page if isinstance(page, bytes) else page.encode("utf-8")


UNIT_URL = hse28.canonical_url(
    "https://www.28hse.com/estate/detail/太古城-1140/history/stateno-1/blockno-18/unitid-172141"
)


@pytest.fixture
def dbpath(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
    return path


def counts(dbpath):
    with db.session(dbpath) as con:
        return (
            con.execute("SELECT count(*) FROM txn_raw").fetchone()[0],
            con.execute("SELECT count(*) FROM txn").fetchone()[0],
            con.execute("SELECT count(*) FROM unit").fetchone()[0],
        )


def run(dbpath, fetcher):
    return backfill.backfill_estates(
        {"test": [EST]}, fetcher=fetcher, db_path=dbpath, log=lambda *a: None
    )


def test_backfill_loads_lists_grid_and_flat_pages(dbpath):
    run(dbpath, FakeFetcher(site()))
    with db.session(dbpath) as con:
        ids = {r[0] for r in con.execute("SELECT source_id FROM txn_raw").fetchall()}
        assert ids == {
            "u180707:2026-10-14",
            "u172680:2026-10-13",
            "deal-52882206",
            "u48989:2021-03-12",
            "u172141:2023-11-09",  # from the per-flat page (2 deals, none in the lists)
            "u172141:2019-04-02",
        }
        row = con.execute(
            "SELECT building_id, floor_label, flat, price_hkd, deal_type FROM txn_raw "
            "WHERE source_id = 'u172141:2019-04-02'"
        ).fetchone()
        assert row == ("h28-1140-s1-b18", "30", "C", 12_500_000, "registry")
        # registry deal from a list page: building from its stateno/blockno link
        b = con.execute(
            "SELECT building_id FROM txn_raw WHERE source_id = 'u180707:2026-10-14'"
        ).fetchone()[0]
        assert b == "h28-1140-s6-b58"
        # estate, phases, buildings and units from the block grids
        assert con.execute("SELECT name_zh FROM estate").fetchall() == [("太古城",)]
        assert con.execute("SELECT count(*) FROM phase").fetchone()[0] == 2
        assert con.execute(
            "SELECT name_zh FROM building WHERE building_id = 'h28-1140-s1-b18'"
        ).fetchone() == ("建安閣",)
        unit = con.execute(
            "SELECT floor_num, floor_kind, saleable_sqft, gross_sqft FROM unit "
            "WHERE unit_id = 'h28-1140-s1-b18|30|C'"
        ).fetchone()
        assert unit == (30, "number", 703.0, 840.0)
        ground = con.execute(
            "SELECT floor_label, floor_num, floor_kind FROM unit "
            "WHERE unit_id = 'h28-1140-s1-b18|G|B'"
        ).fetchone()
        assert ground == ("G", 0, "ground")


def test_second_run_adds_nothing(dbpath):
    run(dbpath, FakeFetcher(site()))
    first = counts(dbpath)
    second_fetcher = FakeFetcher(site())
    run(dbpath, second_fetcher)
    assert counts(dbpath) == first
    # Every step was recorded as done, so the rerun made no requests at all.
    assert second_fetcher.requests == []


def test_refresh_reruns_steps_but_still_adds_no_duplicates(dbpath):
    run(dbpath, FakeFetcher(site()))
    first = counts(dbpath)
    backfill.backfill_estates(
        {"test": [EST]}, fetcher=FakeFetcher(site()), db_path=dbpath, log=lambda *a: None,
        refresh=True,
    )
    assert counts(dbpath) == first


def test_interrupted_run_resumes_from_progress(dbpath):
    full = FakeFetcher(site())
    reference = Path(str(dbpath) + ".ref.duckdb")
    with db.session(reference) as con:
        db.init_db(con)
    run(reference, full)

    with pytest.raises(KeyboardInterrupt):
        run(dbpath, FakeFetcher(site(), fail_after=8))
    with db.session(dbpath) as con:
        status = con.execute("SELECT status FROM run_log ORDER BY run_id").fetchall()
        assert status == [("interrupted",)]
        done_before = con.execute("SELECT count(*) FROM run_progress").fetchone()[0]
    assert done_before > 0

    resumed = FakeFetcher(site())
    run(dbpath, resumed)
    assert counts(dbpath) == counts(reference)
    assert len(resumed.requests) < len(full.requests)  # finished steps were skipped
    with db.session(dbpath) as con:
        status = con.execute("SELECT status FROM run_log ORDER BY run_id").fetchall()
        assert status == [("interrupted",), ("ok",)]


def test_market_deal_building_matched_by_block_name():
    buildings = {
        "h28-1-s3-b53": ("星輝台", "衛星閣"),
        "h28-1-s3-b48": ("星輝台", "海星閣"),
        "h28-2-s1-b1": ("1期", "1座"),
        "h28-2-s2-b9": ("2期", "1座"),
    }
    assert backfill.match_building("星輝台 衛星閣", buildings) == "h28-1-s3-b53"
    assert backfill.match_building("星輝台 海星閣 (48座)", buildings) == "h28-1-s3-b48"
    assert backfill.match_building("2期 1座", buildings) == "h28-2-s2-b9"
    assert backfill.match_building("1座", buildings) is None  # ambiguous
    # phase text may hold several words (estate + phase); any of them can disambiguate
    multi = {"x1": ("首都 第1座", "1座"), "x2": ("領都 第1座", "1座")}
    assert backfill.match_building("領都 1座", multi) == "x2"
    assert backfill.match_building("不知名閣", buildings) is None


def test_rate_limited_flat_page_is_retried_not_marked_done(dbpath):
    pauses = []
    backfill.backfill_estates(
        {"test": [EST]}, fetcher=FakeFetcher(site(), busy=[UNIT_URL]), db_path=dbpath,
        log=lambda *a: None, pause=pauses.append,
    )
    assert pauses  # it cooled down after the 429
    with db.session(dbpath) as con:
        ids = {r[0] for r in con.execute("SELECT source_id FROM txn_raw").fetchall()}
    assert "u172141:2019-04-02" in ids  # the retry after the pause got the page


def test_a_patient_backfill_waits_out_any_number_of_429s(dbpath):
    f = FakeFetcher(site(), busy=[UNIT_URL])
    f.busy[UNIT_URL] = 20  # far more than BUSY_LIMIT
    pauses = []
    backfill.backfill_estates({"test": [EST]}, fetcher=f, db_path=dbpath,
                              log=lambda *a: None, pause=pauses.append, patient=True)
    assert len(pauses) == 20
    with db.session(dbpath) as con:
        ids = {r[0] for r in con.execute("SELECT source_id FROM txn_raw").fetchall()}
    assert "u172141:2019-04-02" in ids


def test_a_patient_backfill_skips_the_fetchers_pause_when_a_cool_down_is_pending(dbpath):
    f = FakeFetcher(site(), busy=[UNIT_URL])
    f.busy[UNIT_URL] = 5
    f.cooldown_remaining = lambda url: 900.0  # the real fetcher waits before the next request
    pauses = []
    backfill.backfill_estates({"test": [EST]}, fetcher=f, db_path=dbpath,
                              log=lambda *a: None, pause=pauses.append, patient=True)
    assert pauses == []


def test_repeated_rate_limits_stop_the_run_for_a_later_resume(dbpath):
    f = FakeFetcher(site(), busy=[UNIT_URL])
    f.busy[UNIT_URL] = 99
    with pytest.raises(backfill.SourceBusy):
        backfill.backfill_estates(
            {"test": [EST]}, fetcher=f, db_path=dbpath, log=lambda *a: None,
            pause=lambda s: None,
        )
    with db.session(dbpath) as con:
        assert con.execute(
            "SELECT count(*) FROM run_progress WHERE step_key LIKE '%:flat:%'"
        ).fetchone()[0] == 0
        assert con.execute("SELECT status FROM run_log").fetchone()[0] == "failed"
    run(dbpath, FakeFetcher(site()))  # a later run picks the flat up
    with db.session(dbpath) as con:
        assert con.execute(
            "SELECT count(*) FROM txn_raw WHERE source_id = 'u172141:2019-04-02'"
        ).fetchone()[0] == 1


def test_flats_marked_with_a_rate_limit_error_by_older_runs_are_retried(dbpath):
    run(dbpath, FakeFetcher(site()))
    with db.session(dbpath) as con:
        con.execute("DELETE FROM txn_raw WHERE source_id LIKE 'u172141:%'")
        con.execute(
            "UPDATE run_progress SET detail = 'error 429' WHERE step_key LIKE '%:flat:172141'"
        )
        db.clear_steps(con, "backfill", "hse28:1140:flats")
    run(dbpath, FakeFetcher(site()))
    with db.session(dbpath) as con:
        assert con.execute(
            "SELECT count(*) FROM txn_raw WHERE source_id LIKE 'u172141:%'"
        ).fetchone()[0] == 2


def test_block_name_variants_fold_together():
    buildings = {"a": ("高山台", "盧山閣"), "b": ("高山台", "龍山閣")}
    assert backfill.match_building("高山臺 廬山閣 (9座)", buildings) == "a"


def test_address_range_block_maps_to_each_numbered_building():
    buildings = {
        "s1-b1": ("第一期", "百老匯街1號"),
        "s1-b2": ("第一期", "百老匯街2號"),
        "s1-b3": ("第一期", "百老匯街3號"),
        "s1-b4": ("第一期", "百老匯街4號"),
    }
    assert sorted(backfill.match_buildings("1期 百老匯街1-3號", buildings)) == [
        "s1-b1", "s1-b2", "s1-b3"
    ]
    assert backfill.match_buildings("1期 百老匯街4號", buildings) == ["s1-b4"]
    assert backfill.match_building("1期 百老匯街1-3號", buildings) is None  # several


def test_street_type_word_is_ignored_in_numbered_addresses():
    # Centadata writes 蘭秀道1-3號, 28Hse 蘭秀街1號 / 蘭秀街3號 (same blocks)
    buildings = {"b1": ("第五期", "蘭秀街1號"), "b3": ("第五期", "蘭秀街3號"),
                 "b9": ("第五期", "恆柏街1號")}
    assert sorted(backfill.match_buildings("5期 蘭秀道1-3號", buildings)) == ["b1", "b3"]


def test_grid_for_estate_with_no_flats_does_not_crash(dbpath, monkeypatch):
    """An estate whose grid lists no flats (28Hse id 34052) used to raise on executemany([])."""
    from types import SimpleNamespace

    empty = SimpleNamespace(phases=[], blocks=[], units=[], current_state=None)
    monkeypatch.setattr(hse28, "parse_history_page", lambda html: empty)
    loader = backfill._Loader(FakeFetcher({EST.history_url(): "x"}), dbpath, lambda *a: None, 1)
    loader.grid(EST, "test")
    with db.session(dbpath) as con:
        assert con.execute("SELECT count(*) FROM unit").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM estate").fetchone()[0] == 1
