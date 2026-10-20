"""The daily listing run end to end, offline: fixture pages through a fake fetcher."""

from datetime import date, datetime
from pathlib import Path

import pytest

from hkestate import db, listing_run, locks
from hkestate.fetch import FetchError

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 16)


class FakeFetcher:
    """Serves fixture pages by URL; anything else is a 404. Records requested URLs."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.asked: list[str] = []

    def get(self, url, page_type):
        self.asked.append(url)
        for key, text in self.pages.items():
            if url.startswith(key) or key in url:
                return text
        raise FetchError(url, 404)

    def close(self):
        pass


def read(*parts):
    return FIX.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
        con.execute("INSERT INTO estate (estate_id, name_zh, group_name, source, source_key) "
                    "VALUES ('h28-1140', '太古城', 'taikoo-shing', 'hse28', '1140')")
        for i, (phase, block) in enumerate(
            [("安盛台", "寧安閣"), ("星輝台", "海星閣"), ("海景花園(東)", "美菊閣"),
             ("金殿台", "漢宮閣")], 1
        ):
            con.execute("INSERT INTO phase VALUES (?, 'h28-1140', ?, ?)", [f"p{i}", phase, i])
            con.execute("INSERT INTO building (building_id, estate_id, phase_id, name_zh) "
                        "VALUES (?, 'h28-1140', ?, ?)", [f"b{i}", f"p{i}", block])
            con.execute("INSERT INTO unit (unit_id, building_id, floor_label, floor_num, flat) "
                        "VALUES (?, ?, '30', 30, 'A')", [f"b{i}|30|A", f"b{i}"])
    return path


def pages():
    return {
        "sitemap_TC_findproperty_detail.xml": read("centanet", "sitemap_detail.xml"),
        "_ZZA001?theme=buy": read("centanet", "detail_sale.html"),
        "/buy/apartment/a1/dg9/c1140/page-2": "<html></html>",
        "/rent/apartment/a1/dg9/c1140": "<html></html>",
        "/buy/apartment/a1/dg9/c1140": read("hse28", "search_page.html"),
        "property-9000001": read("hse28", "listing_detail.html"),
        "48-%E5%A4%AA%E5%8F%A4%E5%9F%8E/for-sale/page/2": "<html></html>",
        "n/48-%E5%A4%AA%E5%8F%A4%E5%9F%8E": read("spacious", "search_page.html"),
    }


def test_daily_run_on_fixtures(env):
    fake = FakeFetcher(pages())
    summary = listing_run.run_listings(
        ["taikoo-shing"], ["centanet", "hse28", "spacious"], db_path=env, log=lambda m: None,
        fetcher=fake, hse28_fetcher=fake, now=datetime(2026, 10, 16, 8), today=TODAY,
    )
    with db.session(env) as con:
        rows = con.execute(
            "SELECT source, listing_id, building_id, match_method, floor_band, status "
            "FROM listing ORDER BY 1, 2").fetchall()
        flags = con.execute(
            "SELECT listing_id, flag FROM risk_flag ORDER BY 1, 2").fetchall()
        snaps = con.execute("SELECT count(*) FROM listing_snapshot").fetchone()[0]
    assert rows == [
        ("centanet", "ZZA001", "b3", "exact", "high", "active"),
        ("hse28", "9000001", "b1", "exact", "high", "active"),
        ("hse28", "9000002", "b4", "exact", "low", "active"),
        ("spacious", "19000001", "b2", "exact", "unknown", "active"),
        ("spacious", "19000002", None, "none", "unknown", "active"),
    ]  # ZZA002 (rent) 404s and is skipped; 康怡花園 is filtered out of 太古城's Spacious page
    assert ("9000002", "tenanted") in flags
    assert ("9000001", "sea_view") in flags and ("9000001", "haunted") not in flags
    assert ("ZZA001", "renovated") in flags
    assert snaps == 5
    tks = summary["match"]["taikoo-shing"]
    assert (tks["listings"], tks["matched"]) == (5, 4)
    # 9000002's detail page 404s: counted as an error, the search card still counts
    assert summary["sources"]["hse28"]["taikoo-shing"]["errors"] == 1


def test_second_run_same_day_reads_no_detail_pages_again(env):
    kw = dict(db_path=env, log=lambda m: None, now=datetime(2026, 10, 16, 8), today=TODAY)
    first = FakeFetcher(pages())
    listing_run.run_listings(["taikoo-shing"], ["centanet", "hse28"], fetcher=first,
                             hse28_fetcher=first, **kw)
    second = FakeFetcher(pages())
    listing_run.run_listings(["taikoo-shing"], ["centanet", "hse28"], fetcher=second,
                             hse28_fetcher=second, **kw)
    assert not [u for u in second.asked if "ZZA001" in u or "property-9000001" in u]
    with db.session(env) as con:
        assert con.execute("SELECT count(*) FROM listing_snapshot").fetchone()[0] == 3


def test_hse28_skipped_while_a_backfill_holds_the_lock(env, tmp_path):
    import os
    path = locks.lock_path("hse28")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getppid()))
    fake = FakeFetcher(pages())
    summary = listing_run.run_listings(
        ["taikoo-shing"], ["hse28"], db_path=env, log=lambda m: None, fetcher=fake,
        hse28_fetcher=fake, now=datetime(2026, 10, 16, 8), today=TODAY,
    )
    assert fake.asked == []
    res = summary["sources"]["hse28"]["taikoo-shing"]
    assert res["complete"] is False and "skipped" in res["note"]


def _sitemap(ids, estate="太古城"):
    locs = "".join(
        f"<url><loc>https://hk.centanet.com/findproperty/detail/{estate}-x_{i}?theme=buy"
        "</loc></url>" for i in ids)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'


class CentanetPages(FakeFetcher):
    """The sitemap lists `ids`; every detail page is the fixture page (the id comes from the
    url). `stop_at`: the n-th detail request raises KeyboardInterrupt (a stopped run)."""

    def __init__(self, ids, stop_at=None, estate="太古城"):
        super().__init__({})
        self.xml, self.stop_at, self.n = _sitemap(ids, estate), stop_at, 0
        self.html = read("centanet", "detail_sale.html")
        self.lock_seen: list[bool] = []

    def get(self, url, page_type):
        self.asked.append(url)
        self.lock_seen.append(locks.lock_path("centanet").exists())
        if "sitemap" in url:
            return self.xml
        self.n += 1
        if self.n == self.stop_at:
            raise KeyboardInterrupt
        return self.html


IDS = [f"ZZC{i:03d}" for i in range(30)]


def test_centanet_holds_the_centanet_lock_only_while_it_reads(env):
    fake = CentanetPages(IDS[:2])
    listing_run.run_listings(["taikoo-shing"], ["centanet", "spacious"], db_path=env,
                             log=lambda m: None, fetcher=fake, now=datetime(2026, 10, 16, 8),
                             today=TODAY, parallel=False)
    centanet = [s for u, s in zip(fake.asked, fake.lock_seen, strict=True) if "centanet" in u]
    others = [s for u, s in zip(fake.asked, fake.lock_seen, strict=True) if "centanet" not in u]
    assert centanet and all(centanet)  # held for every Centanet request
    assert others and not any(others)  # released before Spacious
    assert not locks.lock_path("centanet").exists()


def test_centanet_skipped_while_another_process_holds_the_centanet_lock(env):
    import os
    path = locks.lock_path("centanet")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getppid()))
    fake = CentanetPages(IDS[:2])
    summary = listing_run.run_listings(
        ["taikoo-shing"], ["centanet"], db_path=env, log=lambda m: None, fetcher=fake,
        now=datetime(2026, 10, 16, 8), today=TODAY)
    assert fake.asked == []
    res = summary["sources"]["centanet"]["taikoo-shing"]
    assert res["complete"] is False and "skipped" in res["note"]


def test_a_stale_centanet_lock_is_taken_over(env, monkeypatch):
    path = locks.lock_path("centanet")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("999999")
    monkeypatch.setattr(locks, "pid_alive", lambda pid: False)  # its process has ended
    fake = CentanetPages(IDS[:2])
    listing_run.run_listings(["taikoo-shing"], ["centanet"], db_path=env, log=lambda m: None,
                             fetcher=fake, now=datetime(2026, 10, 16, 8), today=TODAY)
    assert len(fake.asked) == 3 and all(fake.lock_seen)
    assert not path.exists()


def test_a_stopped_run_keeps_the_reads_of_its_stored_batches(env):
    """Known listings: last_detail_at is written with each batch of 25, not only at the end."""
    old = datetime(2026, 10, 1)
    with db.session(env) as con:
        for lid in IDS:
            con.execute("INSERT INTO listing (source, listing_id, group_name, status, "
                        "last_detail_at) VALUES ('centanet', ?, 'taikoo-shing', 'active', ?)",
                        [lid, old])
    now = datetime(2026, 10, 16, 8)
    fake = CentanetPages(IDS, stop_at=28)  # all 30 are due (read 15 days ago); stop at 28
    with pytest.raises(KeyboardInterrupt):
        listing_run.run_listings(["taikoo-shing"], ["centanet"], db_path=env,
                                 log=lambda m: None, fetcher=fake, now=now, today=TODAY)
    with db.session(env) as con:
        read = dict(con.execute("SELECT listing_id, last_detail_at FROM listing").fetchall())
    assert sum(t == now for t in read.values()) == 25
    assert sum(t == old for t in read.values()) == 5
    assert not locks.lock_path("centanet").exists()  # released on the way out


def test_new_listings_read_by_a_stopped_run_are_not_read_again(env):
    """New listings have no `listing` row until the run finishes: their stored detail pages
    count as read."""
    first = CentanetPages(IDS, stop_at=28)
    with pytest.raises(KeyboardInterrupt):
        listing_run.run_listings(["taikoo-shing"], ["centanet"], db_path=env,
                                 log=lambda m: None, fetcher=first, today=TODAY)
    second = CentanetPages(IDS)
    logs: list[str] = []
    listing_run.run_listings(["taikoo-shing"], ["centanet"], db_path=env, log=logs.append,
                             fetcher=second, today=TODAY)
    again = [u for u in second.asked if "sitemap" not in u]
    assert len(again) == 5 and all(any(i in u for i in IDS[25:]) for u in again)
    assert any("30 live, 5 new; reading 5 new + 0 re-read" in m for m in logs)
    with db.session(env) as con:
        assert con.execute("SELECT count(*) FROM listing").fetchone()[0] == 30
    known = listing_run.ListingRun(db_path=env)._known("centanet")
    assert len(known) == 30 and all(t is not None for t, _ in known.values())


def test_territory_log_counts_new_and_re_reads_within_the_budget(env):
    logs: list[str] = []
    fake = CentanetPages(IDS[:5], estate="海景苑")
    run = listing_run.ListingRun(db_path=env, log=logs.append, fetcher=fake, centanet_budget=3,
                                 now=datetime(2026, 10, 16, 8), today=TODAY)
    run.centanet([listing_run.TERRITORY])
    line = next(m for m in logs if m.startswith("centanet territory:"))
    assert "5 live, 5 new; reading 3 new + 0 re-read (2 left for later runs)" in line
    assert len([u for u in fake.asked if "sitemap" not in u]) == 3


def _hse_listing(i):
    from types import SimpleNamespace as NS
    return NS(listing_id=str(i), source_estate_key=None, price_hkd=1, url=f"detail-{i}",
              kind="sale", estate_name=None, text_for_flags="")


class FailingDetails(FakeFetcher):
    """Search pages work; detail pages fail with `status` (the first `fail_n`, or all)."""

    def __init__(self, pages, status=400, fail_n=None):
        super().__init__(pages)
        self.status, self.fail_n, self.n = status, fail_n, 0

    def get(self, url, page_type):
        self.asked.append(url)
        if "detail" in url:
            self.n += 1
            if self.fail_n is None or self.n <= self.fail_n:
                raise FetchError(url, self.status)
            return read("centanet", "detail_sale.html")
        return "<html></html>"


def _failing_centanet(**kw):
    locs = "".join(
        f"<url><loc>https://hk.centanet.com/findproperty/detail/太古城-x_ZZB{i:03d}?theme=buy"
        "</loc></url>" for i in range(12))
    fake = FailingDetails({}, **kw)
    real_get = fake.get

    def get(url, page_type):
        if "sitemap" in url:
            return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'
        return real_get(url, page_type)

    fake.get = get
    return fake


def test_centanet_stops_after_five_consecutive_non_throttle_errors(env):
    fake = _failing_centanet(status=400)
    summary = listing_run.run_listings(
        ["taikoo-shing"], ["centanet"], db_path=env, log=lambda m: None, fetcher=fake,
        hse28_fetcher=fake, now=datetime(2026, 10, 16, 8), today=TODAY)
    assert len([u for u in fake.asked if "ZZB" in u]) == 5
    res = summary["sources"]["centanet"]["taikoo-shing"]
    assert res["complete"] is False and "5 consecutive" in res["note"]


@pytest.mark.parametrize(("status", "fail_n", "slept", "seen"), [
    (429, 2, [600, 1200], 12),  # the same page again after each cool-down
    (500, 5, [600], 11),  # page 1 failed 3 times (skipped); 5 in a row: a cool-down
])
def test_centanet_cools_down_and_resumes_when_throttled(env, status, fail_n, slept, seen):
    """2026-10-20: five 500s in a row stopped Centanet for the day. Now it cools down
    (10 min, 20 min ...) and carries on."""
    fake = _failing_centanet(status=status, fail_n=fail_n)
    sleeps, logs = [], []
    summary = listing_run.run_listings(
        ["taikoo-shing"], ["centanet"], db_path=env, log=logs.append, fetcher=fake,
        now=datetime(2026, 10, 16, 8), today=TODAY, sleep=sleeps.append)
    res = summary["sources"]["centanet"]["taikoo-shing"]
    assert res["note"] == "" and res["new"] == seen
    assert sleeps == slept
    assert any("cooling down 10 min" in m for m in logs)


@pytest.mark.parametrize("status", [429, 503, 500])
def test_centanet_gives_up_for_the_day_after_three_cool_downs(env, status):
    fake = _failing_centanet(status=status)
    sleeps, logs = [], []
    summary = listing_run.run_listings(
        ["taikoo-shing"], ["centanet"], db_path=env, log=logs.append, fetcher=fake,
        now=datetime(2026, 10, 16, 8), today=TODAY, sleep=sleeps.append)
    res = summary["sources"]["centanet"]["taikoo-shing"]
    assert res["complete"] is False and "gave up after 3 cool-downs" in res["note"]
    assert sleeps == [600, 1200, 1800]  # 10, 20, 30 min, then it stopped


def test_hse28_stops_after_five_consecutive_fetch_errors(env, monkeypatch):
    from types import SimpleNamespace as NS
    monkeypatch.setattr(listing_run.hse28_listings, "parse_search_page",
                        lambda text, kind: NS(listings=[_hse_listing(i) for i in range(12)],
                                              next_url=None))
    monkeypatch.setattr(listing_run.ListingRun, "_store", lambda *a, **k: None)
    fake = FailingDetails({})
    summary = listing_run.run_listings(
        ["taikoo-shing"], ["hse28"], db_path=env, log=lambda m: None, fetcher=fake,
        hse28_fetcher=fake, now=datetime(2026, 10, 16, 8), today=TODAY)
    assert len([u for u in fake.asked if u.startswith("detail-")]) == 5
    res = summary["sources"]["hse28"]["taikoo-shing"]
    assert res["complete"] is False and "5 consecutive" in res["note"]


def test_hse28_gives_up_for_the_day_after_three_cool_downs(env, monkeypatch):
    from types import SimpleNamespace as NS
    monkeypatch.setattr(listing_run.hse28_listings, "parse_search_page",
                        lambda text, kind: NS(listings=[_hse_listing(i) for i in range(12)],
                                              next_url=None))
    monkeypatch.setattr(listing_run.ListingRun, "_store", lambda *a, **k: None)
    fake = FailingDetails({}, status=429)
    sleeps = []
    summary = listing_run.run_listings(
        ["taikoo-shing"], ["hse28"], db_path=env, log=lambda m: None, fetcher=fake,
        hse28_fetcher=fake, now=datetime(2026, 10, 16, 8), today=TODAY, sleep=sleeps.append)
    assert sleeps == [600, 1200, 1800]
    res = summary["sources"]["hse28"]["taikoo-shing"]
    assert res["complete"] is False and "gave up after 3 cool-downs" in res["note"]


def test_territory_reads_every_due_page_by_default(env):
    fake = CentanetPages(IDS[:5], estate="海景苑")
    run = listing_run.ListingRun(db_path=env, log=lambda m: None, fetcher=fake,
                                 now=datetime(2026, 10, 16, 8), today=TODAY)
    assert run.centanet_budget is None
    run.centanet([listing_run.TERRITORY])
    assert len([u for u in fake.asked if "sitemap" not in u]) == 5


def test_sources_run_at_the_same_time_and_finish_waits_for_all(env, monkeypatch):
    """Centanet can't finish until Spacious has made a request: only possible in parallel."""
    import threading

    spacious_asked = threading.Event()
    order = []

    class Gate(CentanetPages):
        def get(self, url, page_type):
            if "centanet" in url and "sitemap" not in url:
                assert spacious_asked.wait(10), "Spacious did not run alongside Centanet"
            elif "centanet" not in url:
                spacious_asked.set()
                order.append("spacious")
                return "<html></html>"
            order.append("centanet")
            return super().get(url, page_type)

    real_finish = listing_run.ListingRun.finish

    def finish(self):
        order.append("finish")
        return real_finish(self)

    monkeypatch.setattr(listing_run.ListingRun, "finish", finish)
    fake = Gate(IDS[:2])
    summary = listing_run.run_listings(
        ["taikoo-shing"], ["centanet", "spacious"], db_path=env, log=lambda m: None,
        fetcher=fake, now=datetime(2026, 10, 16, 8), today=TODAY)
    assert order[-1] == "finish" and order.count("finish") == 1
    assert summary["sources"]["centanet"]["taikoo-shing"]["new"] == 2


def test_an_error_in_one_source_thread_is_raised_after_the_others_finish(env):
    fake = CentanetPages(IDS[:3], stop_at=2)  # Centanet's 2nd detail page: Ctrl+C
    with pytest.raises(KeyboardInterrupt):
        listing_run.run_listings(["taikoo-shing"], ["centanet", "spacious"], db_path=env,
                                 log=lambda m: None, fetcher=fake, today=TODAY)
    assert any("spacious" in u for u in fake.asked)  # Spacious still ran to its end
    assert not locks.lock_path("centanet").exists()
