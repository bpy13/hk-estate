"""Repeat-sales pairs (Task 5.1): only clean deals on the same unit, held 3+ years, with
first-hand starts tagged; Ricacorp, Centadata % change and estimated pairs kept apart."""

import numpy as np
import pandas as pd
import pytest

from hkestate import pairs


def deal(unit, day, price, *, reason=None, source="hse28", sid=None, area=500.0, **kw):
    row = {
        "unit_id": unit, "building_id": unit.split("|")[0], "source": source,
        "source_id": sid or f"{unit}:{day}", "agreement_date": pd.Timestamp(day),
        "price_hkd": price, "area_sqft": area, "psf_hkd": price / area,
        "exclude_reason": reason, "parking": False, "excl_low_price": price < 500_000,
        "excl_non_market": False, "excl_outlier": False, "gain_pct": np.nan,
        "district": "東區", "built_year": 1990.0, "is_pilot": True, "rel_floor": 0.5,
        "floor_num": 10.0, "facing": "unknown", "sea_view": False, "special_unit": False,
        "mtr_m": 400.0, "estate_units": 12000.0, "rvd_class": "B", "estate_key": "tks",
    }
    row.update(kw)
    return row


def frame(*rows):
    return pd.DataFrame(list(rows))


def test_pair_needs_same_unit_and_three_years():
    d = frame(
        deal("b|1|A", "2018-01-10", 8_000_000),
        deal("b|1|A", "2022-01-10", 10_000_000),   # 4 years: a pair
        deal("b|1|A", "2023-06-01", 11_000_000),   # 1.4 years after the last: no pair
        deal("b|2|A", "2019-01-10", 9_000_000),    # a different unit, alone
    )
    p = pairs.real_pairs(d)
    assert len(p) == 1
    row = p.iloc[0]
    assert row["unit_id"] == "b|1|A"
    assert row["start_price"] == 8_000_000 and row["end_price"] == 10_000_000
    assert row["years"] == pytest.approx(4.0, abs=0.01)
    assert row["ann_growth"] == pytest.approx((10 / 8) ** (1 / row["years"]) - 1)
    assert row["pair_type"] == "real"
    assert not row["first_hand_start"]


def test_excluded_deals_never_start_or_end_a_pair():
    d = frame(
        deal("b|1|A", "2015-01-10", 6_000_000),
        deal("b|1|A", "2019-01-10", 2_000_000, reason="price_check"),  # part-share transfer
        deal("b|1|A", "2023-01-10", 10_000_000),
        deal("b|2|A", "2015-01-10", 6_000_000, reason="parking"),
        deal("b|2|A", "2020-01-10", 9_000_000),
        deal("b|3|A", "2015-01-10", 6_000_000),
        deal("b|3|A", "2020-01-10", 6_000_000, reason="duplicate"),
    )
    p = pairs.real_pairs(d)
    # the price-check deal is skipped, so the clean 2015 and 2023 deals pair up
    assert p[["unit_id", "start_price", "end_price"]].values.tolist() == [
        ["b|1|A", 6_000_000, 10_000_000]]


def test_first_hand_start_is_tagged_and_ignores_the_bulk_rule():
    d = frame(
        deal("b|1|A", "2016-03-01", 5_000_000, reason="first_hand_rule"),
        deal("b|1|A", "2021-03-01", 7_000_000),
        deal("b|2|A", "2016-03-01", 5_000_000, reason="first_hand", parking=True),
        deal("b|2|A", "2021-03-01", 7_000_000),
        deal("b|3|A", "2016-03-01", 5_000_000, reason="first_hand", excl_outlier=True),
        deal("b|3|A", "2021-03-01", 7_000_000),
    )
    p = pairs.real_pairs(d)
    assert p["unit_id"].tolist() == ["b|1|A"]
    assert bool(p.iloc[0]["first_hand_start"])


def test_a_first_hand_deal_cannot_end_a_pair():
    d = frame(deal("b|1|A", "2010-01-01", 5_000_000),
              deal("b|1|A", "2016-01-01", 7_000_000, reason="first_hand_rule"))
    assert pairs.real_pairs(d).empty


def test_pairs_with_very_different_areas_are_dropped():
    d = frame(deal("b|1|A", "2015-01-01", 5_000_000, area=500.0),
              deal("b|1|A", "2020-01-01", 7_000_000, area=700.0))
    assert pairs.real_pairs(d).empty


def test_same_day_copies_count_once():
    d = frame(deal("b|1|A", "2015-01-01", 5_000_000, sid="x1"),
              deal("b|1|A", "2015-01-01", 5_000_000, sid="x2", source="centadata"),
              deal("b|1|A", "2020-01-01", 7_000_000))
    assert len(pairs.real_pairs(d)) == 1


def test_ricacorp_pairs_use_the_previous_deal_and_a_clean_current_deal():
    d = frame(deal("b|1|A", "2026-05-01", 10_000_000, source="centadata", sid="c1"),
              deal("b|2|A", "2026-05-01", 10_000_000, source="centadata", sid="c2",
                   reason="price_check"))
    dp = pd.DataFrame([
        {"cur_source": "centadata", "cur_id": "c1", "unit_id": "b|1|A",
         "cur_date": pd.Timestamp("2026-05-01"), "cur_price_hkd": 10_000_000,
         "prev_date": pd.Timestamp("2006-05-01"), "prev_price_hkd": 4_000_000,
         "hold_days": 7305, "gain_pct": 150.0, "prev_derived": True},
        {"cur_source": "centadata", "cur_id": "c2", "unit_id": "b|2|A",
         "cur_date": pd.Timestamp("2026-05-01"), "cur_price_hkd": 10_000_000,
         "prev_date": pd.Timestamp("2006-05-01"), "prev_price_hkd": 4_000_000,
         "hold_days": 7305, "gain_pct": 150.0, "prev_derived": True},
        # a matched stored previous deal is already a real pair: not repeated here
        {"cur_source": "centadata", "cur_id": "c1", "unit_id": "b|1|A",
         "cur_date": pd.Timestamp("2026-05-01"), "cur_price_hkd": 10_000_000,
         "prev_date": pd.Timestamp("2010-05-01"), "prev_price_hkd": 5_000_000,
         "hold_days": 5844, "gain_pct": 100.0, "prev_derived": False},
    ])
    p = pairs.ricacorp_pairs(dp, d)
    assert len(p) == 1
    assert p.iloc[0]["start_price"] == 4_000_000
    assert p.iloc[0]["pair_type"] == "ricacorp"
    assert p.iloc[0]["years"] == pytest.approx(20.0, abs=0.01)


def test_gain_pairs_give_previous_price_without_a_date():
    d = frame(deal("b|1|A", "2017-05-01", 6_000_000, source="centadata", gain_pct=50.0),
              deal("b|2|A", "2017-05-01", 6_000_000, source="centadata", gain_pct=20.0,
                   reason="outlier"),
              deal("b|3|A", "2017-05-01", 6_000_000, source="centadata", gain_pct=20.0,
                   reason="duplicate"))
    g = pairs.gain_pairs(d)
    assert sorted(g["unit_id"]) == ["b|1|A", "b|3|A"]  # a duplicate copy still carries its gain
    row = g.set_index("unit_id").loc["b|1|A"]
    assert row["start_price"] == pytest.approx(4_000_000)
    assert pd.isna(row["start_date"])
    assert row["log_gain"] == pytest.approx(np.log(1.5))


def test_gain_pairs_check_against_real_pairs():
    d = frame(deal("b|1|A", "2018-05-01", 6_000_000),
              deal("b|1|A", "2022-05-01", 7_500_000, source="centadata", gain_pct=25.0))
    real = pairs.real_pairs(d)
    g = pairs.gain_pairs(d)
    chk = pairs.gain_check(g, real)
    assert chk["n"] == 1
    assert chk["median_abs_diff"] == pytest.approx(0.0, abs=1e-9)
    # a gain pair whose end is already a real pair's end is not an extra pair
    assert pairs.gain_pairs(d, exclude=real).empty


def test_estimated_pair_candidates_are_flats_last_sold_before_2018():
    d = frame(
        deal("b|1|A", "2005-01-01", 3_000_000),                        # last sold 2005: yes
        deal("b|2|A", "2005-01-01", 3_000_000),
        deal("b|2|A", "2021-01-01", 7_000_000),                        # sold again: no
        deal("b|3|A", "2010-01-01", 4_000_000, reason="first_hand_rule"),  # yes, tagged
        deal("b|4|A", "2010-01-01", 4_000_000),
        deal("b|4|A", "2020-01-01", 2_000_000, reason="price_check"),  # odd later deal: no
    )
    c = pairs.estimate_candidates(d)
    assert sorted(c["unit_id"]) == ["b|1|A", "b|3|A"]
    assert c.set_index("unit_id").loc["b|3|A", "first_hand_start"]


def test_estimated_pairs_carry_model_error():
    c = pairs.estimate_candidates(frame(deal("b|1|A", "2005-01-01", 3_000_000)))
    e = pairs.estimated_pairs(c, value_now=np.array([6_000_000.0]),
                              value_date=pd.Timestamp("2026-08-15"), sigma=np.array([0.08]))
    row = e.iloc[0]
    assert row["pair_type"] == "estimated"
    assert row["end_price"] == 6_000_000
    assert row["log_growth"] == pytest.approx(np.log(2))
    assert row["ann_sd_model"] == pytest.approx(0.08 / row["years"])


def test_ricacorp_pair_repeating_a_real_pair_end_is_dropped():
    # the stored sale at 2026-05-01 already ends a real pair; the derived Ricacorp price is
    # about 1% off so it was not matched as a stored previous deal
    d = frame(deal("b|1|A", "2006-05-01", 4_000_000, sid="s0"),
              deal("b|1|A", "2026-05-01", 10_000_000, source="centadata", sid="c1"),
              deal("b|2|A", "2026-05-01", 10_000_000, source="centadata", sid="c2"))
    dp = pd.DataFrame([
        {"cur_source": "centadata", "cur_id": cid, "unit_id": u,
         "cur_date": pd.Timestamp("2026-05-01"), "cur_price_hkd": 10_000_000,
         "prev_date": pd.Timestamp("2006-05-01"), "prev_price_hkd": 4_040_000,
         "hold_days": 7305, "gain_pct": 147.0, "prev_derived": True}
        for cid, u in (("c1", "b|1|A"), ("c2", "b|2|A"))])
    real = pairs.real_pairs(d)
    assert len(real) == 1
    p = pairs.ricacorp_pairs(dp, d, real=real)
    assert list(p["unit_id"]) == ["b|2|A"]


def test_two_sale_centadata_history_makes_one_real_pair():
    # a flat's whole Centadata history (1996 and 2011 sales), as the history loader stores it
    d = frame(deal("b|27|H", "1996-09-20", 3_390_000, source="centadata"),
              deal("b|27|H", "2011-03-04", 5_860_000, source="centadata", gain_pct=73.0))
    p = pairs.real_pairs(d)
    assert len(p) == 1
    row = p.iloc[0]
    assert row["pair_type"] == "real"
    assert row["start_date"] == pd.Timestamp("1996-09-20")
    assert row["end_date"] == pd.Timestamp("2011-03-04")
    assert (row["start_source"], row["end_source"]) == ("centadata", "centadata")
    assert row["years"] == pytest.approx(14.5, abs=0.05)


def test_window_keeps_only_pairs_with_both_sales_inside():
    # Task 6: a pair counts when both its sales fall inside [start, end]
    d = frame(
        # inside; ends after the window; starts before it
        deal("b|1|A", "1996-01-10", 3_000_000), deal("b|1|A", "2001-01-10", 2_000_000),
        deal("b|2|A", "1996-01-10", 3_000_000), deal("b|2|A", "2005-01-10", 2_500_000),
        deal("b|3|A", "1994-01-10", 3_000_000), deal("b|3|A", "1999-01-10", 2_500_000),
    )
    p = pairs.real_pairs(d)
    assert len(p) == 3
    inside = p[pairs.in_window(p, "1995-01-01", "2003-07-31")]
    assert list(inside["unit_id"]) == ["b|1|A"]
    assert pairs.in_window(p).all()  # no window: everything
    assert list(p[pairs.in_window(p, start="1995-01-01")]["unit_id"]) == ["b|1|A", "b|2|A"]


def test_a_gain_pair_whose_start_date_is_now_known_is_not_a_gain_pair_any_more():
    d = frame(
        # held 14 years: a real pair; its "% change" no longer stands alone
        deal("b|1|A", "1996-09-20", 3_390_000, source="centadata"),
        deal("b|1|A", "2011-03-04", 5_860_000, source="centadata", gain_pct=73.0),
        # held 2 years: too short for a real pair, but dated, so not an undated gain either
        deal("b|2|A", "2019-01-10", 8_000_000, source="centadata"),
        deal("b|2|A", "2021-01-10", 9_000_000, source="centadata", gain_pct=12.5),
        # no earlier sale stored: still a gain pair
        deal("b|3|A", "2020-01-10", 7_000_000, source="centadata", gain_pct=40.0),
    )
    real = pairs.real_pairs(d)
    assert list(real["unit_id"]) == ["b|1|A"]
    assert sorted(pairs.gain_pairs(d, exclude=real)["unit_id"]) == ["b|2|A", "b|3|A"]  # before
    g = pairs.undated_gain_pairs(d, real)
    assert list(g["unit_id"]) == ["b|3|A"]
