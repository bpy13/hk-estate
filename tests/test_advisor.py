"""Decision helper (Task 18): two contrasting profiles on a small fixture, hard limits,
unknown data, rent intent, and the loaders for the saved files."""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hkestate import advisor, commute, rent

FIX = Path(__file__).parent / "fixtures" / "advisor"
TODAY = date(2026, 10, 23)

# Three stations: Admiralty (ADM) and Wan Chai (WAC) one stop apart on the Island line,
# Tuen Mun (TUM) far away on the Tuen Ma line, joined to Admiralty by a 21-stop line (the
# in-between stations have no coordinates, so each stop costs the 2-minute fallback).
STATIONS_CSV = (FIX / "mtr_stations.csv").read_text("utf-8")
_HEADER = ('"Line Code","Direction","Station Code","Station ID","Chinese Name",'
           '"English Name","Sequence"\n')


def _rows(line, stops):
    return "".join(f'"{line}","DT","{c}","0","x","{c}",{i:.2f}\n' for i, c in enumerate(stops))


LINES_CSV = (_HEADER + _rows("ISL", ["WAC", "ADM"])
             + _rows("TML", ["TUM"] + [f"X{i}" for i in range(20)] + ["ADM"]))

BUILDINGS = pd.DataFrame({
    "estate_key": ["E-harbour", "E-sunny", "E-mid"],
    "estate_name": ["Harbour Court", "Sunny Garden", "Mid Tower"],
    "building_name": ["Tower 1", "Block 2", "Mid Tower"],
    "district": ["灣仔區", "屯門區", "灣仔區"],
    "lat": [22.2790, 22.3952, np.nan], "lon": [114.1650, 113.9735, np.nan],
    "mtr_m": [300.0, 1200.0, np.nan],
    "built_year": [2001, 2016, 1996], "estate_units": [800, 3000, 200],
    "school_net": ["12", "70", None],
}, index=pd.Index(["b-harbour", "b-sunny", "b-mid"], name="building_id"))


def _sale(key, building, area, rooms, asking, fair, confidence="normal", exclude=None,
          rel_floor=0.5):
    b = BUILDINGS.loc[building]
    return {"key": key, "url": f"https://example.test/{key}", "source": "centanet",
            "listing_id": key, "building_id": building, "estate_key": b["estate_key"],
            "estate_name": b["estate_name"], "building_name": b["building_name"],
            "district": b["district"], "area_sqft": float(area), "rooms": rooms,
            "age_years": float(TODAY.year - b["built_year"]), "lat": b["lat"], "lon": b["lon"],
            "mtr_m": b["mtr_m"], "rel_floor": rel_floor, "estate_units": b["estate_units"],
            "special_unit": False, "facing": "unknown", "sea_view": False,
            "school_net": b["school_net"], "floor": "10/F", "flat": "A",
            "asking_hkd": float(asking), "fair_price_hkd": float(fair),
            "discount_pct": 1 - asking * 0.95 / fair, "confidence": confidence,
            "exclude_reason": exclude}


SALE = pd.DataFrame([
    # a small, cheap flat beside Admiralty, well below fair price: the investor's pick
    _sale("L1", "b-harbour", 450, 2, 8_000_000, 9_500_000),
    # a big family flat in Tuen Mun, net 70, 45 minutes from Admiralty
    _sale("L2", "b-sunny", 900, 3, 7_000_000, 7_000_000),
    # no coordinates, no net, low confidence: unknown commute, net and discount
    _sale("L3", "b-mid", 600, 3, 6_500_000, 7_200_000, confidence="low"),
    # a data check failed: never recommended
    _sale("L4", "b-harbour", 5500, 5, 5_000_000, 30_000_000, exclude="check_area"),
])


def _rvd():
    months = pd.date_range("2026-01-01", "2026-08-01", freq="MS")
    rows = []
    for m in months:
        for g in ("ABC", "DE", "ALL"):
            rows.append(("rent", "rvd_rent_index", "month", m, g, "ALL", 100.0))
        for c in "ABCDE":
            for r in ("HK", "KLN", "NT"):
                rows.append(("rent", "rvd_avg_rent_psm", "month", m, c, r, 400.0))
    return pd.DataFrame(rows, columns=["kind", "series", "freq", "period", "class", "region",
                                       "value"])


def _rent_rows(building, psf, n, area=500.0, kind="rent"):
    b = BUILDINGS.loc[building]
    return [{"source": "centanet", "listing_id": f"R-{building}-{i}", "building_id": building,
             "estate_key": b["estate_key"], "district": b["district"], "kind": kind,
             "status": "active", "price_hkd": psf * area, "saleable_sqft": area,
             "rooms": 2, "age_years": None, "month": pd.Timestamp("2026-08-01")}
            for i in range(n)]


def _rent_model():
    raw = pd.DataFrame(_rent_rows("b-harbour", 40.0, 5) + _rent_rows("b-sunny", 20.0, 5))
    li = rent.clean_rent_listings(raw).assign(month=pd.Timestamp("2026-08-01"))
    return rent.RentModel.from_frames(li, BUILDINGS[["estate_key", "district"]], _rvd())


def _rent_listings():
    # the harbour flat asks 20% under its building's level; the sunny one 50% over
    rows = (_rent_rows("b-harbour", 40.0, 5) + _rent_rows("b-sunny", 20.0, 5)
            + [{**_rent_rows("b-harbour", 32.0, 1)[0], "listing_id": "CHEAP"},
               {**_rent_rows("b-sunny", 30.0, 1)[0], "listing_id": "DEAR"}])
    raw = {"listings": pd.DataFrame(rows)}
    return advisor.rent_candidates(raw, BUILDINGS)


@pytest.fixture
def data():
    stations, names = advisor.read_stations(STATIONS_CSV)
    graph = commute.build_graph(LINES_CSV, {c: (la, lo) for c, la, lo in stations})
    districts, effects = advisor.growth_tables(FIX / "growth", "all")
    return advisor.AdvisorData(sale=SALE, rent_listings=_rent_listings(), buildings=BUILDINGS,
                               district_growth=districts, effects=effects, stations=stations,
                               station_names=names, graph=graph, rent_model=_rent_model(),
                               today=TODAY, growth_setting="all")


INVESTOR = advisor.Profile(intent="buy", weights={"investment": 1}, budget_hkd=10_000_000)
FAMILY = advisor.Profile(intent="buy", weights={"near_place": 1, "school": 1, "space": 1},
                         budget_hkd=10_000_000, min_sqft=600, min_rooms=3, stations=("ADM",))


# ---- two contrasting profiles ---------------------------------------------------------------


def test_investor_and_family_rank_differently(data):
    inv = advisor.recommend(INVESTOR, data)
    fam = advisor.recommend(FAMILY, data)
    # investor: L1 (20% below fair, 5%/yr district, near MTR) > L2 (5% below, 2%/yr) > L3
    # (district 5%/yr but low confidence, so its discount counts 0)
    assert list(inv.listings["key"]) == ["L1", "L2", "L3"]
    # family: L1 is under 600 sq ft; L2 is big and reachable, L3's commute is unknown
    assert list(fam.listings["key"]) == ["L2", "L3"]
    assert list(inv.estates["estate_name"]) == ["Harbour Court", "Sunny Garden", "Mid Tower"]
    assert list(fam.estates["estate_name"]) == ["Sunny Garden", "Mid Tower"]
    assert "L4" not in set(inv.listings["key"])  # data check reason


def test_investor_scores_are_explainable(data):
    r = advisor.recommend(INVESTOR, data).listings.set_index("key")
    l1 = r.loc["L1"]
    # district 5%/yr -> 62.5; mtr_near robust +1.0 pp -> 75; yield 40x450x12/8m = 2.7% -> 45;
    # discount 1 - 8m x 0.95 / 9.5m = 20% -> 100 (capped): mean 70.6
    assert l1["score_investment"] == pytest.approx((62.5 + 75 + 45 + 100) / 4, abs=0.1)
    assert l1["overall"] == pytest.approx(l1["score_investment"])
    assert "district grew 5.0%/yr" in l1["reason"]
    assert "mtr near +1.0" in l1["reason"]
    assert "rent yield 2.7%" in l1["reason"]
    assert "20% below fair price" in l1["reason"]
    # the low-confidence flat's discount is unknown and counts as 0, never as good
    assert "discount unknown (low confidence)" in r.loc["L3", "reason"]
    assert r.loc["L3", "score_investment"] < l1["score_investment"]


def test_family_scores_are_explainable(data):
    r = advisor.recommend(FAMILY, data).listings.set_index("key")
    l2 = r.loc["L2"]
    assert 42 <= l2["commute_min"] <= 45  # TUM -> ADM: 21 stops at 2 min + a short walk
    assert l2["reason"].startswith(f"{l2['commute_min']:.0f} min to Admiralty")
    # 900 sq ft = 1.5x the 600 minimum -> size 75; 10 years old -> 80: 0.75x75 + 0.25x80
    assert l2["score_space"] == pytest.approx(0.75 * 75 + 0.25 * 80)
    assert "900 sq ft (1.5x your minimum), 3 rooms, 10 years old" in l2["reason"]
    assert np.isnan(l2["score_school"]) and "no net chosen" in l2["reason"]
    # three equal weights, school unknown counts 0
    assert l2["overall"] == pytest.approx((l2["score_near_place"] + l2["score_space"]) / 3)


# ---- hard limits ------------------------------------------------------------------------------


def test_budget_is_a_hard_limit(data):
    r = advisor.recommend(advisor.Profile(weights={"investment": 1}, budget_hkd=6_800_000),
                          data)
    assert list(r.listings["key"]) == ["L3"]
    assert r.left_out["budget"] == 2


def test_income_runs_the_mortgage_test(data):
    # 70% loan on 8m = 5.6m at 3.5% over 30 years is HK$25,147 a month: a 40,000 income
    # gives DSR 63% (cap 50%) so L1 and L2 fail; 6.5m -> 20,432 -> 51%: fails too
    tight = advisor.Profile(weights={"investment": 1}, income_hkd=40_000, rate=0.035)
    assert advisor.recommend(tight, data).listings.empty
    ok = advisor.Profile(weights={"investment": 1}, income_hkd=60_000, rate=0.035)
    assert len(advisor.recommend(ok, data).listings) == 3


def test_size_and_rooms_limits(data):
    p = advisor.Profile(weights={"space": 1}, min_sqft=500, min_rooms=3)
    r = advisor.recommend(p, data)
    assert set(r.listings["key"]) == {"L2", "L3"}
    assert r.left_out == {"budget": 0, "size": 1, "rooms": 0}


def test_commute_limit_counts_unknown_out_unless_allowed(data):
    p = advisor.Profile(weights={"near_place": 1}, stations=("ADM",), max_commute_min=30)
    r = advisor.recommend(p, data)
    assert list(r.listings["key"]) == ["L1"]  # L2 too far, L3 has no coordinates
    assert r.left_out["commute"] == 2
    p2 = advisor.Profile(weights={"near_place": 1}, stations=("ADM",), max_commute_min=30,
                         allow_unknown=True)
    r2 = advisor.recommend(p2, data)
    assert list(r2.listings["key"]) == ["L1", "L3"]
    l3 = r2.listings.set_index("key").loc["L3"]
    assert np.isnan(l3["score_near_place"]) and l3["overall"] == 0
    assert l3["reason"] == "commute unknown (no coordinates)"


def test_school_net_limit(data):
    p = advisor.Profile(weights={"school": 1}, school_net="70")
    r = advisor.recommend(p, data)
    assert list(r.listings["key"]) == ["L2"]
    assert r.listings.iloc[0]["reason"] == "net 70"
    p2 = advisor.Profile(weights={"school": 1}, school_net="12", allow_unknown=True)
    r2 = advisor.recommend(p2, data).listings.set_index("key")
    assert r2.loc["L1", "score_school"] == 100 and r2.loc["L3", "reason"] == "school net unknown"
    assert np.isnan(r2.loc["L3", "score_school"])


def test_nothing_passes_gives_an_empty_result_with_a_note(data):
    r = advisor.recommend(advisor.Profile(budget_hkd=1_000_000), data)
    assert r.listings.empty and r.estates.empty
    assert "no listing passes the hard limits" in r.notes


# ---- rent intent, other intents, profile checks --------------------------------------------


def test_rent_intent_ranks_by_fit_to_the_rent_estimate(data):
    p = advisor.Profile(intent="rent", weights={"investment": 1}, budget_hkd=30_000)
    r = advisor.recommend(p, data).listings
    assert r.iloc[0]["listing_id"] == "CHEAP"  # 20% under the building's level -> 100
    assert r.iloc[0]["score_investment"] == 100
    assert "20% below, building level" in r.iloc[0]["reason"]
    assert r.iloc[-1]["listing_id"] == "DEAR"  # 50% over -> 0
    assert r.iloc[-1]["score_investment"] == 0
    assert (r["price_hkd"] <= 30_000).all()


def test_rent_intent_uses_the_same_living_goals(data):
    p = advisor.Profile(intent="rent", weights={"near_place": 1}, stations=("WAC",),
                        max_commute_min=20)
    r = advisor.recommend(p, data).listings
    assert set(r["building_id"]) == {"b-harbour"}
    assert r.iloc[0]["reason"].endswith("min to Wan Chai")


def test_sell_and_let_have_no_listing_search(data):
    r = advisor.recommend(advisor.Profile(intent="let"), data)
    assert r.listings.empty and "sell-vs-let" in r.notes[0]


def test_profile_checks():
    with pytest.raises(ValueError):
        advisor.Profile(intent="swap")
    with pytest.raises(ValueError):
        advisor.Profile(weights={"views": 1})
    with pytest.raises(ValueError):
        advisor.Profile(weights={"space": -1})
    assert advisor.Profile(weights={}).weights == {g: 1.0 for g in advisor.GOALS}
    with pytest.raises(ValueError, match="unknown MTR station"):
        advisor.recommend(advisor.Profile(stations=("ZZZ",)), _data_without_rent())


def _data_without_rent():
    stations, names = advisor.read_stations(STATIONS_CSV)
    return advisor.AdvisorData(sale=SALE, rent_listings=pd.DataFrame(), buildings=BUILDINGS,
                               district_growth=pd.DataFrame(), effects=pd.DataFrame(),
                               stations=stations, station_names=names,
                               graph=commute.build_graph(LINES_CSV), today=TODAY)


def test_missing_growth_and_rent_data_read_as_unknown():
    r = advisor.recommend(INVESTOR, _data_without_rent()).listings.set_index("key")
    l1 = r.loc["L1"]
    assert "district growth unknown" in l1["reason"] and "rent yield unknown" in l1["reason"]
    assert l1["score_investment"] == pytest.approx((0 + 50 + 0 + 100) / 4)


# ---- loaders ----------------------------------------------------------------------------------


def test_growth_tables_take_recorded_pairs_only():
    districts, effects = advisor.growth_tables(FIX / "growth", "all")
    assert districts.loc["灣仔區", "growth"] == pytest.approx(0.05)
    assert len(districts) == 2 and effects.loc["mtr_near", "coef"] == pytest.approx(0.01)


def test_read_stations_skips_rows_without_coordinates():
    rows, names = advisor.read_stations(STATIONS_CSV + "NOP,無,Nowhere,,\n")
    assert [r[0] for r in rows] == ["ADM", "WAC", "TUM"]
    assert names["ADM"] == "Admiralty"
