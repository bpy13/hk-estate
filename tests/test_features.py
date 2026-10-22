"""Task 4.1: the feature table. One row per deal and one per listing, with the feature
definitions the plan names, and the Phase 4 exclusions (parking, first-hand, houses)."""

import math
from datetime import date, datetime

import pandas as pd
import pytest
from phase4_data import add_deal, add_listing, make_db

from hkestate import db, features

# ---- single definitions ---------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("東南", "SE"), ("西北", "NW"), ("南", "S"), ("北", "N"), ("東", "E"), ("西", "W"),
    ("東北", "NE"), ("西南", "SW"), ("South East", "SE"), ("north-west", "NW"), ("S", "S"),
    ("南北", "unknown"), (None, "unknown"), ("", "unknown"), ("開揚", "unknown"),
])
def test_facing_bins(text, expected):
    assert features.facing_bin(text) == expected
    assert features.facing_bin(text) in features.FACING_BINS


@pytest.mark.parametrize("text,expected", [
    ("元朗區", "元朗區"), ("將軍澳 (西貢區)", "西貢區"), ("華景 | 荔灣 (葵青區)", "葵青區"),
    (None, None), ("", None),
])
def test_district_name(text, expected):
    assert features.district_name(text) == expected


def test_month_index_counts_from_january_2000():
    assert features.month_index(date(2000, 1, 15)) == 0
    assert features.month_index(date(2026, 10, 1)) == 26 * 12 + 9


def test_relative_floor_is_floor_over_top_floor():
    assert features.relative_floor(15, 30) == pytest.approx(0.5)
    assert features.relative_floor(0, 30) == 0
    assert math.isnan(features.relative_floor(None, 30))
    assert math.isnan(features.relative_floor(10, None))
    # a label above the recorded top (skipped labels, roof units) is capped
    assert features.relative_floor(40, 30) == pytest.approx(1.2)


def test_listing_floor_uses_exact_floor_then_band_then_middle():
    assert features.listing_floor("23樓", "unknown", 30) == (23, pytest.approx(23 / 30), "exact")
    num, rel, basis = features.listing_floor("高層", "high", 30)
    assert basis == "band" and rel == pytest.approx(5 / 6) and num == 25
    num, rel, basis = features.listing_floor(None, "low", 30)
    assert basis == "band" and rel == pytest.approx(1 / 6)
    num, rel, basis = features.listing_floor(None, "unknown", 30)
    assert basis == "imputed" and rel == pytest.approx(0.5)
    num, rel, basis = features.listing_floor(None, "unknown", None)
    assert basis == "imputed" and math.isnan(rel)


def test_rvd_class_by_saleable_area():
    # RVD classes: A < 40 m2, B 40-69.9, C 70-99.9, D 100-159.9, E 160+ (1 m2 = 10.764 sq ft)
    assert features.rvd_class(400) == "A"
    assert features.rvd_class(500) == "B"
    assert features.rvd_class(900) == "C"
    assert features.rvd_class(1500) == "D"
    assert features.rvd_class(2000) == "E"
    assert features.rvd_class(None) == "ALL"


def test_special_unit_words():
    assert features.is_special_unit("連天台")
    assert features.is_special_unit("連1樓(複式)")
    assert features.is_special_unit("頂層")
    assert not features.is_special_unit("")
    assert not features.is_special_unit(None)


def test_agreement_date_shifts_28hse_registry_dates_only():
    df = pd.DataFrame({
        "source": ["hse28", "hse28", "centadata", "srpe"],
        "deal_type": ["registry", "market", "registry", "first_hand"],
        "deal_date": pd.to_datetime(["2024-03-15", "2024-03-15", "2024-03-15", "2024-03-15"]),
        "instrument_date": pd.to_datetime([None, None, "2024-03-01", "2024-03-02"]),
    })
    got = features.agreement_dates(df, lag_days=14)
    assert list(got.dt.date) == [date(2024, 3, 1), date(2024, 3, 15), date(2024, 3, 1),
                                 date(2024, 3, 2)]


# ---- first-hand rule -------------------------------------------------------------------------


def _rule_frame(rows, flats=None):
    df = pd.DataFrame(rows, columns=["unit_id", "building_id", "agreement_date",
                                     "anchor_date", "srpe_building"])
    df["building_flats"] = flats  # None: count the flats seen in the deals
    return df


def test_first_hand_rule_compares_early_sales_with_all_flats_in_the_building():
    # an old building whose recorded history starts in 2018 (28Hse): its first recorded
    # deals are resales, a few % of its 300 flats a year, not a launch
    rows = [(f"t|{i}|A", "t", pd.Timestamp("2018-01-01") + pd.Timedelta(days=20 * i), pd.NaT,
             False) for i in range(60)]
    assert not features.first_hand_rule(_rule_frame(rows, flats=300)).any()


def test_first_hand_rule_with_completion_date():
    df = _rule_frame([
        # completed 2020-06 (2010 or later: 72-month window): a flat's first deal before
        # 2026-06 is a developer sale
        ("b|1|A", "b", pd.Timestamp("2020-03-01"), pd.Timestamp("2020-06-01"), False),
        ("b|1|A", "b", pd.Timestamp("2023-05-01"), pd.Timestamp("2020-06-01"), False),
        ("b|2|A", "b", pd.Timestamp("2027-01-01"), pd.Timestamp("2020-06-01"), False),
    ])
    assert list(features.first_hand_rule(df)) == [True, False, False]


def test_first_hand_window_is_72_months_for_buildings_completed_2010_or_later():
    new = pd.Timestamp("2015-06-01")
    old = pd.Timestamp("2005-06-01")
    df = _rule_frame([
        # first-ever sale 50 months after completion
        ("n|1|A", "n", new + pd.DateOffset(months=50), new, False),
        # a second sale of a flat is never first-hand
        ("n|1|A", "n", new + pd.DateOffset(months=55), new, False),
        # first sale 73 months after completion: outside the window
        ("n|2|A", "n", new + pd.DateOffset(months=73), new, False),
        # the same 50 months in a building completed before 2010: window stays 24 months
        ("o|1|A", "o", old + pd.DateOffset(months=50), old, False),
        ("o|2|A", "o", old + pd.DateOffset(months=20), old, False),
    ])
    assert list(features.first_hand_rule(df)) == [True, False, False, False, True]


def test_first_hand_rule_from_first_deal_needs_a_cluster_of_early_sales():
    # no completion date: the building's first deal is the anchor, but only when many flats
    # first sold in that window (a developer launch), not a handful of old latest deals
    launch = [(f"n|{i}|A", "n", pd.Timestamp("2024-01-01") + pd.Timedelta(days=i), pd.NaT, False)
              for i in range(8)]
    resale = [("n|0|A", "n", pd.Timestamp("2026-05-01"), pd.NaT, False)]
    later = [(f"n|{i}|B", "n", pd.Timestamp("2027-06-01"), pd.NaT, False) for i in range(2)]
    got = features.first_hand_rule(_rule_frame(launch + resale + later))
    assert list(got) == [True] * 8 + [False] + [False, False]

    old = [("o|1|A", "o", pd.Timestamp("2005-01-01"), pd.NaT, False)] + [
        (f"o|{i}|B", "o", pd.Timestamp("2015-01-01") + pd.Timedelta(days=30 * i), pd.NaT, False)
        for i in range(2, 40)]
    assert not features.first_hand_rule(_rule_frame(old)).any()


def test_first_hand_rule_skips_buildings_srpe_covers_and_data_start():
    df = _rule_frame([
        ("s|1|A", "s", pd.Timestamp("2021-01-01"), pd.Timestamp("2021-01-01"), True),
        # anchored on a first deal before 1998: the data simply starts there
        ("o|1|A", "o", pd.Timestamp("1996-01-01"), pd.NaT, False),
        ("o|2|A", "o", pd.Timestamp("1996-02-01"), pd.NaT, False),
    ])
    assert not features.first_hand_rule(df).any()


def test_price_check_flags_deals_far_from_the_building_median_even_in_clusters():
    # 10 ordinary deals around HK$10,000/sq ft and 4 part-share-like deals at 10-43% of it:
    # too many for a MAD rule, but each is under half the building's index-adjusted median
    psf = [10000, 10200, 9800, 10100, 9900, 10050, 9950, 10300, 9700, 10000,
           1100, 2200, 4300, 1700, 21000]
    adj = pd.Series(psf, dtype=float)
    building = pd.Series(["b"] * len(psf))
    got = features.price_check(adj, building)
    assert list(got) == [False] * 10 + [True] * 5  # 21,000 is over double: flagged too
    # a building with fewer than 5 deals is not judged
    assert not features.price_check(pd.Series([10000.0, 1000.0]), pd.Series(["c", "c"])).any()


def test_low_psf_check_floor_under_the_district_and_the_flats_own_sales():
    """Task 8 (cheap-sale rule). A 50-row sample of the clean sales under HK$1,000/sq ft was
    94% real 1995-2006 tenement prices, so a district-level floor must sit very low (0.15 x the
    district's index-adjusted median); part shares show up against the flat's own other clean
    sales instead (under half of them, within 5 years)."""
    adj = pd.Series([10000.0, 9500.0, 10500.0, 1200.0, 2000.0,  # district x
                     10000.0, 10500.0, 4000.0, 4000.0,           # unit u in district y
                     1000.0])                                     # no district
    district = pd.Series(["x"] * 5 + ["y"] * 4 + [None])
    unit = pd.Series(["x1", "x2", "x3", "x4", "x5", "u", "u", "u", "u", "z"])
    dates = pd.to_datetime(["2020-01-01"] * 5
                           + ["2020-01-01", "2022-01-01", "2021-01-01", "2010-01-01"]
                           + ["2020-01-01"])
    got = features.low_psf_check(adj, district, unit, dates)
    # 1,200 is 0.12 of district x's median (10,000): flagged; 2,000 (0.2) is not
    assert list(got[:5]) == [False, False, False, True, False]
    # the 2021 sale at 4,000 is under half the flat's other clean sales within 5 years; the
    # 2010 one has no other sale within 5 years and sits at 0.4 of district y: kept
    assert list(got[5:9]) == [False, False, True, False]
    # no district: judged against the median of every clean deal
    assert bool(got[9])


def test_low_psf_reason_on_deals(tmp_path):
    path = make_db(tmp_path / "t.duckdb", n_deals=120)
    con = db.connect(path)
    # a small building (under 5 clean deals, so the building price check never judges it)
    con.execute("INSERT INTO building (building_id, estate_id, name_zh, completion_date, floors, "
                "flats, saleable_to_gross) VALUES ('cd-1-LOW', 'cd-2-AAA', '第4座', "
                "DATE '2005-06-01', 30, 240, NULL)")
    con.execute("INSERT INTO master_building (building_id, estate_code, block_name, district, "
                "lat, lon, mtr_m, built_year, built_basis) VALUES ('cd-1-LOW', '2-AAA', '第4座', "
                "'元朗區', 22.46, 114.0, 800, 2005, 'completion')")
    # flat 20/B sold whole in 2024 and as a part share (about a third of the price) in 2025
    add_deal(con, "whole", "cd-1-LOW", 20, "B", date(2024, 1, 10), 7_000_000, source="centadata")
    add_deal(con, "part", "cd-1-LOW", 20, "B", date(2025, 1, 10), 2_500_000, source="centadata")
    # a lone sale at HK$1,200/sq ft, about a tenth of the district's level
    add_deal(con, "floor", "cd-1-LOW", 8, "C", date(2025, 3, 1), 600_000, source="centadata")
    raw = features.extract(con)
    con.close()
    reasons = features.deal_features(raw).set_index("source_id")["exclude_reason"]
    assert reasons["part"] == "low_psf"
    assert reasons["floor"] == "low_psf"
    assert reasons["whole"] is None
    assert "low_psf" in features.EXCLUDE_ORDER


# ---- the feature tables from a database ----------------------------------------------------


@pytest.fixture
def snap(tmp_path):
    path = make_db(tmp_path / "t.duckdb", n_deals=120)
    con = db.connect(path)
    # parking deal, SRPE first-hand deal, house deal
    add_deal(con, "park", "h28-1-b1", 12, "A", date(2025, 5, 1), 9_000_000,
             raw={"with_parking": True})
    # a part-share-like transfer at a fifth of the building's price per sq ft
    add_deal(con, "share", "h28-1-b1", 14, "B", date(2025, 7, 1), 2_000_000, area=520.0)
    add_deal(con, "srpe1", "cd-1-AAA", 20, "C", date(2025, 6, 1), 6_000_000, source="srpe",
             first_hand=True, raw={"has_parking": False})
    add_deal(con, "house", "cd-1-HHH", 1, "H1", date(2025, 6, 1), 30_000_000,
             source="centadata", area=2000.0)
    add_deal(con, "good-lohas", "h28-9-b1", 6, "B", date(2024, 2, 1), 5_100_000,
             source="centadata", estate_id="h28-9", fetched_at=datetime(2026, 10, 16, 19, 50))
    # a new building with a launch of 6 flats in its first months (no completion date)
    for i in range(6):
        add_deal(con, f"new{i}", "cd-1-NEW", 10 + i, "A", date(2026, 3, 1 + i), 7_000_000,
                 source="centadata")
    add_listing(con, "centanet", "L1", "cd-1-AAA", price=6_500_000, area=520.0, flat="B",
                floor_text="高層", band="high", facing="東南", features="分層單位|天台")
    add_listing(con, "centanet", "L2", "h28-1-b1", price=12_000_000, area=610.0, flat="C",
                floor_text="18樓", facing="西北", rooms=3)
    add_listing(con, "centanet", "R1", "cd-1-AAA", price=20_000, area=520.0, kind="rent")
    add_listing(con, "spacious", "S1", None, price=5_000_000, area=400.0)
    con.execute("INSERT INTO risk_flag (source, listing_id, flag, evidence) "
                "VALUES ('centanet', 'L1', 'sea_view', '海景')")
    raw = features.extract(con)
    con.close()
    return raw


def test_one_row_per_deal_with_exclusion_reasons(snap):
    deals = features.deal_features(snap)
    assert deals["source_id"].is_unique
    reasons = deals.set_index("source_id")["exclude_reason"]
    assert reasons["park"] == "parking"
    assert reasons["srpe1"] == "first_hand"
    assert reasons["house"] == "house"
    # a lone one is caught by the yearly MAD rule already; clusters by the price check
    # (test_price_check_flags_deals_far_from_the_building_median_even_in_clusters)
    assert reasons["share"] in ("outlier", "price_check")
    assert "good-lohas" in reasons.index
    assert (reasons[[f"new{i}" for i in range(6)]] == "first_hand_rule").all()
    assert deals["first_hand"].sum() >= 7


def test_deals_dated_after_the_snapshot_are_excluded(tmp_path):
    """Pre-release review 2026-10-21: an SRPE register typo dated one sale 2055-09-26 and
    another 2026-10-26 (after the snapshot). A deal dated after the snapshot's extraction
    date gets `in_future`, next to the pre-1990 `too_old` rule; a deal dated the extraction
    day stays."""
    path = make_db(tmp_path / "t.duckdb", n_deals=120)
    con = db.connect(path)
    add_deal(con, "typo", "cd-1-AAA", 21, "C", date(2055, 9, 26), 6_000_000, source="centadata")
    add_deal(con, "soon", "cd-1-AAA", 22, "C", date(2030, 1, 5), 6_000_000, source="centadata")
    add_deal(con, "edge", "cd-1-AAA", 23, "C", date(2026, 10, 21), 6_000_000,
             source="centadata")
    raw = features.extract(con)
    con.close()
    raw["meta"]["extracted_at"] = "2026-10-21T17:54:59"
    assert features.snapshot_cutoff(raw) == pd.Timestamp("2026-10-21")
    reasons = features.deal_features(raw).set_index("source_id")["exclude_reason"]
    assert reasons["typo"] == "in_future" and reasons["soon"] == "in_future"
    assert reasons["edge"] != "in_future"


def test_centadata_label_beats_the_first_hand_rule_and_chinese_floors_are_read(tmp_path):
    """Task 4b (e): the first-hand rule applies only where Centadata gives no label; a
    SecondHand label keeps a deal the rule would mark, a FirstHand label marks one it would
    miss. (a): a Chinese-numeral floor label with no stored floor number is read on the fly."""
    path = make_db(tmp_path / "t.duckdb", n_deals=120)
    con = db.connect(path)
    # a launch of 6 flats in a new building (no completion date): the rule marks the
    # unlabelled ones; two carry the source's own SecondHand label
    for i in range(6):
        raw = {"firstOrSecondHand": "SecondHand"} if i < 2 else {}
        add_deal(con, f"new{i}", "cd-1-NEW", 10 + i, "A", date(2026, 3, 1 + i), 7_000_000,
                 source="centadata", raw=raw)
    # a 2010 sale in a building completed in 2005: outside the rule's window, labelled first-hand
    add_deal(con, "lab-fh", "cd-1-AAA", 25, "D", date(2010, 3, 1), 3_000_000,
             source="centadata", raw={"firstOrSecondHand": "FirstHand"})
    add_deal(con, "zh", "cd-1-AAA", "十二", "E", date(2025, 3, 1), 4_000_000,
             source="centadata")
    raw = features.extract(con)
    con.close()
    deals = features.deal_features(raw).set_index("source_id")
    reasons = deals["exclude_reason"]
    assert reasons["lab-fh"] == "first_hand"
    assert (reasons[["new2", "new3", "new4", "new5"]] == "first_hand_rule").all()
    assert reasons[["new0", "new1"]].isna().all()
    assert not deals.loc[["new0", "new1"], "first_hand"].any()
    assert deals.loc["zh", "floor_num"] == 12
    assert deals.loc["zh", "rel_floor"] == pytest.approx(12 / 30)


def test_deal_feature_definitions(snap):
    deals = features.deal_features(snap).set_index("source_id")
    for col in ["area_sqft", "rooms", "floor_num", "rel_floor", "top_floor", "facing",
                "sea_view", "special_unit", "age_years", "district", "mtr_m", "estate_units",
                "first_hand", "month_idx", "is_pilot", "log_psf", "rvd_class"]:
        assert col in deals.columns, col
    d = deals.loc["d0"]
    assert d["rel_floor"] == pytest.approx(d["floor_num"] / d["top_floor"], abs=0.01) or (
        d["rel_floor"] == pytest.approx(1.2))
    terr = deals[deals["building_id"] == "cd-1-AAA"].iloc[0]
    assert terr["district"] == "元朗區"
    assert terr["estate_units"] == 800
    assert terr["mtr_m"] == 800
    assert not terr["is_pilot"]
    assert terr["age_years"] == terr["agreement_date"].year - 2005
    pilot = deals[deals["building_id"] == "h28-1-b1"].iloc[0]
    assert pilot["district"] == "東區"  # pilot estates: district by group (28Hse has none)
    assert pilot["is_pilot"]
    # missing MTR distance stays missing (the model copes)
    assert deals[deals["building_id"] == "cd-1-BBB"]["mtr_m"].isna().all()
    # a deal on flat B of cd-1-AAA gets the facing and sea view seen on a listing of that flat
    flat_b = deals[(deals["building_id"] == "cd-1-AAA") & (deals["flat"] == "B")]
    assert (flat_b["facing"] == "SE").all() and flat_b["sea_view"].all()
    assert set(deals["facing"]) <= set(features.FACING_BINS)


def test_one_row_per_active_sale_listing(snap):
    lst = features.listing_features(snap, today=date(2026, 10, 17))
    assert set(lst["listing_id"]) == {"L1", "L2", "S1"}  # rent listing left out
    row = lst.set_index("listing_id").loc["L1"]
    assert row["facing"] == "SE" and row["sea_view"] and row["special_unit"]
    assert row["floor_basis"] == "band" and row["rel_floor"] == pytest.approx(5 / 6)
    assert row["month_idx"] == features.month_index(date(2026, 10, 17))
    assert row["flags"] == "sea_view"
    l2 = lst.set_index("listing_id").loc["L2"]
    assert l2["floor_num"] == 18 and l2["floor_basis"] == "exact" and l2["facing"] == "NW"
    s1 = lst.set_index("listing_id").loc["S1"]
    assert s1["value_note"] == "no building match"


# ---- the snapshot carries school nets and MTR stations (Task 18b) ---------------------------


def test_snapshot_carries_school_nets_and_stations(tmp_path):
    """The decision helper reads a building's primary school net and the MTR stations from
    the snapshot, never from the live database. `school_net` is a column the school-net load
    adds later, so an older database without it still extracts (all None)."""
    from hkestate.sources import schools

    path = make_db(tmp_path / "t.duckdb", n_deals=120)
    con = db.connect(path)
    con.execute("INSERT INTO mtr_station VALUES ('ADM', '金鐘', 'Admiralty', 22.2793, 114.1646)")
    raw = features.extract(con)  # no school_net column yet
    assert raw["buildings"]["school_net"].isna().all()
    schools.ensure_tables(con)
    con.execute("UPDATE master_building SET school_net = '72' WHERE building_id = 'cd-1-AAA'")
    raw = features.extract(con)
    con.close()
    b = features.building_table(raw)
    assert b.loc["cd-1-AAA", "school_net"] == "72" and pd.isna(b.loc["cd-1-BBB", "school_net"])
    assert list(raw["stations"].columns) == ["code", "name_zh", "name_en", "lat", "lon"]
    assert raw["stations"].iloc[0]["code"] == "ADM"
    out = features.save_snapshot(raw, tmp_path / "snap")
    assert (out / "stations.parquet").exists()
    back = features.load_snapshot(out)
    assert back["stations"].iloc[0]["name_en"] == "Admiralty"
    assert back["buildings"].set_index("building_id").loc["cd-1-AAA", "school_net"] == "72"
    # an older snapshot without the file loads an empty frame
    (out / "stations.parquet").unlink()
    assert features.load_snapshot(out)["stations"].empty
