"""Daily listing snapshots, delisting after 3 missed run days, and relistings."""

from datetime import date, timedelta

import pytest

from hkestate import db, listings

D1 = date(2026, 10, 1)


def day(n: int) -> date:
    return D1 + timedelta(days=n - 1)


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    yield c
    c.close()


def item(lid="L1", price=9_000_000, **kw):
    return {"listing_id": lid, "kind": "sale", "url": f"https://x/{lid}", "price_hkd": price,
            "saleable_sqft": 580.0, "flat": "C", "building_text": "寧安閣", **kw}


def snaps(con, lid="L1"):
    return con.execute(
        "SELECT snapshot_date, price_hkd, status FROM listing_snapshot "
        "WHERE listing_id = ? ORDER BY snapshot_date", [lid]
    ).fetchall()


def run(con, n, items):
    listings.record_seen(con, "centanet", "tks", items, day(n))
    listings.mark_delisted(con, "centanet", "tks", day(n))


def status(con, lid="L1"):
    return con.execute(
        "SELECT status, first_seen, last_seen, delisted_on FROM listing WHERE listing_id = ?",
        [lid],
    ).fetchone()


def test_one_snapshot_per_listing_per_hk_day(con):
    run(con, 1, [item(price=9_000_000)])
    run(con, 1, [item(price=8_800_000)])  # a second run the same day: the later price wins
    assert snaps(con) == [(day(1), 8_800_000, "new")]
    run(con, 2, [item(price=8_800_000)])
    run(con, 3, [item(price=8_500_000)])
    assert snaps(con) == [
        (day(1), 8_800_000, "new"),
        (day(2), 8_800_000, "seen"),
        (day(3), 8_500_000, "price_change"),
    ]


def test_delisted_after_three_missed_run_days(con):
    run(con, 1, [item()])
    run(con, 2, [])
    run(con, 3, [])
    assert status(con)[0] == "active"
    run(con, 4, [])
    assert status(con) == ("delisted", day(1), day(1), day(4))
    assert snaps(con)[-1] == (day(4), 9_000_000, "delisted")


def test_days_without_a_run_do_not_count(con):
    run(con, 1, [item()])
    run(con, 10, [])  # laptop off for a week: only one missed run
    assert status(con)[0] == "active"


def test_an_incomplete_run_does_not_count_as_a_missed_day(con):
    run(con, 1, [item()])
    run(con, 2, [])
    run(con, 3, [])
    listings.record_seen(con, "centanet", "tks", [], day(4), complete=False)  # blocked midway
    listings.mark_delisted(con, "centanet", "tks", day(4))
    assert status(con)[0] == "active"


def test_relisting_at_a_new_price_is_a_new_snapshot_of_the_same_listing(con):
    run(con, 1, [item(price=9_000_000)])
    for n in (2, 3, 4):
        run(con, n, [])
    assert status(con)[0] == "delisted"
    run(con, 6, [item(price=8_600_000)])
    assert status(con) == ("active", day(1), day(6), None)
    assert snaps(con)[-1] == (day(6), 8_600_000, "relisted")
    assert con.execute("SELECT count(DISTINCT listing_id) FROM listing").fetchone()[0] == 1


def test_relisting_under_a_new_id_links_to_the_old_listing(con):
    run(con, 1, [item("OLD", price=9_000_000)])
    for n in (2, 3, 4):
        run(con, n, [])
    run(con, 6, [item("NEW", price=8_600_000)])
    for lid in ("OLD", "NEW"):
        con.execute("UPDATE listing SET building_id = 'b1', floor_band = 'high' "
                    "WHERE listing_id = ?", [lid])
    assert listings.relink_relistings(con, "centanet", day(6)) == 1
    assert con.execute(
        "SELECT relist_of FROM listing WHERE listing_id = 'NEW'").fetchone()[0] == "OLD"
    assert snaps(con, "NEW")[-1] == (day(6), 8_600_000, "relisted")
