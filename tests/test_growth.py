"""Attribute regression, out-of-sample checks, repeat-sales index and drawdown (Task 5.3),
on synthetic pairs with known effects."""

import numpy as np
import pandas as pd
import pytest

from hkestate import growth


def synthetic_pairs(n=3000, seed=3, floor_effect=0.02):
    """Pairs where high floors grow `floor_effect` a year faster; size has no effect."""
    rng = np.random.default_rng(seed)
    districts = np.array(["東區", "沙田區", "觀塘區", "元朗區", "中西區", "西貢區"])
    start = pd.to_datetime("2005-01-01") + pd.to_timedelta(rng.integers(0, 365 * 12, n), "D")
    years = rng.uniform(3, 12, n)
    end = start + pd.to_timedelta((years * 365.25).astype(int), "D")
    rel = rng.uniform(0, 1, n)
    area = rng.choice([350.0, 600.0, 900.0, 1200.0], n)
    district = rng.choice(districts, n)
    ann = 0.03 + floor_effect * (rel > 2 / 3) + rng.normal(0, 0.03, n)
    years_true = (end - start).days / 365.25
    start_price = area * rng.uniform(8000, 12000, n)
    end_price = start_price * np.exp(ann * years_true)
    p = pd.DataFrame({
        "pair_type": "real", "unit_id": [f"b{i % 400}|{i}|A" for i in range(n)],
        "building_id": [f"b{i % 400}" for i in range(n)], "start_date": start,
        "end_date": end, "start_price": start_price, "end_price": end_price,
        "years": years_true, "ann_log_growth": np.log(end_price / start_price) / years_true,
        "area_sqft": area, "start_psf": start_price / area, "rel_floor": rel,
        "mtr_m": rng.uniform(100, 1500, n), "estate_units": rng.choice([500, 5000], n),
        "special_unit": False, "facing": "unknown", "sea_view": False,
        "first_hand_start": False, "built_year": 1990.0, "district": district,
    })
    return p


def test_attribute_frame_builds_dummies():
    p = synthetic_pairs(200)
    d = growth.attribute_frame(p)
    assert set(growth.ATTRIBUTES) <= set(d.columns)
    assert (d["floor_high"] == (p["rel_floor"] > 2 / 3).astype(int)).all()
    assert d["size_small"].sum() == (p["area_sqft"] < 430).sum()


def test_unknown_estate_size_counts_as_not_big():
    # a nullable-integer estate_units with a missing value used to crash the report
    p = synthetic_pairs(50)
    p["estate_units"] = pd.array([5000, None] * 25, dtype="Int64")
    d = growth.attribute_frame(p)
    assert list(d["big_estate"][:2]) == [1, 0]


def test_cluster_ols_matches_least_squares_coefficients():
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"const": 1.0, "x": rng.normal(size=500)})
    y = 1 + 2 * X["x"].to_numpy() + rng.normal(size=500)
    res = growth._ols_cluster(y, X, np.arange(500) % 50)
    beta = np.linalg.lstsq(X.to_numpy(), y, rcond=None)[0]
    assert res["coef"].to_numpy() == pytest.approx(beta)
    assert res.loc["x", "p"] < 1e-6


def test_cluster_standard_errors_match_the_textbook_formula():
    rng = np.random.default_rng(4)
    n, groups = 600, np.arange(600) % 40
    X = pd.DataFrame({"const": 1.0, "x": rng.normal(size=n), "z": rng.normal(size=n)})
    y = 1 + 2 * X["x"].to_numpy() + rng.normal(size=n) + rng.normal(size=40)[groups]
    res = growth._ols_cluster(y, X, groups)
    # the same sandwich, written out plainly: sum over clusters of (X' e)(X' e)'
    Xv = X.to_numpy()
    beta = np.linalg.lstsq(Xv, y, rcond=None)[0]
    e = y - Xv @ beta
    meat = sum(np.outer(Xv[groups == g].T @ e[groups == g], Xv[groups == g].T @ e[groups == g])
               for g in range(40))
    bread = np.linalg.inv(Xv.T @ Xv)
    adj = (40 / 39) * ((n - 1) / (n - 3))
    se = np.sqrt(np.diag(adj * bread @ meat @ bread))
    assert res["se"].to_numpy() == pytest.approx(se)


def test_regression_finds_the_planted_effect_and_marks_it_robust():
    d = growth.attribute_frame(synthetic_pairs())
    v = growth.verdicts(d)
    assert v.loc["floor_high", "coef"] == pytest.approx(0.02, abs=0.004)
    assert v.loc["floor_high", "robust"]
    # size has no effect: not robust (not significant, or the sign flips)
    assert not v.loc["size_small", "robust"] or v.loc["size_small", "p"] < 0.05
    assert abs(v.loc["size_small", "coef"]) < 0.005
    assert set(v.attrs["checks"]) == {"time", "district"}


def test_robust_needs_the_same_sign_in_both_halves():
    d = growth.attribute_frame(synthetic_pairs())
    # flip the effect in half of the districts: still significant on average, but the
    # district check sees opposite signs
    desc, first = growth.district_split(d)
    d.loc[first & (d["floor_high"] == 1), "y"] -= 0.06
    v = growth.verdicts(d)
    assert not v.loc["floor_high", "district_holds"]
    assert not v.loc["floor_high", "robust"]


def test_time_split_falls_back_to_start_year_when_few_pairs_end_by_2018():
    d = growth.attribute_frame(synthetic_pairs())
    d["end_date"] = pd.Timestamp("2026-08-15")  # e.g. estimated pairs: all end today
    desc, first = growth.time_split(d)
    assert desc.startswith("pairs starting by")
    assert 0.3 < first.mean() < 0.7


def test_attributes_with_too_few_pairs_are_not_tested():
    d = growth.attribute_frame(synthetic_pairs(500))
    d["sea_view"] = 0
    d.loc[:4, "sea_view"] = 1
    assert "sea_view" not in growth.testable(d)


def test_rs_index_recovers_a_known_path():
    rng = np.random.default_rng(1)
    months = pd.date_range("2010-01-01", "2020-12-01", freq="MS")
    true = pd.Series(np.cumsum(np.r_[0, rng.normal(0.004, 0.01, len(months) - 1)]),
                     index=months)
    n = 4000
    a = rng.integers(0, len(months) - 36, n)
    b = np.minimum(a + rng.integers(36, 120, n), len(months) - 1)
    p = pd.DataFrame({"start_date": months[a], "end_date": months[b]})
    p["start_price"] = 1e6
    p["end_price"] = 1e6 * np.exp(true.to_numpy()[b] - true.to_numpy()[a]
                                  + rng.normal(0, 0.02, n))
    idx = growth.rs_index(p, smooth=0.5)
    got = np.log(idx / 100)
    assert np.abs(got.to_numpy() - true.to_numpy()).max() < 0.03


def test_rs_index_with_every_pair_in_one_month_is_flat():
    p = pd.DataFrame({"start_date": pd.to_datetime(["2020-01-05", "2020-01-20"]),
                      "end_date": pd.to_datetime(["2020-01-25", "2020-01-28"]),
                      "start_price": [1e6, 2e6], "end_price": [1.1e6, 2.1e6]})
    idx = growth.rs_index(p)
    assert len(idx) == 1 and idx.iloc[0] == 100.0


def test_drawdown_finds_the_worst_fall():
    s = pd.Series([100, 120, 90, 110, 130, 104], index=pd.date_range("2020-01-01", periods=6,
                                                                       freq="MS"))
    dd = growth.drawdown(s)
    assert dd["drawdown"] == pytest.approx(0.25)
    assert dd["peak"] == pd.Timestamp("2020-02-01")
    assert dd["trough"] == pd.Timestamp("2020-03-01")


def test_rvd_long_run_summarises_each_class():
    periods = pd.date_range("1993-01-01", "2002-12-01", freq="MS")
    vals = np.r_[np.linspace(100, 170, 55), np.linspace(170, 60, 65)]
    rvd = pd.DataFrame({"series": "rvd_price_index", "freq": "month", "period": periods,
                        "class": "A", "region": "ALL", "value": vals})
    out = growth.rvd_long_run(rvd)
    row = out.iloc[0]
    assert row["drawdown"] == pytest.approx(1 - 60 / 170)


def test_an_attribute_that_never_varies_within_a_district_is_not_tested():
    d = growth.attribute_frame(synthetic_pairs())
    d["big_estate"] = (d["district"] == "東區").astype(int)  # the same as the district
    assert "big_estate" not in growth.testable(d)
