"""Weekly transaction refresh: newest deal-list pages only, stopping at known deals."""

from test_backfill import EST, FakeFetcher, page, site

from hkestate import backfill, db


def load(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
    backfill.backfill_estates({"test": [EST]}, fetcher=FakeFetcher(site()), db_path=path,
                              log=lambda *a: None)
    return path


class Recording(FakeFetcher):
    def __init__(self, pages):
        super().__init__(pages)
        self.types = []

    def get(self, url, page_type):
        self.types.append(page_type)
        return super().get(url, page_type)


def test_update_stops_at_the_first_page_without_new_deals(tmp_path):
    path = load(tmp_path)
    f = Recording(site())
    added = backfill.update_estates({"test": [EST]}, fetcher=f, db_path=path,
                                    log=lambda *a: None)
    assert added == 0
    assert f.requests == [EST.list_url(1)]
    assert f.types == ["listing"]  # a fresh copy, not last week's cached page


def test_update_reads_on_while_pages_bring_new_deals(tmp_path):
    path = load(tmp_path)
    pages = site()
    # a changed price on page 1 = a new version of that deal
    pages[EST.list_url(1)] = page("list_page.html").replace("$875 萬元", "$885 萬元", 1)
    f = Recording(pages)
    added = backfill.update_estates({"test": [EST]}, fetcher=f, db_path=path,
                                    log=lambda *a: None)
    assert added >= 1
    assert len(f.requests) >= 2  # went on to the next page


def test_update_skips_an_estate_that_was_never_loaded(tmp_path):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
    f = Recording(site())
    assert backfill.update_estates({"test": [EST]}, fetcher=f, db_path=path,
                                   log=lambda *a: None) == 0
    assert f.requests == []
