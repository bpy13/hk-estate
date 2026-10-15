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


def test_unit_id_is_building_floor_flat():
    assert db.unit_id("b1", "10", "A") == "b1|10|A"
