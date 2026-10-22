"""Rent estimate (Task 15): each fallback level, the index adjustment, the sanity band."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from hkestate import rent

ON = date(2026, 10, 15)


def _rvd(avg_hk_b=500.0):
    months = pd.date_range("2026-01-01", "2026-08-01", freq="MS")
    rows = []
    for i, m in enumerate(months):
        for g in ("ABC", "DE", "ALL"):
            rows.append(("rent", "rvd_rent_index", "month", m, g, "ALL", 100.0 + 10 * i))
        for c in "ABCDE":
            for r in ("HK", "KLN", "NT"):
                rows.append(("rent", "rvd_avg_rent_psm", "month", m, c, r, avg_hk_b))
    return pd.DataFrame(rows, columns=["kind", "series", "freq", "period", "class", "region",
                                       "value"])


def _listing(building, estate, district, psf, area=500.0, month="2026-08-01", kind="rent"):
    return {"building_id": building, "estate_key": estate, "district": district,
            "kind": kind, "status": "active", "price_hkd": psf * area, "saleable_sqft": area,
            "month": pd.Timestamp(month)}


def _model(rows, anchor=1.0):
    cols = ["building_id", "estate_key", "district", "kind", "status", "price_hkd",
            "saleable_sqft", "month"]
    raw = pd.DataFrame(rows, columns=cols)
    li = rent.clean_rent_listings(raw)
    li = li.assign(month=raw.loc[li.index, "month"])
    bld = pd.DataFrame({"estate_key": ["E1", "E1", "E2", "E3"],
                        "district": ["灣仔區", "灣仔區", "灣仔區", "沙田區"]},
                       index=pd.Index(["b1", "b2", "b3", "b4"], name="building_id"))
    return rent.RentModel.from_frames(li, bld, _rvd(), anchor)


def test_building_level_when_five_listings():
    rows = [_listing("b1", "E1", "灣仔區", 40.0) for _ in range(5)]
    rows += [_listing("b2", "E1", "灣仔區", 60.0) for _ in range(5)]
    m = _model(rows)
    # latest listing month is Aug; October is carried on the last index value: no change
    assert m.estimate("b1", 600, on=ON) == (pytest.approx(40.0 * 600), "building")


def test_estate_level_when_building_has_four():
    rows = [_listing("b1", "E1", "灣仔區", 40.0) for _ in range(4)]
    rows += [_listing("b2", "E1", "灣仔區", 60.0) for _ in range(3)]  # estate: 7 listings
    m = _model(rows)
    rent_, basis = m.estimate("b1", 500, on=ON)
    assert basis == "estate"
    assert rent_ == pytest.approx(40.0 * 500)  # median of 4x40 and 3x60 is 40


def test_district_level_when_estate_short():
    rows = [_listing("b3", "E2", "灣仔區", 50.0) for _ in range(2)]
    rows += [_listing("b1", "E1", "灣仔區", 30.0) for _ in range(3)]  # district: 5
    m = _model(rows)
    rent_, basis = m.estimate("b3", 400, on=ON)
    assert basis == "district"
    assert rent_ == pytest.approx(30.0 * 400)


def test_district_argument_for_unknown_building():
    rows = [_listing("b1", "E1", "灣仔區", 30.0) for _ in range(5)]
    m = _model(rows)
    assert m.estimate("nope", 400, district="灣仔區", on=ON)[1] == "district"


def test_rvd_fallback_per_m2_to_sqft_and_anchor():
    m = _model([_listing("b1", "E1", "灣仔區", 30.0)])  # 1 listing: not enough anywhere
    rent_, basis = m.estimate("b4", 500, on=date(2026, 8, 20))  # Sha Tin: NT, class C
    assert basis == "rvd"
    assert rent_ == pytest.approx(500.0 / 10.7639 * 500)
    m2 = _model([], anchor=1.2)
    assert m2.estimate("b4", 500, on=date(2026, 8, 20))[0] == pytest.approx(
        500.0 / 10.7639 * 500 * 1.2)
    assert m2.achieved_from_asking(1200.0) == pytest.approx(1000.0)


def test_rvd_carries_past_last_month_with_the_rent_index():
    m = _model([])
    # index was 170 in Aug (the last month); October carries it: no extra change
    now, _ = m.estimate("b4", 500, on=date(2026, 10, 1))
    aug, _ = m.estimate("b4", 500, on=date(2026, 8, 1))
    assert now == pytest.approx(aug)


def test_index_adjusts_old_listing_to_current_month():
    # listed in Jan (index 100), asked 40; the index is 170 in Aug -> 68 a sq ft
    rows = [_listing("b1", "E1", "灣仔區", 40.0, month="2026-01-01") for _ in range(5)]
    m = _model(rows)
    rent_, basis = m.estimate("b1", 500, on=date(2026, 8, 5))
    assert basis == "building"
    assert rent_ == pytest.approx(68.0 * 500)


def test_impossible_listings_dropped():
    bad = [_listing("b1", "E1", "灣仔區", 5.0),  # rent per sq ft too low
           _listing("b1", "E1", "灣仔區", 400.0),  # too high (price typed as rent)
           _listing("b1", "E1", "灣仔區", 40.0, area=100.0),  # area too small
           _listing("b1", "E1", "灣仔區", 40.0, area=6000.0),  # area too large
           _listing("b1", "E1", "灣仔區", 40.0, kind="sale")]
    m = _model(bad + [_listing("b1", "E1", "灣仔區", 40.0) for _ in range(4)])
    assert len(m.listings) == 4
    # 4 good listings in the building (< 5): the bad ones must not make it five
    assert m.estimate("b1", 500, on=ON)[1] == "estate" or m.estimate("b1", 500, on=ON)[1] \
        == "rvd"
    assert "building" != m.estimate("b1", 500, on=ON)[1]


def test_nothing_known_returns_nan():
    m = _model([])
    m.avg_rent = {}
    rent_, basis = m.estimate("b4", 500, on=ON)
    assert basis == "none" and np.isnan(rent_)
