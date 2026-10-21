"""Schema and DB layer: init_db creates the tables, is idempotent, and the clean views drop the
rows decisions.md excludes from analysis."""

from datetime import date, datetime

import pytest

from hkestate import db

TABLES = {
    "estate",
    "phase",
    "building",
    "unit",
    "txn_raw",
    "listing_raw",
    "listing_snapshot",
    "rent_index",
    "price_index",
    "market_stat",
    "attribute",
    "risk_flag",
    "source_map",
    "run_log",
    "run_progress",
}


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    yield c
    c.close()


def table_names(con):
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'"
    ).fetchall()
    return {r[0] for r in rows}


def view_names(con):
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW'"
    ).fetchall()
    return {r[0] for r in rows}


def test_init_db_creates_tables_and_views(con):
    assert TABLES <= table_names(con)
    assert {"txn_latest", "txn_all", "txn"} <= view_names(con)


def test_init_db_is_idempotent(tmp_path):
    c = db.connect(tmp_path / "t.duckdb")
    db.init_db(c)
    c.execute(
        "INSERT INTO estate (estate_id, name_zh, source, source_key) "
        "VALUES ('e1', '太古城', 'x', '1')"
    )
    db.init_db(c)  # second run must neither fail nor wipe data
    db.init_db(c)
    assert c.execute("SELECT count(*) FROM estate").fetchone()[0] == 1
    assert TABLES <= table_names(c)


@pytest.mark.parametrize(
    "message",
    [
        "IO Error: Could not set lock on file",
        "IO Error: Cannot open file: The process cannot access the file because it is being "
        "used by another process. File is already open in python.exe",
    ],
)
def test_connect_waits_while_another_process_holds_the_file(tmp_path, monkeypatch, message):
    import duckdb

    real = duckdb.connect
    calls = []

    def flaky(path, *a, **k):
        calls.append(path)
        if len(calls) < 3:
            raise duckdb.IOException(message)
        return real(path, *a, **k)

    monkeypatch.setattr(db.duckdb, "connect", flaky)
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    c = db.connect(tmp_path / "x.duckdb")
    c.close()
    assert len(calls) == 3


def test_connect_creates_parent_folder(tmp_path):
    path = tmp_path / "nested" / "dir" / "x.duckdb"
    c = db.connect(path)
    c.close()
    assert path.exists()


# ---- clean views ------------------------------------------------------------------------


def _setup_building(con, building_id="b1", ratio=None):
    con.execute(
        "INSERT INTO estate (estate_id, name_zh, source, source_key) VALUES ('e1','E','t','1')"
    )
    con.execute(
        "INSERT INTO building (building_id, estate_id, name_zh, saleable_to_gross) "
        "VALUES (?, 'e1', 'B', ?)",
        [building_id, ratio],
    )


def _txn(con, source_id, price, day, *, unit="u", building="b1", saleable=500.0, gross=None,
         deal_type="registry", first_hand=False, non_market=False, source="hse28",
         fetched_at=datetime(2026, 10, 16, 12)):
    db.insert_txn_raw(
        con,
        [
            {
                "source": source,
                "source_id": source_id,
                "fetched_at": fetched_at,
                "url": "https://example.com/x",
                "building_id": building,
                "floor_label": "10",
                "flat": unit,
                "deal_date": day,
                "deal_type": deal_type,
                "price_hkd": price,
                "saleable_sqft": saleable,
                "gross_sqft": gross,
                "first_hand": first_hand,
                "non_market": non_market,
            }
        ],
    )


def _clean_ids(con):
    return {r[0] for r in con.execute("SELECT source_id FROM txn").fetchall()}


def test_clean_view_drops_deals_under_500k(con):
    _setup_building(con)
    _txn(con, "cheap", 400_000, date(2024, 1, 5), unit="A")
    _txn(con, "ok", 8_000_000, date(2024, 1, 6), unit="B")
    assert _clean_ids(con) == {"ok"}
    flags = dict(con.execute("SELECT source_id, excl_low_price FROM txn_all").fetchall())
    assert flags == {"cheap": True, "ok": False}


def test_clean_view_drops_first_hand_and_non_market(con):
    _setup_building(con)
    _txn(con, "first", 8_000_000, date(2024, 1, 5), unit="A", first_hand=True)
    _txn(con, "gift", 8_000_000, date(2024, 2, 5), unit="B", non_market=True)
    _txn(con, "ok", 8_000_000, date(2024, 3, 6), unit="C")
    assert _clean_ids(con) == {"ok"}


def test_clean_view_drops_5_mad_outliers_within_building_year(con):
    _setup_building(con)
    # 10 normal deals around HK$16,000/sq ft, one at HK$40,000/sq ft in the same year.
    for i, psf in enumerate([15500, 15800, 16000, 16100, 16200, 15900, 16050, 15700, 16300, 16150]):
        _txn(con, f"n{i}", psf * 500, date(2024, 1 + i, 3), unit=f"U{i}")
    _txn(con, "outlier", 40_000 * 500, date(2024, 12, 1), unit="X")
    assert "outlier" not in _clean_ids(con)
    assert len(_clean_ids(con)) == 10


def test_uniform_building_does_not_flag_a_normal_premium(con):
    # Near-identical flats: the raw MAD is tiny. A high-floor deal 20% above the median is
    # ordinary and must not be flagged; a deal at 2.3x the median still is.
    _setup_building(con)
    for i, psf in enumerate([20000, 20100, 19900, 20050, 19950, 20000, 20020, 19980, 20010]):
        _txn(con, f"n{i}", psf * 800, date(2021, 1 + i, 3), unit=f"U{i}", saleable=800.0)
    _txn(con, "premium", 24_000 * 800, date(2021, 11, 1), unit="P", saleable=800.0)
    _txn(con, "odd", 46_000 * 800, date(2021, 12, 1), unit="Q", saleable=800.0)
    ids = _clean_ids(con)
    assert "premium" in ids
    assert "odd" not in ids


def test_outlier_rule_needs_enough_deals(con):
    # With only 2 deals in the building-year the median absolute deviation is meaningless:
    # nothing is flagged.
    _setup_building(con)
    _txn(con, "a", 5_000_000, date(2024, 1, 3), unit="A")
    _txn(con, "b", 20_000_000, date(2024, 2, 3), unit="B")
    assert _clean_ids(con) == {"a", "b"}


def test_clean_view_drops_same_day_bulk_deals(con):
    _setup_building(con)
    for flat in "ABC":  # three flats in one building on the same day
        _txn(con, f"bulk{flat}", 8_000_000, date(2024, 5, 5), unit=flat)
    _txn(con, "single", 8_000_000, date(2024, 5, 6), unit="D")
    assert _clean_ids(con) == {"single"}


def test_txn_latest_keeps_newest_version_of_a_source_row(con):
    _setup_building(con)
    _txn(con, "d1", 8_000_000, date(2024, 1, 5), deal_type="market",
         fetched_at=datetime(2026, 1, 1))
    _txn(con, "d1", 8_100_000, date(2024, 1, 5), deal_type="registry",
         fetched_at=datetime(2026, 2, 1))
    rows = con.execute("SELECT price_hkd, deal_type FROM txn_latest").fetchall()
    assert rows == [(8_100_000, "registry")]
    assert con.execute("SELECT count(*) FROM txn_raw").fetchone()[0] == 2  # append-only


def test_insert_txn_raw_skips_identical_rows(con):
    _setup_building(con)
    _txn(con, "d1", 8_000_000, date(2024, 1, 5), fetched_at=datetime(2026, 1, 1))
    _txn(con, "d1", 8_000_000, date(2024, 1, 5), fetched_at=datetime(2026, 1, 8))
    assert con.execute("SELECT count(*) FROM txn_raw").fetchone()[0] == 1


def test_market_report_superseded_by_registry_deal_on_same_unit(con):
    # An agency-reported deal and the later Land Registry record of the same sale (same flat,
    # same price, a few weeks apart) count once.
    _setup_building(con)
    _txn(con, "m", 8_000_000, date(2024, 1, 5), unit="A", deal_type="market")
    _txn(con, "r", 8_000_000, date(2024, 1, 20), unit="A", deal_type="registry")
    assert _clean_ids(con) == {"r"}


def test_market_report_with_floor_band_matches_registry_deal_by_building_and_flat(con):
    _setup_building(con)
    _txn(con, "m", 8_000_000, date(2024, 1, 5), unit="A", deal_type="market")
    con.execute("UPDATE txn_raw SET floor_label = '高層' WHERE source_id = 'm'")
    _txn(con, "r", 8_050_000, date(2024, 1, 25), unit="A", deal_type="registry")
    _txn(con, "other-flat", 8_000_000, date(2024, 1, 26), unit="B", deal_type="registry")
    assert _clean_ids(con) == {"r", "other-flat"}


def test_same_sale_from_list_and_flat_page_is_one_version(con):
    _setup_building(con)
    _txn(con, "d1", 8_000_000, date(2024, 1, 5), gross=None)
    # The per-flat page adds gross area and a different URL: not a new version.
    db.insert_txn_raw(
        con,
        [
            {
                "source": "hse28",
                "source_id": "d1",
                "fetched_at": datetime(2026, 10, 17),
                "url": "https://example.com/flat",
                "building_id": "b1",
                "floor_label": "10",
                "flat": "u",
                "deal_date": date(2024, 1, 5),
                "deal_type": "registry",
                "price_hkd": 8_000_000,
                "saleable_sqft": 500.0,
                "gross_sqft": 640.0,
                "rooms": 3,
            }
        ],
    )
    assert con.execute("SELECT count(*) FROM txn_raw").fetchone()[0] == 1


def test_second_source_duplicate_is_dropped_but_unique_deal_kept(con):
    _setup_building(con)
    _txn(con, "h", 8_000_000, date(2024, 1, 5), unit="A")
    _txn(con, "c-dup", 8_000_000, date(2024, 1, 5), unit="A", source="centadata")
    _txn(con, "c-old", 3_000_000, date(2009, 6, 1), unit="A", source="centadata")
    assert _clean_ids(con) == {"h", "c-old"}


def test_area_basis_saleable_converted_and_gross_only(con):
    _setup_building(con, "b1", ratio=0.8)
    _setup_building_extra(con, "b2", ratio=None)
    _txn(con, "s", 8_000_000, date(2024, 1, 5), unit="A", saleable=500.0, gross=640.0)
    _txn(con, "c", 8_000_000, date(2024, 2, 5), unit="B", saleable=None, gross=600.0)
    _txn(con, "g", 8_000_000, date(2024, 3, 5), unit="C", building="b2", saleable=None,
         gross=600.0)
    rows = {
        r[0]: (r[1], r[2])
        for r in con.execute("SELECT source_id, area_sqft, area_basis FROM txn_all").fetchall()
    }
    assert rows["s"] == (500.0, "saleable")
    assert rows["c"] == (pytest.approx(480.0), "converted")
    assert rows["g"] == (600.0, "gross_only")


def _setup_building_extra(con, building_id, ratio):
    con.execute(
        "INSERT INTO building (building_id, estate_id, name_zh, saleable_to_gross) "
        "VALUES (?, 'e1', 'B2', ?)",
        [building_id, ratio],
    )


def test_hse28_registry_dates_are_registration_dates(con):
    # The Centadata cross-check showed 28Hse dates Land Registry deals by registration date.
    _setup_building(con)
    _txn(con, "r", 8_000_000, date(2024, 1, 5), unit="A", deal_type="registry")
    _txn(con, "m", 8_000_000, date(2024, 3, 5), unit="B", deal_type="market")
    rows = dict(
        con.execute("SELECT source_id, registration_date FROM txn_all").fetchall()
    )
    assert rows == {"r": date(2024, 1, 5), "m": None}


def test_unit_id_is_building_floor_flat():
    assert db.unit_id("b1", "10", "A") == "b1|10|A"


# ---- Phase 5: shared clean view (price check, parking, quarantined rows) -------------------


def _txn_raw_row(source_id, price, day, *, unit, building="b1", saleable=500.0,
                 source="hse28", raw=None, estate=None, floor="10", deal_type="registry",
                 fetched_at=datetime(2026, 10, 16, 12), first_hand=False):
    import json

    return {
        "source": source, "source_id": source_id, "fetched_at": fetched_at,
        "url": "https://example.com/x", "building_id": building, "estate_id": estate,
        "floor_label": floor, "flat": unit, "deal_date": day, "deal_type": deal_type,
        "price_hkd": price, "saleable_sqft": saleable, "first_hand": first_hand,
        "raw_json": json.dumps(raw or {}),
    }


def test_price_check_flags_clustered_part_share_deals(con):
    # One deal a year at ~HK$10,000/sq ft, then five 2026 "sales" at HK$3,000/sq ft (part-share
    # transfers). They are too many for the yearly 5-MAD rule (they are that year's median),
    # but under half the building's median price per sq ft: excl_price_check.
    _setup_building(con)
    for i, yr in enumerate(range(2019, 2025)):
        _txn(con, f"n{i}", 10_000 * 500 + i * 1000, date(yr, 3, 1), unit=f"U{i}")
    for i in range(5):
        _txn(con, f"part{i}", 3_000 * 500, date(2026, 1 + i, 9), unit=f"P{i}")
    flags = dict(con.execute("SELECT source_id, excl_price_check FROM txn_all").fetchall())
    assert all(flags[f"part{i}"] for i in range(5))
    assert not any(flags[f"n{i}"] for i in range(6))
    assert {f"part{i}" for i in range(5)}.isdisjoint(_clean_ids(con))
    assert all(f"n{i}" in _clean_ids(con) for i in range(6))


def test_price_check_adjusts_for_the_market_index(con):
    # A 1996 deal at HK$3,000/sq ft in a building whose recent deals are HK$10,000: the RVD
    # index rose from 30 to 100 in between, so index-adjusted it is a normal price.
    _setup_building(con)
    con.executemany(
        "INSERT INTO price_index (series, freq, period, class, region, value) "
        "VALUES ('rvd_price_index', 'month', ?, ?, 'ALL', ?)",
        [(date(1996, 6, 1), c, 30.0) for c in ("A", "B", "ALL")]
        + [(date(2024, 1, 1), c, 100.0) for c in ("A", "B", "ALL")])
    for i in range(6):
        _txn(con, f"n{i}", 10_000 * 500, date(2024, 1 + i, 3), unit=f"U{i}")
    _txn(con, "old", 3_000 * 500, date(1996, 6, 15), unit="OLD")
    flags = dict(con.execute("SELECT source_id, excl_price_check FROM txn_all").fetchall())
    assert flags["old"] is False


def test_price_check_needs_five_deals(con):
    _setup_building(con)
    _txn(con, "a", 10_000 * 500, date(2024, 1, 3), unit="A")
    _txn(con, "b", 3_000 * 500, date(2025, 2, 3), unit="B")
    assert _clean_ids(con) == {"a", "b"}


def test_parking_deals_are_flagged_and_left_out(con):
    _setup_building(con)
    db.insert_txn_raw(con, [
        _txn_raw_row("park", 9_000_000, date(2024, 1, 3), unit="A", raw={"with_parking": True}),
        _txn_raw_row("srpe_park", 9_000_000, date(2024, 2, 3), unit="B", source="srpe",
                     raw={"has_parking": True}),
        _txn_raw_row("flat", 8_000_000, date(2024, 3, 3), unit="C", raw={"with_parking": False}),
    ])
    flags = dict(con.execute("SELECT source_id, excl_parking FROM txn_all").fetchall())
    assert flags == {"park": True, "srpe_park": True, "flat": False}
    assert _clean_ids(con) == {"flat"}


def test_quarantined_lohas_park_rows_are_flagged_and_ignored_by_other_rules(con):
    # Centadata rows stored against the wrong LOHAS Park building by the first cross-check run
    # (before 19:44 on 2026-10-16): flagged, out of `txn`, and not counted towards a bulk deal.
    con.execute("INSERT INTO estate (estate_id, name_zh, group_name, source, source_key) "
                "VALUES ('lp', 'LP6', 'lohas-park', 'hse28', '9')")
    con.execute("INSERT INTO building (building_id, estate_id, name_zh) VALUES ('lpb', 'lp', 'x')")
    bad = datetime(2026, 10, 16, 19, 30)
    db.insert_txn_raw(con, [
        _txn_raw_row("q1", 8_000_000, date(2024, 5, 5), unit="A", building="lpb",
                     source="centadata", estate="lp", fetched_at=bad),
        _txn_raw_row("h1", 8_000_000, date(2024, 5, 5), unit="B", building="lpb", estate="lp"),
        _txn_raw_row("h2", 8_100_000, date(2024, 5, 5), unit="C", building="lpb", estate="lp"),
        _txn_raw_row("late", 8_000_000, date(2024, 6, 5), unit="D", building="lpb",
                     source="centadata", estate="lp",
                     fetched_at=datetime(2026, 10, 16, 19, 50)),
    ])
    flags = {r[0]: r[1:] for r in con.execute(
        "SELECT source_id, excl_quarantine, excl_bulk FROM txn_all").fetchall()}
    assert flags["q1"][0] is True
    assert flags["late"][0] is False
    assert flags["h1"] == (False, False)  # two real deals that day: not a bulk deal
    assert _clean_ids(con) == {"h1", "h2", "late"}


def test_duplicate_flag_lands_on_the_worse_copy_only(con):
    """The duplicate test's two ways of matching (same unit; a floor-band agency report on the
    same building and flat), the source-rank tie-break, the price and date limits, and
    quarantined rows not counting as the other copy. Written against the OR join and kept
    for its replacement (two equi-joins, 2026-10-21): both must flag exactly these rows."""
    _setup_building(con)
    con.execute("INSERT INTO estate (estate_id, name_zh, group_name, source, source_key) "
                "VALUES ('lp', 'LP6', 'lohas-park', 'hse28', '9')")
    con.execute("INSERT INTO building (building_id, estate_id, name_zh) VALUES ('lpb', 'lp', 'x')")
    bad = datetime(2026, 10, 16, 19, 30)  # quarantined: Centadata LOHAS Park before 19:44
    db.insert_txn_raw(con, [
        # (a) one sale of flat A on floor 10, from 28Hse (registry, rank 1) and Centadata
        # (rank 3), plus an agency report (rank 2) that gives only the floor band
        _txn_raw_row("h-A", 8_000_000, date(2024, 1, 20), unit="A"),
        _txn_raw_row("c-A", 8_000_000, date(2024, 1, 20), unit="A", source="centadata"),
        _txn_raw_row("m-A", 8_050_000, date(2024, 1, 5), unit="A", floor="高層",
                     deal_type="market"),
        # same unit but the price is 6% off, or the date is 7 months away: other sales
        _txn_raw_row("c-A-far", 8_500_000, date(2024, 1, 20), unit="A", source="centadata"),
        _txn_raw_row("c-A-old", 8_000_000, date(2023, 6, 1), unit="A", source="centadata"),
        # a band report of another flat in the same building: no match
        _txn_raw_row("m-E", 8_000_000, date(2024, 1, 20), unit="E", floor="中層",
                     deal_type="market"),
        # (b) two sources of equal rank (both 9): the alphabetically first source wins
        _txn_raw_row("r-B", 7_000_000, date(2024, 3, 1), unit="B", source="ricacorp"),
        _txn_raw_row("s-B", 7_000_000, date(2024, 3, 1), unit="B", source="squarefoot"),
        # (c) a quarantined Centadata row is not "the other copy": the rank-9 row of the
        # same unit, and the band report of the same flat, are kept
        _txn_raw_row("q-C", 8_000_000, date(2024, 5, 5), unit="C", building="lpb",
                     source="centadata", estate="lp", fetched_at=bad),
        _txn_raw_row("r-C", 8_000_000, date(2024, 5, 5), unit="C", building="lpb",
                     source="ricacorp", estate="lp"),
        _txn_raw_row("q-D", 8_000_000, date(2024, 5, 5), unit="D", building="lpb",
                     source="centadata", estate="lp", fetched_at=bad),
        _txn_raw_row("r-D", 8_000_000, date(2024, 5, 9), unit="D", building="lpb",
                     source="ricacorp", estate="lp", floor="低層"),
    ])
    flags = dict(con.execute("SELECT source_id, excl_duplicate FROM txn_all").fetchall())
    assert flags == {
        "h-A": False, "c-A": True, "m-A": True, "c-A-far": False, "c-A-old": False,
        "m-E": False,
        "r-B": False, "s-B": True,
        "q-C": False, "r-C": False, "q-D": False, "r-D": False,
    }


def test_same_source_copies_keep_the_lowest_source_id(con):
    """Task 4b (b): one source holding a sale under two ids (Centadata: the agreement and
    its assignment, weeks apart at the same price) keeps the copy with the lowest source_id;
    the same tolerance as across sources (price within 1%, dates within 90 days). A sale of
    the same flat a year later at the same price is a real resale."""
    _setup_building(con)
    db.insert_txn_raw(con, [
        _txn_raw_row("TW02", 1_850_000, date(1995, 8, 22), unit="A", source="centadata"),
        _txn_raw_row("TW01", 1_850_000, date(1995, 6, 30), unit="A", source="centadata"),
        _txn_raw_row("TW03", 1_850_000, date(1996, 6, 30), unit="A", source="centadata"),
        # three copies of one 28Hse registry deal: only the lowest id survives
        _txn_raw_row("h3", 5_000_000, date(2024, 1, 5), unit="B"),
        _txn_raw_row("h2", 5_000_000, date(2024, 1, 5), unit="B"),
        _txn_raw_row("h1", 5_020_000, date(2024, 1, 5), unit="B"),
    ])
    flags = dict(con.execute("SELECT source_id, excl_duplicate FROM txn_all").fetchall())
    assert flags == {"TW01": False, "TW02": True, "TW03": False,
                     "h1": False, "h2": True, "h3": True}


def test_clean_view_drops_deals_dated_before_1990(con):
    """Task 4b (c): Centadata holds a few dozen sales dated 1955-1988 (the data starts in
    1995); they are flagged `excl_bad_date` and left out of `txn`, not deleted."""
    _setup_building(con)
    _txn(con, "old", 1_730_000, date(1955, 9, 8), unit="A", source="centadata")
    _txn(con, "edge", 1_730_000, date(1990, 1, 1), unit="B", source="centadata")
    _txn(con, "ok", 1_730_000, date(1995, 9, 8), unit="C", source="centadata")
    assert _clean_ids(con) == {"edge", "ok"}
    flags = dict(con.execute("SELECT source_id, excl_bad_date FROM txn_all").fetchall())
    assert flags == {"old": True, "edge": False, "ok": False}


def test_centadata_first_hand_label_decides_the_first_hand_flag(con):
    """Task 4b (e): Centadata history rows carry the source's own label
    (`firstOrSecondHand` in raw_json); where present it decides `excl_first_hand`. Rows
    without it (Centadata latest-deal rows, other sources) keep the stored flag."""
    _setup_building(con)
    db.insert_txn_raw(con, [
        _txn_raw_row("fh", 8_000_000, date(2024, 1, 5), unit="A", source="centadata",
                     raw={"firstOrSecondHand": "FirstHand"}),
        _txn_raw_row("sh", 8_000_000, date(2024, 2, 5), unit="B", source="centadata",
                     raw={"firstOrSecondHand": "SecondHand"}, first_hand=True),
        _txn_raw_row("none-fh", 8_000_000, date(2024, 3, 5), unit="C", source="centadata",
                     first_hand=True),
        _txn_raw_row("none", 8_000_000, date(2024, 4, 5), unit="D", source="centadata"),
        _txn_raw_row("srpe", 8_000_000, date(2024, 5, 5), unit="E", source="srpe",
                     first_hand=True),
    ])
    rows = con.execute(
        "SELECT source_id, excl_first_hand, first_hand_label FROM txn_all").fetchall()
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        "fh": (True, True), "sh": (False, False), "none-fh": (True, None),
        "none": (False, None), "srpe": (True, None)}
    assert _clean_ids(con) == {"sh", "none"}


LOCK_MSG = ("IO Error: Cannot open file: The process cannot access the file because it is "
            "being used by another process.")


def test_connect_backs_off_from_1s_to_5s_and_logs_each_wait(tmp_path, monkeypatch, capsys):
    import duckdb

    sleeps = []
    clock = [0.0]
    real = duckdb.connect
    tries = []

    def busy_then_free(path, *a, **k):
        tries.append(1)
        if len(tries) <= 7:
            raise duckdb.IOException(LOCK_MSG)
        return real(path, *a, **k)

    monkeypatch.setattr(db.duckdb, "connect", busy_then_free)
    monkeypatch.setattr(db.time, "monotonic", lambda: clock[0])

    def fake_sleep(s):
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr(db.time, "sleep", fake_sleep)
    db.connect(tmp_path / "x.duckdb").close()
    base = [1, 2, 4, 5, 5, 5, 5]
    assert all(b <= s < b + 1 for s, b in zip(sleeps, base, strict=True))  # + jitter < 1 s
    assert capsys.readouterr().err.count("database busy") == 7


def test_connect_gives_up_after_the_wait_limit(tmp_path, monkeypatch):
    import duckdb

    clock = [0.0]

    def always_busy(path, *a, **k):
        raise duckdb.IOException(LOCK_MSG)

    monkeypatch.setattr(db.duckdb, "connect", always_busy)
    monkeypatch.setattr(db.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(db.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    with pytest.raises(duckdb.IOException):
        db.connect(tmp_path / "x.duckdb")
    assert 25 * 60 <= clock[0] <= 30 * 60  # waited about the 30 minutes, not 5


def test_long_jobs_wait_for_the_file_for_ever(tmp_path, monkeypatch, capsys):
    import duckdb

    clock = [0.0]
    real = duckdb.connect
    tries = []

    def busy_for_three_hours(path, *a, **k):
        tries.append(1)
        if clock[0] < 3 * 3600:
            raise duckdb.IOException(LOCK_MSG)
        return real(path, *a, **k)

    monkeypatch.setattr(db.duckdb, "connect", busy_for_three_hours)
    monkeypatch.setattr(db.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(db.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(db, "_lock_wait_s", db.LOCK_WAIT_S)
    db.set_lock_wait(None)  # what the CLI does for backfill-territory, cd-history, ...
    db.connect(tmp_path / "x.duckdb").close()
    assert clock[0] >= 3 * 3600
    lines = capsys.readouterr().err.count("database busy")
    assert lines < len(tries) - 1 and lines >= db.LOCK_LOG_EVERY_FIRST  # fewer lines later


def test_the_cli_sets_the_long_wait_only_for_long_jobs(tmp_path, monkeypatch):
    from hkestate import cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_status", lambda args: seen.setdefault("status",
                                                                       db.lock_wait()) and 0)
    monkeypatch.setattr(cli, "cmd_cd_history", lambda args: seen.setdefault("cd",
                                                                            db.lock_wait()) or 0)
    cli.main(["--db", str(tmp_path / "x.duckdb"), "status"])
    cli.main(["--db", str(tmp_path / "x.duckdb"), "cd-history"])
    assert seen == {"status": db.LOCK_WAIT_S, "cd": None}
    assert db.lock_wait() == db.LOCK_WAIT_S  # put back after the command


def test_sessions_of_threads_in_one_process_take_turns(tmp_path):
    """DuckDB lets threads of one process open the same file, but two writing the same row
    at once get 'Conflict on update' (checked 2026-10-20); db.session serialises them."""
    import threading

    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        con.execute("CREATE TABLE u (k INTEGER PRIMARY KEY, v INTEGER)")
        con.execute("INSERT INTO u VALUES (1, 0)")
    errors = []

    def work():
        for _ in range(50):
            try:
                with db.session(path) as con:
                    con.execute("UPDATE u SET v = v + 1 WHERE k = 1")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.session(path) as con:
        assert con.execute("SELECT v FROM u").fetchone()[0] == 150
    assert errors == []


def test_connect_does_not_retry_other_errors(tmp_path, monkeypatch):
    import duckdb

    calls = []

    def broken(path, *a, **k):
        calls.append(1)
        raise duckdb.IOException("IO Error: Could not read enough bytes from file")

    monkeypatch.setattr(db.duckdb, "connect", broken)
    monkeypatch.setattr(db.time, "sleep", lambda s: pytest.fail("must not wait"))
    with pytest.raises(duckdb.IOException):
        db.connect(tmp_path / "x.duckdb")
    assert len(calls) == 1


def test_connect_waits_for_a_real_second_process(tmp_path, monkeypatch):
    """A child process holds the file for ~2 s; connect() waits and then succeeds."""
    import subprocess
    import sys

    path = tmp_path / "held.duckdb"
    db.connect(path).close()
    code = ("import duckdb,sys,time; c=duckdb.connect(sys.argv[1]); print('held',flush=True);"
            "time.sleep(2); c.close()")
    child = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE,
                             text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        monkeypatch.setattr(db, "LOCK_BACKOFF_FIRST_S", 0.5)
        con = db.connect(path, wait_s=60)
        con.close()
    finally:
        child.wait(timeout=30)


def test_python_dash_m_hkestate_runs_the_cli(tmp_path):
    """The scheduled tasks start `python.exe -m hkestate <command>` (not hkestate.exe)."""
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "hkestate", "--db", str(tmp_path / "t.duckdb"),
                          "status"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
