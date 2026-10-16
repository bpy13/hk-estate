"""Task 4.1: the feature table. One row per deal and one per listing, with the feature
definitions the plan names, and the Phase 4 exclusions (parking, first-hand, houses, the
quarantined LOHAS Park rows)."""

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
        # completed 2020-06: a flat's first deal before 2022-06 is a developer sale
        ("b|1|A", "b", pd.Timestamp("2020-03-01"), pd.Timestamp("2020-06-01"), False),
        ("b|1|A", "b", pd.Timestamp("2023-05-01"), pd.Timestamp("2020-06-01"), False),
        ("b|2|A", "b", pd.Timestamp("2023-01-01"), pd.Timestamp("2020-06-01"), False),
    ])
    assert list(features.first_hand_rule(df)) == [True, False, False]


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


# ---- the feature tables from a database ----------------------------------------------------


@pytest.fixture
def snap(tmp_path):
    path = make_db(tmp_path / "t.duckdb", n_deals=120)
    con = db.connect(path)
    # parking deal, SRPE first-hand deal, house deal, a quarantined LOHAS Park row
    add_deal(con, "park", "h28-1-b1", 12, "A", date(2025, 5, 1), 9_000_000,
             raw={"with_parking": True})
    add_deal(con, "srpe1", "cd-1-AAA", 20, "C", date(2025, 6, 1), 6_000_000, source="srpe",
             first_hand=True, raw={"has_parking": False})
    add_deal(con, "house", "cd-1-HHH", 1, "H1", date(2025, 6, 1), 30_000_000,
             source="centadata", area=2000.0)
    add_deal(con, "bad-lohas", "h28-9-b1", 5, "A", date(2024, 1, 1), 5_000_000,
             source="centadata", estate_id="h28-9", fetched_at=datetime(2026, 10, 16, 19, 30))
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
    assert "good-lohas" in reasons.index  # fetched by the fixed run: kept
    assert "bad-lohas" not in reasons.index  # quarantined before the flags are computed
    assert snap["meta"]["quarantined_rows"] == 1
    assert (reasons[[f"new{i}" for i in range(6)]] == "first_hand_rule").all()
    assert deals["first_hand"].sum() >= 7


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
