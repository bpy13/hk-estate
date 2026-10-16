"""Task 4.4: `hkestate rank` writes data/reports/ranked_listings_<date>.csv sorted by %
discount, with fair price, interval, confidence (low if <10 comparables in 24 months), flags
and the top 3 SHAP reasons."""

from datetime import date

import pandas as pd
import pytest
from phase4_data import add_listing, make_db

from hkestate import cli, db, features, model, rank

TODAY = date(2026, 10, 17)


def test_discount_is_against_the_expected_deal_price():
    # asking 10m, usual gap 5% -> expected deal 9.5m; fair 10m -> 5% below fair
    assert rank.discount(10_000_000, 10_000_000, 0.05) == pytest.approx(0.05)
    assert rank.discount(12_000_000, 10_000_000, 0.05) == pytest.approx(-0.14)


@pytest.fixture
def ranked(tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    path = make_db(tmp_path / "hkestate.duckdb", n_deals=700)
    con = db.connect(path)
    add_listing(con, "centanet", "CHEAP", "cd-1-AAA", price=3_000_000, area=520.0, flat="B",
                floor_text="高層", band="high", facing="東南", dup_group="g1")
    add_listing(con, "spacious", "CHEAP-DUP", "cd-1-AAA", price=3_100_000, area=520.0,
                flat="B", band="high", dup_group="g1")
    add_listing(con, "centanet", "DEAR", "h28-1-b1", price=40_000_000, area=610.0, flat="C",
                floor_text="18樓", features="分層單位|車位")
    add_listing(con, "centanet", "FEW", "cd-1-NEW", price=7_000_000, area=500.0, flat="A",
                floor_text="中層", band="mid")
    add_listing(con, "centanet", "NOMATCH", None, price=5_000_000, area=400.0)
    con.execute("INSERT INTO risk_flag (source, listing_id, flag, evidence) "
                "VALUES ('centanet', 'CHEAP', 'tenanted', '連租約')")
    raw = features.extract(con)
    con.close()
    model.train(raw, rounds=120, today=TODAY, log=lambda *_: None)
    return rank.rank(raw, model.load(), today=TODAY)


def test_ranked_listings_sorted_by_discount_with_all_columns(ranked):
    df = ranked.table
    assert list(df["discount_pct"]) == sorted(df["discount_pct"], reverse=True)
    assert df.iloc[0]["listing_id"] == "CHEAP"
    for col in ["fair_price_hkd", "fair_low_hkd", "fair_high_hkd", "confidence", "comps_24m",
                "flags", "reason_1", "reason_2", "reason_3", "gap_pct", "gap_basis", "url"]:
        assert col in df.columns, col
    assert (df["fair_low_hkd"] < df["fair_price_hkd"]).all()
    assert (df["fair_price_hkd"] < df["fair_high_hkd"]).all()
    assert df["reason_1"].str.contains("%").all()


def test_listings_are_valued_at_the_last_well_populated_month(ranked):
    m = model.load()
    # synthetic deals run to 2026-09: the valuation month is the last with 200+ deals or,
    # with fewer deals in total, the newest month with the most deals seen recently
    assert (ranked.table["month_idx"] == m.meta["valuation_month_idx"]).all()
    assert m.meta["valuation_month_idx"] <= features.month_index(TODAY)


def test_duplicates_across_sites_are_ranked_once(ranked):
    df = ranked.table
    assert "CHEAP-DUP" not in set(df["listing_id"])
    assert "spacious:CHEAP-DUP" in df.set_index("listing_id").loc["CHEAP", "also_listed"]


def test_gap_assumed_flags_and_confidence(ranked):
    df = ranked.table.set_index("listing_id")
    assert (df["gap_pct"] - 0.05).abs().max() < 1e-9
    assert set(df["gap_basis"]) == {"assumed"}
    assert df.loc["CHEAP", "flags"] == "tenanted"
    assert "with_parking" in df.loc["DEAR", "flags"]
    assert df.loc["FEW", "confidence"] == "low"  # its 6 deals are first-hand, none comparable
    assert df.loc["CHEAP", "confidence"] == "normal"
    assert ranked.counts["not_valued"] == {"no building match": 1}


def test_rank_command_writes_dated_csv(tmp_path, monkeypatch, ranked):
    # the fixture trained a model in tmp_path; the command snapshots and ranks again
    assert cli.main(["--db", str(tmp_path / "hkestate.duckdb"), "rank"]) == 0
    out = list((tmp_path / "reports").glob("ranked_listings_*.csv"))
    assert len(out) == 1
    df = pd.read_csv(out[0], encoding="utf-8-sig")
    assert len(df) == len(ranked.table)
    assert (tmp_path / "model" / "ranked_latest.parquet").exists()
    con = db.connect(tmp_path / "hkestate.duckdb")
    status = con.execute("SELECT status FROM run_log WHERE command = 'rank'").fetchall()
    con.close()
    assert status == [("ok",)]
