"""Market summary (Task 20): one district checked by hand on a small made-up table."""
import numpy as np
import pandas as pd
import pytest

from hkestate import market

# district X, class C; the data runs to 2026-06-30, so the 3-month change ends 2026-05-19
_X = [("2024-09-01", 100), ("2024-12-01", 120), ("2025-08-01", 130), ("2025-10-01", 140),
      ("2025-12-15", 150), ("2026-01-15", 160), ("2026-03-01", 170), ("2026-04-01", 190),
      ("2026-06-30", 200)]


def _deals():
    rows = [("X", "C", d, p, None) for d, p in _X]
    rows.append(("X", "C", "2026-04-15", 999, "bulk"))  # not a clean sale: ignored
    rows.append(("Y", "C", "2026-06-01", 1000, None))
    return pd.DataFrame(rows, columns=["district", "rvd_class", "agreement_date", "psf_hkd",
                                       "exclude_reason"])


def _listings():
    rows = [("sale", "X", 5000, 1_000_000), ("sale", "X", 5000, 1_200_000),
            ("sale", "X", np.nan, 2_000_000),  # no area: counted, not valued
            ("rent", "X", 500, 30_000), ("sale", "Y", 5000, 5_000_000)]
    li = pd.DataFrame(rows, columns=["kind", "district", "area_sqft", "price_hkd"])
    li["rvd_class"] = "C"
    return li


def _raw():
    idx = pd.DataFrame({"class": "C", "value": [88.0, 100.0, 110.0],
                        "period": pd.to_datetime(["2025-06-01", "2026-03-01", "2026-06-01"])})
    return {"index": idx}


@pytest.fixture
def table():
    return market.summary(_raw(), deals=_deals(), listings=_listings())


def _row(table, district, cls):
    return table[(table.district == district) & (table.size_class == cls)].iloc[0]


def test_one_district_by_hand(table):
    r = _row(table, "X", "C")
    # last 12 months (2025-06-30, 2026-06-30]: 130 140 150 160 170 190 200
    assert r.median_psf_12m == 160 and r.n_sales_12m == 7
    # 12 months before: 100 120 -> 110
    assert r.own_chg_12m == pytest.approx(160 / 110 - 1) and r.n_own_12m == 2
    # 3 months to 2026-05-19: 170 190 -> 180; the 3 before: 150 160 -> 155
    assert r.own_chg_3m == pytest.approx(180 / 155 - 1) and r.n_own_3m == 2
    # RVD index 110 now, 100 three months back, 88 twelve months back
    assert r.idx_chg_3m == pytest.approx(0.10) and r.idx_chg_12m == pytest.approx(0.25)
    assert r.sale_listings == 3 and r.rent_listings == 1
    # asking 200 and 240 -> 220; deals of the last 6 months 160 170 190 200 -> 180
    assert r.median_ask_psf == 220 and r.median_deal_psf_6m == 180
    assert r.n_premium == 0 and np.isnan(r.ask_premium)  # no building ids in this fixture


def test_ask_premium_is_building_matched(monkeypatch):
    monkeypatch.setattr(market, "PREMIUM_MIN_LISTINGS", 2)
    rows = [("X", "C", "2026-06-01", p, None, b, 5000) for b, p in
            [("b1", 100), ("b1", 100), ("b1", 100), ("b2", 200), ("b2", 200), ("b2", 200),
             ("b4", 100), ("b4", 100)]]  # b4: only 2 deals, too few to match
    deals = pd.DataFrame(rows, columns=["district", "rvd_class", "agreement_date", "psf_hkd",
                                        "exclude_reason", "building_id", "area_sqft"])
    li = pd.DataFrame([("sale", "X", 5000, 550_000, "b1"),    # ask 110 vs 100
                       ("sale", "X", 5000, 1_100_000, "b2"),  # ask 220 vs 200
                       ("sale", "X", 5000, 5_000_000, "b3"),  # no deals: not used
                       ("sale", "X", 5000, 5_000_000, "b4")],  # too few deals: not used
                      columns=["kind", "district", "area_sqft", "price_hkd", "building_id"])
    li["rvd_class"] = "C"
    r = _row(market.summary(_raw(), deals=deals, listings=li), "X", "C")
    assert r.n_premium == 2 and r.ask_premium == pytest.approx(0.10)
    # the naive comparison (all asks vs all deals, mixing different flats) is far higher
    assert r.median_ask_psf / r.median_deal_psf_6m - 1 > 0.5
    monkeypatch.setattr(market, "PREMIUM_MIN_LISTINGS", 3)
    r = _row(market.summary(_raw(), deals=deals, listings=li), "X", "C")
    assert r.n_premium == 2 and np.isnan(r.ask_premium)  # too few listings: blank


def test_ask_premium_compares_with_deals_of_similar_size(monkeypatch):
    monkeypatch.setattr(market, "PREMIUM_MIN_LISTINGS", 1)
    # one building: small flats sell at 200 psf, a big one at 100 psf
    rows = [("X", "C", "2026-06-01", p, None, "b1", a) for p, a in
            [(20000, 500), (20000, 520), (20000, 480), (10000, 1500), (10000, 1500),
             (10000, 1500)]]
    deals = pd.DataFrame(rows, columns=["district", "rvd_class", "agreement_date", "psf_hkd",
                                        "exclude_reason", "building_id", "area_sqft"])
    li = pd.DataFrame([("sale", "X", 500, 11_000_000, "b1"),    # ask 22,000 vs similar 20,000
                       ("sale", "X", 1500, 16_500_000, "b1"),   # ask 11,000 vs similar 10,000
                       ("sale", "X", 900, 9_900_000, "b1")],    # ask 11,000: no 3 similar deals
                      columns=["kind", "district", "area_sqft", "price_hkd", "building_id"])
    li["rvd_class"] = "C"
    ratios = market._listing_ratios(li, market._clean_deals({}, deals), pd.Timestamp("2026-06-30"))
    # the last one falls back to all sizes: median 15,000
    assert list(ratios["ratio"]) == pytest.approx([1.10, 1.10, 11_000 / 15_000])
    assert list(ratios["similar_size"]) == [True, True, False]


def test_premium_uses_similar_size_listings_only(monkeypatch):
    monkeypatch.setattr(market, "PREMIUM_MIN_LISTINGS", 2)
    rows = [("X", "C", "2026-06-01", 20000, None, "b1", a) for a in (500, 510, 520)]
    rows += [("X", "C", "2026-06-01", 10000, None, "b2", a) for a in (1500, 1500, 1500)]
    deals = pd.DataFrame(rows, columns=["district", "rvd_class", "agreement_date", "psf_hkd",
                                        "exclude_reason", "building_id", "area_sqft"])
    li = pd.DataFrame([("sale", "X", 500, 11_000_000, "b1"),   # similar: ratio 1.10
                       ("sale", "X", 500, 11_000_000, "b1"),   # similar: ratio 1.10
                       ("sale", "X", 900, 30_000_000, "b1")],  # fallback: ratio 1.67, left out
                      columns=["kind", "district", "area_sqft", "price_hkd", "building_id"])
    li["rvd_class"] = "C"
    r = _row(market.summary(_raw(), deals=deals, listings=li), "X", "C")
    assert r.n_premium == 2 and r.ask_premium == pytest.approx(0.10)
    monkeypatch.setattr(market, "PREMIUM_MIN_LISTINGS", 3)  # only 2 similar-size: blank
    r = _row(market.summary(_raw(), deals=deals, listings=li), "X", "C")
    assert r.n_premium == 2 and np.isnan(r.ask_premium)


def test_index_columns_are_labelled_territory_wide(table):
    assert "territory" in table.attrs["index_scope"].lower()


def test_all_sizes_and_territory_rows(table):
    assert _row(table, "X", "ALL").n_sales_12m == 7
    t = _row(table, market.TERRITORY, "ALL")
    assert t.n_sales_12m == 8 and t.sale_listings == 4  # X's 7 and Y's 1
    assert _row(table, "X", "A").n_sales_12m == 0
    assert np.isnan(_row(table, "X", "A").median_psf_12m)
    assert table.iloc[0].district == market.TERRITORY


def test_data_date_is_latest_clean_deal(table):
    assert table.attrs["data_to"] == pd.Timestamp("2026-06-30")
    assert table.attrs["three_month_change_ends"] == pd.Timestamp("2026-05-19")


def test_trend_monthly_series():
    t = market.trend(_raw(), "X", months=12, deals=_deals())
    assert list(t.columns[:3]) == ["month", "median_psf", "n_sales"]
    assert t["month"].iloc[-1] == pd.Timestamp("2026-06-01")
    assert len(t) == 12
    mar = t[t.month == "2026-03-01"].iloc[0]
    assert mar.median_psf == 170 and mar.n_sales == 1
    assert t[t.month == "2026-05-01"].iloc[0].n_sales == 0  # empty month stays in the series
    assert bool(t["partial"].iloc[-1]) and not bool(t["partial"].iloc[0])
