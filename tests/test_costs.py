"""Round-trip costs and rental yields (Task 5.2). Stamp duty amounts are worked by hand from
the IRD tables cited in src/hkestate/costs.py."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from hkestate import costs


@pytest.mark.parametrize("day, price, buyer, duty", [
    # 1997-04-01..1999-03-31 table: $2,176,480-$3,000,000 at 1.5%
    (date(1998, 5, 1), 3_000_000, "additional", 45_000),
    # 1999-04-01..2007-02-28 table: $2,000,000-$2,176,470 is $15,000 + 10% of the excess
    (date(2005, 3, 1), 2_100_000, "additional", 25_000),
    # 2007-02-28..2010-03-31 table: up to $2,000,000 is $100
    (date(2009, 6, 1), 2_000_000, "additional", 100),
    # before Scale 1 (23 Feb 2013) every buyer paid the same table
    (date(2012, 6, 1), 5_000_000, "additional", 150_000),
    # Scale 1 (Part 2 from 5 Nov 2016) for a buyer owning another flat: 6% band
    (date(2015, 6, 1), 5_000_000, "additional", 300_000),
    (date(2015, 6, 1), 5_000_000, "first", 150_000),          # Scale 2: 3% band
    # Part 1 of Scale 1: flat 15% from 5 Nov 2016, 7.5% from 25 Oct 2023
    (date(2018, 6, 1), 8_000_000, "additional", 1_200_000),
    (date(2018, 6, 1), 8_000_000, "first", 300_000),          # 3.75% band
    (date(2023, 12, 1), 8_000_000, "additional", 600_000),
    (date(2023, 12, 1), 8_000_000, "first", 240_000),         # 2023 table: 3% band
    # from 28 Feb 2024 everyone pays Scale 2 (2023 table until 25 Feb 2025)
    (date(2024, 6, 1), 5_000_000, "additional", 112_500),
    # 26 Feb 2025 table: $100 up to $4m; $4m-$4,323,780 is $100 + 20% of the excess
    (date(2025, 6, 1), 4_000_000, "additional", 100),
    (date(2025, 6, 1), 4_100_000, "additional", 20_100),
    (date(2025, 6, 1), 120_000_000, "additional", 5_100_000),  # top rate 4.25% until 2026
    # 26 Feb 2026: residential over $100m at 6.5% (marginal 30% band first)
    (date(2026, 5, 1), 120_000_000, "additional", 7_800_000),
    (date(2026, 5, 1), 105_000_000, "first", 5_750_000),
    (date(2026, 5, 1), 3_000_000, "first", 100),
])
def test_stamp_duty_known_amounts(day, price, buyer, duty):
    assert costs.stamp_duty(price, day, buyer) == pytest.approx(duty, abs=1)


def test_every_table_is_continuous_at_its_band_edges():
    # marginal relief bands exist so that duty never jumps at a threshold
    for start, table in costs.GENERAL_TABLES + [costs.SCALE1_PART2]:
        for upper, *_ in table[:-1]:
            below = costs.apply_table(table, upper)
            above = costs.apply_table(table, upper + 1)
            assert abs(above - below) < 50, (start, upper)


def test_round_trip_costs():
    c = costs.round_trip(start_price=5_000_000, start_date=date(2024, 6, 1),
                         end_price=6_000_000, buyer="additional")
    # buy: duty 112,500 + agent 1% 50,000 + legal 0.3% 15,000; sell: agent 1% 60,000
    assert c["buy_cost"] == pytest.approx(177_500)
    assert c["sell_cost"] == pytest.approx(60_000)


def test_net_growth_is_below_gross_growth():
    p = pd.DataFrame({"start_price": [5_000_000.0], "end_price": [6_000_000.0],
                      "start_date": [pd.Timestamp("2024-06-01")],
                      "years": [4.0]})
    out = costs.add_net_growth(p)
    gross = (6 / 5) ** (1 / 4) - 1
    expect = (6_000_000 * 0.99 / (5_000_000 + 177_500)) ** (1 / 4) - 1
    assert out["net_ann_growth"].iloc[0] == pytest.approx(expect)
    assert out["net_ann_growth"].iloc[0] < gross


def _rvd(months, rent_psm, price_psm, cls="B", region="HK"):
    rows = []
    for m in months:
        rows.append({"kind": "rent", "series": "rvd_avg_rent_psm", "freq": "month",
                     "period": m, "class": cls, "region": region, "value": rent_psm})
        rows.append({"kind": "price", "series": "rvd_avg_price_psm", "freq": "month",
                     "period": m, "class": cls, "region": region, "value": price_psm})
    return pd.DataFrame(rows)


def test_rvd_yield_is_annual_rent_over_price():
    months = pd.date_range("2020-01-01", "2020-12-01", freq="MS")
    y = costs.rvd_yields(_rvd(months, 300.0, 120_000.0))
    assert y.loc[(pd.Timestamp("2020-06-01"), "B", "HK")] == pytest.approx(0.03)


def test_pair_yield_averages_over_the_holding_period_and_applies_the_anchor():
    months = pd.date_range("2020-01-01", "2023-12-01", freq="MS")
    rvd = pd.concat([_rvd(months[:24], 300.0, 120_000.0), _rvd(months[24:], 400.0, 120_000.0)])
    y = costs.rvd_yields(rvd)
    p = pd.DataFrame({"start_date": [pd.Timestamp("2020-01-15")],
                      "end_date": [pd.Timestamp("2023-12-20")],
                      "rvd_class": ["B"], "district": ["東區"]})
    out = costs.pair_yields(p, y, anchor=0.9)
    assert out.iloc[0] == pytest.approx(0.9 * (0.03 + 0.04) / 2)


def test_region_of_district():
    assert costs.region_of("東區") == "HK"
    assert costs.region_of("深水埗區") == "KLN"
    assert costs.region_of("沙田區") == "NT"
    assert costs.region_of(None) is None


def test_anchor_compares_listing_yields_with_rvd():
    months = pd.date_range("2026-01-01", "2026-08-01", freq="MS")
    y = costs.rvd_yields(_rvd(months, 300.0, 120_000.0))  # RVD yield 3%
    rents = pd.DataFrame({"building_id": [f"b{i}" for i in range(25)],
                          "rent_psf": [30.0] * 25})  # HK$30 a sq ft a month
    sales = pd.DataFrame({"building_id": [f"b{i}" for i in range(25)],
                          "sale_psf": [15_000.0] * 25, "rvd_class": "B", "region": "HK"})
    a = costs.anchor_factor(rents, sales, y)
    # listing yield 30*12/15000 = 2.4% vs RVD 3%: anchor 0.8
    assert a["factor"] == pytest.approx(0.8)
    assert a["buildings"] == 25 and a["basis"] == "listings"


def test_anchor_falls_back_to_one_with_few_buildings():
    months = pd.date_range("2026-01-01", "2026-08-01", freq="MS")
    y = costs.rvd_yields(_rvd(months, 300.0, 120_000.0))
    rents = pd.DataFrame({"building_id": ["b1"], "rent_psf": [30.0]})
    sales = pd.DataFrame({"building_id": ["b1"], "sale_psf": [15_000.0], "rvd_class": "B",
                          "region": "HK"})
    a = costs.anchor_factor(rents, sales, y)
    assert a["factor"] == 1.0 and a["basis"] == "rvd_only"
    assert not np.isnan(a["listing_median_yield"])


def test_vectorised_stamp_duty_matches_the_scalar_rule():
    # Task 8: `add_net_growth` uses the array version; it must give the scalar rule's
    # amounts exactly, across every table, both buyers, and the cut-over days themselves
    rng = np.random.default_rng(3)
    n = 3000
    days = pd.to_datetime("1987-06-01") + pd.to_timedelta(rng.integers(0, 365 * 40, n), "D")
    edges = [pd.Timestamp(s) for s, _ in costs.GENERAL_TABLES] + [
        pd.Timestamp(costs.SCALE1_FROM), pd.Timestamp(costs.PART1_ENDS)] + [
        pd.Timestamp(s) for s, _ in costs.PART1_FLAT]
    days = pd.Series(list(days) + edges + [e - pd.Timedelta(days=1) for e in edges])
    price = np.exp(rng.uniform(np.log(3e5), np.log(1.5e8), len(days)))
    for buyer in costs.BUYERS:
        want = np.array([costs.stamp_duty(p, d, buyer) for p, d in zip(price, days, strict=True)])
        got = costs.stamp_duty_many(price, days, buyer)
        np.testing.assert_array_equal(got, want)
    p = pd.DataFrame({"start_price": price, "end_price": price * 1.3, "start_date": days,
                      "years": 4.0})
    out = costs.add_net_growth(p)
    want = [costs.round_trip(s, d, e) for s, d, e in zip(price, days, price * 1.3, strict=True)]
    np.testing.assert_array_equal(out["buy_cost"].to_numpy(), [w["buy_cost"] for w in want])
    np.testing.assert_array_equal(out["sell_cost"].to_numpy(), [w["sell_cost"] for w in want])


def _pair_yields_rowwise(pairs, y, anchor=1.0):
    """The row-by-row rule `pair_yields` replaced (kept here as the reference)."""
    look = costs._yield_lookup(y)
    out = []
    for s, e, cls, dist in zip(pairs["start_date"], pairs["end_date"], pairs["rvd_class"],
                               pairs["district"], strict=True):
        region = costs.region_of(dist) or "NT"
        ser = look.get((cls, region)) if cls in ("A", "B", "C", "D", "E") else None
        ser = ser if ser is not None else look.get(("B", region))
        if ser is None or pd.isna(s) or pd.isna(e):
            out.append(np.nan)
            continue
        lo = pd.Timestamp(s).to_period("M").to_timestamp()
        hi = pd.Timestamp(e).to_period("M").to_timestamp()
        part = ser[(ser.index >= lo) & (ser.index <= hi)]
        if part.empty:
            part = ser.iloc[[0]] if hi < ser.index.min() else ser.iloc[[-1]]
        out.append(float(part.mean()) * anchor)
    return pd.Series(out, index=pairs.index, dtype=float)


def test_vectorised_pair_yields_match_the_row_by_row_rule():
    rng = np.random.default_rng(4)
    months = pd.date_range("2000-01-01", "2024-12-01", freq="MS")
    rows = []
    for cls in "ABC":  # no D/E series: those classes fall back to B
        for region in ("HK", "KLN", "NT"):
            if cls == "C" and region == "KLN":
                continue  # a missing (class, region) also falls back to B
            for m in months:  # a different yield every month, so averages are real tests
                rows += [{"series": "rvd_avg_rent_psm", "freq": "month", "period": m,
                          "class": cls, "region": region, "value": 200.0 + 50 * rng.random()},
                         {"series": "rvd_avg_price_psm", "freq": "month", "period": m,
                          "class": cls, "region": region,
                          "value": 100_000.0 + 20_000 * rng.random()}]
    y = costs.rvd_yields(pd.DataFrame(rows))
    n = 2000
    start = pd.to_datetime("1995-01-01") + pd.to_timedelta(rng.integers(0, 365 * 33, n), "D")
    end = start + pd.to_timedelta(rng.integers(0, 365 * 12, n), "D")
    pairs = pd.DataFrame({
        "start_date": start, "end_date": end,
        "rvd_class": rng.choice(["A", "B", "C", "D", "E", "X", None], n),
        "district": rng.choice(["東區", "深水埗區", "沙田區", None, "unknown"], n)})
    pairs.loc[rng.choice(n, 20, replace=False), "end_date"] = pd.NaT
    got = costs.pair_yields(pairs, y, anchor=0.9)
    want = _pair_yields_rowwise(pairs, y, anchor=0.9)
    assert got.isna().equals(want.isna()) and got.isna().sum() == 20
    np.testing.assert_allclose(got.dropna(), want.dropna(), rtol=1e-10)
    # every branch was exercised: whole-range averages, partial overlaps, before and after
    inside = (end >= months[0]) & (start <= months[-1]) & pairs["end_date"].notna()
    assert 0 < inside.sum() < n and (end < months[0]).any() and (start > months[-1]).any()


def test_total_return_adds_the_yield_to_net_growth():
    months = pd.date_range("2018-01-01", "2024-12-01", freq="MS")
    y = costs.rvd_yields(_rvd(months, 300.0, 120_000.0))
    p = pd.DataFrame({"start_price": [5_000_000.0], "end_price": [6_000_000.0],
                      "start_date": [pd.Timestamp("2019-06-01")],
                      "end_date": [pd.Timestamp("2023-06-01")], "years": [4.0],
                      "rvd_class": ["B"], "district": ["灣仔區"]})
    out = costs.add_total_return(costs.add_net_growth(p), y, anchor=1.0)
    assert out["rental_yield"].iloc[0] == pytest.approx(0.03)
    assert out["total_return"].iloc[0] == pytest.approx(out["net_ann_growth"].iloc[0] + 0.03)
