"""Decision helper (Tasks 18, 18b, 19): two contrasting profiles on a small fixture, hard
limits, unknown data, rent intent, rent-yield trust, the same flat listed twice, the loaders
for the saved files, and the buy-vs-rent / sell-vs-let arithmetic worked by hand."""
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from phase4_data import make_db

from hkestate import advisor, commute, db, features, model, mortgage, rent
from hkestate.sources import schools

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
                               today=TODAY, growth_setting="all",
                               district_index=advisor.growth_index(FIX / "growth", "all"))


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
    assert "rent yield 2.7% (building rents)" in l1["reason"]
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
    assert "20% below; 5 building listings of a similar size" in r.iloc[0]["reason"]
    assert r.iloc[-1]["listing_id"] == "DEAR"  # 50% over -> 0
    assert r.iloc[-1]["score_investment"] == 0
    assert (r["price_hkd"] <= 30_000).all()


def test_rent_intent_trusts_the_rent_and_compares_like_sizes(data):
    """Rent intent (changed 2026-10-23 after the pre-release review): the reference rent is
    the median rent per sq ft of the other listings at the estimate's level (building, estate
    or district) within +/-30% of the flat's area when 5 or more exist; a district reference
    is capped at the district's median rent per sq ft; RVD averages score unknown."""
    rows = (_rent_rows("b-harbour", 40.0, 5) + _rent_rows("b-harbour", 25.0, 5, area=1000.0)
            + [{**_rent_rows("b-harbour", 32.0, 1)[0], "listing_id": "CHEAP"},
               {**_rent_rows("b-harbour", 26.0, 1, area=1000.0)[0], "listing_id": "BIG"},
               {**_rent_rows("b-mid", 30.0, 1)[0], "listing_id": "MIDR"},
               {**_rent_rows("b-sunny", 30.0, 1)[0], "listing_id": "SUNNY"}])
    raw = pd.DataFrame(rows)
    li = rent.clean_rent_listings(raw).assign(month=pd.Timestamp("2026-08-01"))
    model_ =rent.RentModel.from_frames(li, BUILDINGS[["estate_key", "district"]], _rvd())
    d = replace(data, rent_listings=advisor.rent_candidates({"listings": raw}, BUILDINGS),
                rent_model=model_)
    p = advisor.Profile(intent="rent", weights={"investment": 1}, allow_unknown=True)
    r = advisor.recommend(p, d).listings.set_index("listing_id")
    # BIG (1,000 sq ft at HK$26) against the building's other 900-1,300 sq ft flats (HK$25),
    # not the whole building's median (HK$29, mostly small flats): 4% above -> 50 - 20 = 30
    big = r.loc["BIG"]
    assert big["score_investment"] == pytest.approx(30)
    assert "asking HK$26,000 vs HK$25,000 (4% above; 5 building listings of a similar size)" \
        in big["reason"]
    # CHEAP still 20% under its 500 sq ft neighbours (HK$40) -> 100
    assert r.loc["CHEAP", "score_investment"] == 100
    # MIDR (Mid Tower, no listings of its own) falls to 灣仔區: similar sizes say HK$40, but
    # a district reference is capped at the district median, HK$30 a sq ft: 0% -> 50
    midr = r.loc["MIDR"]
    assert midr["rent_basis"] == "district"
    assert midr["score_investment"] == pytest.approx(50)
    assert "capped at the district median" in midr["reason"]
    # SUNNY (屯門區, nothing near it) has only the RVD average: unknown, never good
    sunny = r.loc["SUNNY"]
    assert sunny["rent_basis"] == "rvd" and np.isnan(sunny["score_investment"])
    assert sunny["reason"] == "value unknown (no rent listings near this flat)"


def test_rent_intent_uses_the_same_living_goals(data):
    p = advisor.Profile(intent="rent", weights={"near_place": 1}, stations=("WAC",),
                        max_commute_min=20)
    r = advisor.recommend(p, data).listings
    assert set(r["building_id"]) == {"b-harbour"}
    assert r.iloc[0]["reason"].endswith("min to Wan Chai")


def test_sell_and_let_have_no_listing_search(data):
    r = advisor.recommend(advisor.Profile(intent="let"), data)
    assert r.listings.empty and "sell-vs-let" in r.notes[0]


# ---- Task 18b: rent-yield trust and the same flat listed twice ------------------------------


def test_yield_counts_by_how_near_the_rent_evidence_is(data):
    """Building or estate rents: the yield counts in full. District rents: the yield is
    pulled halfway to the district's median yield (growth study). RVD average only: the
    yield part is unknown (0)."""
    r = advisor.recommend(INVESTOR, data).listings.set_index("key")
    # L3 (b-mid, no rent listings in its building or estate) falls to 灣仔區's 5 listings at
    # HK$40/sq ft: 40 x 600 x 12 / 6.5m = 4.4%, pulled halfway to the district median 3.0%
    # -> 3.7%, then capped at the median itself: 3.0%, scored 3.0 / 6 x 100 = 50
    l3 = r.loc["L3"]
    assert "rent yield 4.4% (district rents; scored as 3.0%, capped at the district median " \
           "3.0%)" in l3["reason"]
    y3 = 0.03 * 100 / 6 * 100
    # a yield below the median is only pulled up halfway, never past the median
    row = pd.Series({"rent_est": 20 * 600, "rent_basis": "district", "price_hkd": 6_500_000})
    low_score, _ = advisor._yield_part(row, "灣仔區", data)
    assert low_score == pytest.approx((0.5 * (20 * 600 * 12 / 6_500_000) + 0.015) * 100 / 6 * 100)
    # district 5%/yr -> 62.5; no robust attribute -> 50; yield 61.9; discount unknown -> 0
    assert l3["score_investment"] == pytest.approx((62.5 + 50 + y3 + 0) / 4, abs=0.1)
    # with rent listings only in Harbour Court, Sunny Garden's flat (屯門區, no listings)
    # gets an RVD-average estimate: no yield credit
    raw = pd.DataFrame(_rent_rows("b-harbour", 40.0, 5))
    li = rent.clean_rent_listings(raw).assign(month=pd.Timestamp("2026-08-01"))
    only_harbour = rent.RentModel.from_frames(li, BUILDINGS[["estate_key", "district"]], _rvd())
    r2 = advisor.recommend(INVESTOR, replace(data, rent_model=only_harbour)).listings
    l2 = r2.set_index("key").loc["L2"]
    assert l2["rent_basis"] == "rvd"
    assert "rent yield unknown (no rent listings near this flat)" in l2["reason"]
    # district 2%/yr -> 25; big estate / size flags: no robust effect -> 50; yield 0;
    # discount 1 - 0.95 = 5% -> 75
    assert l2["score_investment"] == pytest.approx((25 + 50 + 0 + 75) / 4, abs=0.1)


def test_same_flat_listed_twice_keeps_the_cheaper(data):
    """rank.py merges the same flat only when the two prices are within 2%; the helper also
    merges listings 2-5% apart, but only when they are truly one flat: same building, same
    flat letter (never blank), same floor when both floors are known (an exact floor, or a
    band), area within 2% and price within 5% of the cheaper. The ranked table's floor is
    usually a band or 'not given', so the same flat letter on different floors must not
    merge."""
    rows = [
        _sale("L5", "b-harbour", 452, 2, 8_300_000, 9_500_000),       # L1's flat, 3.75% dearer
        {**_sale("L13", "b-harbour", 450, 2, 8_200_000, 9_500_000),   # floor unknown: may be L1
         "floor": "not given"},
        {**_sale("L7", "b-harbour", 450, 2, 8_100_000, 9_500_000),    # another floor
         "floor": "11/F"},
        {**_sale("L8", "b-harbour", 450, 2, 8_050_000, 9_500_000),    # no flat letter
         "flat": ""},
        _sale("L14", "b-harbour", 450, 2, 8_600_000, 9_500_000),      # 7.5% dearer: kept
        # the same flat letter in different floor bands: two flats
        {**_sale("L11", "b-mid", 600, 3, 6_600_000, 7_200_000), "floor": "high band",
         "flat": "B"},
        {**_sale("L12", "b-mid", 600, 3, 6_700_000, 7_200_000), "floor": "low band",
         "flat": "B"},
    ]
    r = advisor.recommend(INVESTOR, replace(data, sale=pd.concat([SALE, pd.DataFrame(rows)])))
    keys = set(r.listings["key"])
    assert keys == {"L1", "L2", "L3", "L7", "L8", "L14", "L11", "L12"}
    assert r.listings.set_index("key").loc["L1", "also_listed"] == "L13, L5"
    assert "2 listings of a flat already listed cheaper merged into it" in r.notes
    # 10/F A at 8.0m and 11/F A at 8.1m stay apart even though area and price match
    assert pd.isna(r.listings.set_index("key").loc["L7", "also_listed"])
    # the same flat at a different size is another flat (L1 450 vs L4's 5,500 sq ft)
    assert r.listings.set_index("key").loc["L1", "score_space"] == \
        advisor.recommend(INVESTOR, data).listings.set_index("key").loc["L1", "score_space"]


# ---- Task 19: buy vs rent, sell vs let -------------------------------------------------------


def test_mortgage_interest_matches_a_month_by_month_loop():
    loan, rate, years = 5_600_000, 0.035, 10
    pmt = mortgage.monthly_payment(loan, rate, 30)
    bal, interest = loan, 0.0
    for _ in range(years * 12):
        i = bal * rate / 12
        interest += i
        bal -= pmt - i
    got = advisor.mortgage_interest(loan, rate, 30, years)
    assert got["payment"] == pytest.approx(pmt)
    assert got["interest"] == pytest.approx(interest, abs=1)
    assert got["balance"] == pytest.approx(bal, abs=1)
    # a 10-year loan over a 10-year horizon is repaid in full
    assert advisor.mortgage_interest(loan, rate, 10, 10)["balance"] == pytest.approx(0, abs=1)
    assert advisor.mortgage_interest(1_200_000, 0.0, 10, 5)["interest"] == 0


def test_buy_vs_rent_worked_by_hand():
    """Buy an 8m flat (first home, 2026-10-23) or rent it at HK$20,000 a month, 10 years,
    mortgage at 3.5%, prices growing 3%/yr, rent flat, spare cash earning 3%. Both sides are
    counted as wealth at the end of the 10 years (changed 2026-10-23 after the pre-release
    review: the renter now also invests what renting saves each month).
    Buying:  stamp duty 3% (2026 scale, 6-9m band) = 240,000; agent 1% = 80,000; legal 0.3%
             = 24,000; day-one cash = deposit 2.4m + 344,000 = 2,744,000. Loan 70% = 5.6m at
             3.5% over 30 years: HK$25,146.5 a month (interest in 120 months 1,753,485;
             balance left 4,335,905). Each month: the instalment plus rates, government rent
             and management, 18% of the rent = 3,600: 28,746.5. Sale 8m x 1.03^10 =
             10,751,331 less agent 1% 107,513, less the loan left: owner ends with 6,307,913.
    Renting: rent 20,000 a month; the renter invests the day-one cash, 2,744,000 x 1.03^10 =
             3,687,707, and each month the 8,746.5 that renting costs less than owning, at
             3% from the start of that month to the end: 1,222,690. Renter ends with
             4,910,397. Buying is 1,397,516 ahead (1,570,626 under the old count, which
             ignored the monthly saving)."""
    r = advisor.buy_vs_rent(8_000_000, 20_000, 10, 0.035, 0.03, buyer="first",
                            on=date(2026, 10, 23), rent_growth=0.0)
    assert r["stamp_duty"] == 240_000 and r["agent_buy"] == 80_000 and r["legal"] == 24_000
    assert r["loan"] == 5_600_000 and r["deposit"] == 2_400_000
    assert r["upfront_cash"] == 2_744_000
    assert r["monthly_payment"] == pytest.approx(25_146.5, abs=0.1)
    assert r["interest"] == pytest.approx(1_753_485, abs=1)
    assert r["loan_balance_at_end"] == pytest.approx(4_335_905, abs=1)
    mid = r["cases"]["mid"]
    assert mid["rates_gov_rent"] == pytest.approx(192_000)
    assert mid["management"] == pytest.approx(240_000)
    assert mid["sale_price"] == pytest.approx(10_751_331, abs=1)
    assert mid["owner_wealth"] == pytest.approx(6_307_913, abs=1)
    assert mid["upfront_grown"] == pytest.approx(3_687_707, abs=1)
    assert mid["renter_invests"] == pytest.approx(1_222_690, abs=1)
    assert mid["owner_invests"] == 0
    assert mid["renter_wealth"] == pytest.approx(4_910_397, abs=1)
    assert mid["difference"] == pytest.approx(1_397_516, abs=2)
    # the same in cost terms at the horizon: owning 1,398,322, renting 2,795,838
    assert mid["rent_cost"] - mid["buy_cost"] == pytest.approx(mid["difference"], abs=1e-3)
    assert mid["rent_cost"] == pytest.approx(2_795_838, abs=1)
    # a plain number gives one growth in all three cases; a range gives three
    assert r["cases"]["low"]["difference"] == r["cases"]["high"]["difference"]
    assert r["verdict"].startswith("buying comes out HK$1.4m ahead over 10 years")
    # when renting costs more than owning each month, the owner invests the difference
    dear = advisor.buy_vs_rent(8_000_000, 40_000, 10, 0.035, 0.03, on=date(2026, 10, 23),
                               rent_growth=0.0)["cases"]["mid"]
    assert dear["renter_invests"] == 0 and dear["owner_invests"] > 0
    three = advisor.buy_vs_rent(8_000_000, 20_000, 10, 0.035,
                                advisor.GrowthRange(0.0, 0.03, 0.06), on=date(2026, 10, 23))
    d = {k: v["difference"] for k, v in three["cases"].items()}
    assert d["low"] < d["mid"] < d["high"]
    # by default rent grows with prices, so the mid case pays more rent than the flat case
    assert three["cases"]["mid"]["rent_paid"] > 2_400_000
    assert "rates" in r["assumptions"] and "5%" in r["assumptions"]["rates"]


def test_sell_vs_let_worked_by_hand():
    """A flat worth 8m, rent HK$20,000, 4 years, prices 3%/yr, rent flat, cash earning 3%.
    Sell now: 8m less agent 1% = 7,920,000; grown 4 years at 3% = 8,914,030.
    Let: rent 960,000 gross over 2 tenancies; agent half a month each = 20,000; one month
         empty at the change = 20,000, so 940,000 received; rates 5% of 960,000 = 48,000;
         government rent 3% = 28,800; management 10% = 96,000; property tax 15% x 80% x
         (940,000 - 48,000) = 107,040. Net rent 640,160: by year 159,440, 169,440, 141,840
         (fee and empty month) and 169,440. Each year's net rent is cash in mid-year, grown
         at 3% to the end of year 4 so both paths are cash at the same date (changed
         2026-10-23 after the pre-release review): x 1.03^3.5, ^2.5, ^1.5, ^0.5 = 176,818 +
         182,435 + 148,270 + 171,963 = 679,487. Sale 8m x 1.03^4 = 9,004,071 less agent 1% =
         8,914,030. Letting is 679,487 ahead (640,160 under the old sum of unequal-date
         cash)."""
    r = advisor.sell_vs_let(8_000_000, 20_000, 4, 0.03, rent_growth=0.0)
    assert r["sell_now"] == 7_920_000 and r["tenancies"] == 2
    mid = r["cases"]["mid"]
    assert mid["sell_now_grown"] == pytest.approx(8_914_030, abs=1)
    assert mid["rent_received"] == 940_000 and mid["letting_fee"] == 20_000
    assert mid["rates_gov_rent"] == pytest.approx(76_800)
    assert mid["property_tax"] == pytest.approx(107_040)
    assert mid["net_rent"] == pytest.approx(640_160)
    assert mid["net_rent_grown"] == pytest.approx(679_487, abs=1)
    assert mid["sale_net"] == pytest.approx(8_914_030, abs=1)
    assert mid["let_cash"] == pytest.approx(679_487 + 8_914_030, abs=2)
    assert mid["difference"] == pytest.approx(679_487, abs=2)
    assert r["verdict"].startswith("letting comes out HK$0.7m ahead over 4 years")


def test_growth_range_from_the_study(data):
    """灣仔區's index rises 4%/yr for six years then stays flat: its 3-year windows have
    quartiles 0%, 2.0% and 4.0%. The mid case is the study's district median (5%); low and
    high shift it by the quartile spread: 5 - 2 = 3.0% and 5 + 2 = 7.0%."""
    g = advisor.growth_range("灣仔區", 3, data)
    assert g.mid == pytest.approx(0.05)
    assert g.low == pytest.approx(0.05 - 0.0198, abs=0.001)
    assert g.high == pytest.approx(0.05 + 0.04 - 0.0198, abs=0.001)
    assert "灣仔區 median" in g.basis and "3-year windows of the 灣仔區 index" in g.basis
    # 屯門區 has a median (2%) but no index of its own: the territory index (2%/yr flat)
    # gives a spread of zero
    t = advisor.growth_range("屯門區", 3, data)
    assert (t.low, t.mid, t.high) == pytest.approx((0.02, 0.02, 0.02))
    assert "the all index" in t.basis
    # a district the study has not seen: the territory index's own quartiles
    u = advisor.growth_range("離島區", 3, data)
    assert u.mid == pytest.approx(0.02, abs=0.001) and "windows of the all index" in u.basis
    # no study at all: a stated guess
    bare = replace(data, district_growth=pd.DataFrame(), district_index=pd.DataFrame())
    n = advisor.growth_range("灣仔區", 10, bare)
    assert (n.low, n.mid, n.high) == pytest.approx((0.0, 0.03, 0.06)) and "guess" in n.basis


def test_growth_range_takes_a_typed_rate_or_the_rise_since_a_date(data):
    """The page's growth box (owner's call 2026-10-23): a typed rate replaces the mid case;
    left empty, the mid case is the index's yearly rise since 1997. Low and high keep the
    same spread either way."""
    base = advisor.growth_range("灣仔區", 3, data)
    typed = advisor.growth_range("灣仔區", 3, data, mid=0.01)
    assert typed.mid == 0.01 and "your figure of 1.0%/yr" in typed.basis
    assert typed.high - typed.mid == pytest.approx(base.high - base.mid)
    s = data.district_index
    s = s[s["district"] == "灣仔區"].sort_values("month")
    span = (s["month"].iloc[-1] - s["month"].iloc[0]).days / 365.25
    expected = (s["level"].iloc[-1] / s["level"].iloc[0]) ** (1 / span) - 1
    since = advisor.growth_range("灣仔區", 3, data, since="1990-01-01")
    assert since.mid == pytest.approx(expected) and "灣仔區 index since 1990" in since.basis
    # nothing after the date: falls back to the long-run median
    late = advisor.growth_range("灣仔區", 3, data, since="2100-01-01")
    assert late.mid == pytest.approx(0.05)
    # the profile carries both into the listing cards
    p = replace(INVESTOR, growth=-0.02)
    l1 = advisor.recommend(p, data).listings.set_index("key").loc["L1"]
    own = advisor.buy_vs_rent(8_000_000, 18_000, 10, 0.035,
                              advisor.growth_range("灣仔區", 10, data, mid=-0.02), on=TODAY)
    assert l1["vs_rent_hkd"] == pytest.approx(own["cases"]["mid"]["difference"])


def test_buy_listings_carry_a_vs_renting_line(data):
    r = advisor.recommend(INVESTOR, data).listings.set_index("key")
    l1 = r.loc["L1"]
    # L1: rent estimate 40 x 450 = 18,000 a month; 10 years at the profile's 3.5%
    assert l1["vs_rent"].startswith("vs renting a similar flat (HK$18,000/month): buying comes")
    assert "growth 5.0%/yr" in l1["vs_rent"]
    assert np.isfinite(l1["vs_rent_hkd"])
    assert l1["vs_rent_low_hkd"] <= l1["vs_rent_hkd"] <= l1["vs_rent_high_hkd"]
    own = advisor.buy_vs_rent(8_000_000, 18_000, 10, 0.035,
                              advisor.growth_range("灣仔區", 10, data), on=TODAY)
    assert l1["vs_rent_hkd"] == pytest.approx(own["cases"]["mid"]["difference"])
    none = advisor.recommend(INVESTOR, _data_without_rent()).listings.iloc[0]
    assert none["vs_rent"] == "vs renting: unknown (no rent estimate)"
    assert np.isnan(none["vs_rent_hkd"])


def test_vs_renting_trusts_the_rent_like_the_score_does(data):
    """The same trust rule as the yield part of the score (changed 2026-10-23 after the
    pre-release review): a district-level rent is capped at the district's median yield x
    price, and an RVD-average rent gives no buy-vs-rent figures at all."""
    r = advisor.recommend(INVESTOR, data).listings.set_index("key")
    # L3: district rents say 40 x 600 = 24,000 a month on a 6.5m flat (4.4%); 灣仔區's
    # median yield is 3.0%, so the comparison uses 6.5m x 3% / 12 = 16,250
    l3 = r.loc["L3"]
    assert l3["vs_rent"].startswith("vs renting a similar flat (HK$16,250/month, capped at "
                                    "the district's median yield 3.0%)")
    capped = advisor.buy_vs_rent(6_500_000, 16_250, 10, 0.035,
                                 advisor.growth_range("灣仔區", 10, data), on=TODAY)
    assert l3["vs_rent_hkd"] == pytest.approx(capped["cases"]["mid"]["difference"])
    assert advisor.comparison_rent(24_000, "district", 6_500_000, "灣仔區", data)[0] == 16_250
    # a district rent below the median yield is used as it is; building rents always are
    assert advisor.comparison_rent(12_000, "district", 6_500_000, "灣仔區", data)[0] == 12_000
    assert advisor.comparison_rent(40_000, "building", 6_500_000, "灣仔區", data)[0] == 40_000
    # RVD average only: no figures
    raw = pd.DataFrame(_rent_rows("b-harbour", 40.0, 5))
    li = rent.clean_rent_listings(raw).assign(month=pd.Timestamp("2026-08-01"))
    only_harbour = rent.RentModel.from_frames(li, BUILDINGS[["estate_key", "district"]], _rvd())
    l2 = advisor.recommend(INVESTOR, replace(data, rent_model=only_harbour)).listings \
        .set_index("key").loc["L2"]
    assert l2["rent_basis"] == "rvd"
    assert l2["vs_rent"] == "vs renting: no reliable rent figure for this building"
    assert np.isnan(l2["vs_rent_hkd"])


def _price_index():
    rows = []
    for i, m in enumerate(pd.date_range("2026-01-01", "2026-10-01", freq="MS")):
        for cls in ("B", "ALL"):
            rows.append((cls, m, 100 * 1.01 ** i))
    return pd.DataFrame(rows, columns=["class", "period", "value"])


def test_fair_value_uses_the_model_like_rank_does(data):
    """A stand-in model that says HK$15,000/sq ft at its reference month (2026-08); the
    index has risen 1% a month since, so today's value is 15,000 x 1.01^2 x 500 sq ft."""
    fake = model.FairPrice(
        booster=SimpleNamespace(predict=lambda X: np.full(len(X), np.log(15_000))),
        meta={"categories": {c: ["b-harbour"] for c in model.CAT_FEATURES},
              "valuation_month_idx": 320, "index_reference_month": "2026-08-01"})
    d = replace(data, fair_model=fake, price_index=_price_index())
    value, basis = advisor.fair_value("b-harbour", 500, d, floor_text="18樓")
    assert value == pytest.approx(15_000 * 1.01 ** 2 * 500)
    assert basis == "fair-price model, floor exact"
    assert np.isnan(advisor.fair_value("nowhere", 500, d)[0])
    assert np.isnan(advisor.fair_value("b-harbour", 500, data)[0])  # no model loaded


def test_fair_value_at_a_past_date(data):
    """`on` moves the date effect, the age and the index. The stand-in model says
    15,000 x exp(0.01 x (month_idx - 320)); the index rises 1% a month from 2026-01. A flat of
    Harbour Court (built 2001) on 2026-05-15 (month 316; index month 4 against the reference
    month 7) is worth 15,000 x exp(-0.04) x 1.01^-3 x 500, aged 25. A date after the model's
    valuation month uses that month (320), and on=None is today's answer."""
    seen = []

    def predict(X):
        seen.append((int(X["month_idx"].iloc[0]), float(X["age_years"].iloc[0])))
        return np.log(15_000) + 0.01 * (X["month_idx"].to_numpy(dtype=float) - 320)

    fake = model.FairPrice(
        booster=SimpleNamespace(predict=predict),
        meta={"categories": {c: ["b-harbour"] for c in model.CAT_FEATURES},
              "valuation_month_idx": 320, "index_reference_month": "2026-08-01"})
    d = replace(data, fair_model=fake, price_index=_price_index())
    value, _ = advisor.fair_value("b-harbour", 500, d, on=date(2026, 5, 15))
    assert value == pytest.approx(15_000 * np.exp(-0.04) * 1.01 ** -3 * 500)
    assert seen[-1] == (316, 25.0)
    advisor.fair_value("b-harbour", 500, d, on=date(2026, 10, 1))  # later than month 320
    assert seen[-1][0] == 320
    today_value, _ = advisor.fair_value("b-harbour", 500, d)
    assert today_value == pytest.approx(15_000 * 1.01 ** 2 * 500)
    assert seen[-1] == (320, 25.0)
    # facing and sea view reach the model
    cap = []
    fake2 = model.FairPrice(
        booster=SimpleNamespace(predict=lambda X: cap.append(X.iloc[0]) or np.zeros(len(X))),
        meta={"categories": {c: ["b-harbour", "S"] if c == "facing" else ["b-harbour"]
                             for c in model.CAT_FEATURES},
              "valuation_month_idx": 320})
    advisor.fair_value("b-harbour", 500, replace(data, fair_model=fake2), facing="S",
                       sea_view=True)
    assert cap[0]["facing"] == "S" and bool(cap[0]["sea_view"]) is True


def test_advise_owner_gives_both_paths(data):
    """An owner of a 500 sq ft flat in Harbour Court worth 8m (given): the rent estimate is
    the building's HK$40/sq ft = 20,000, growth is 灣仔區's range over 4 years, and the
    sell-vs-let figures follow."""
    out = advisor.advise_owner("b-harbour", 500, data, advisor.Profile(intent="sell", years=4),
                               value_hkd=8_000_000)
    assert out["rent_hkd"] == pytest.approx(20_000) and out["rent_basis"] == "building"
    assert out["value_basis"] == "given" and out["district"] == "灣仔區"
    assert out["growth"]["mid"] == pytest.approx(0.05)
    s = out["sell_vs_let"]
    assert s["sell_now"] == 7_920_000 and set(s["cases"]) == {"low", "mid", "high"}
    assert s["cases"]["mid"]["rent_growth"] == pytest.approx(0.05)  # rent grows with prices
    # no model and no value given: no figures, a note says why
    none = advisor.advise_owner("b-harbour", 500, data)
    assert none["sell_vs_let"] is None and none["notes"] == ["no value: no fair-price model "
                                                              "loaded"]


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


def test_growth_index_reads_district_and_territory_series():
    idx = advisor.growth_index(FIX / "growth", "all")
    assert set(idx["district"]) == {"灣仔區", "all"}
    assert len(idx) == 290  # the estimated-pairs row is left out
    assert advisor.growth_index(FIX / "nowhere", "all").empty


def test_load_data_reads_stations_and_school_nets_from_the_snapshot(tmp_path):
    """Task 18b: the snapshot carries `stations.parquet` and `buildings.school_net`, so the
    helper needs no extra files (the CSV and Series arguments stay as overrides)."""
    path = make_db(tmp_path / "t.duckdb", n_deals=60)
    con = db.connect(path)
    con.execute("INSERT INTO mtr_station VALUES ('ADM', '金鐘', 'Admiralty', 22.2793, 114.1646)")
    con.execute("INSERT INTO mtr_station VALUES ('XXX', '無', 'No Coords', NULL, NULL)")
    schools.ensure_tables(con)
    con.execute("UPDATE master_building SET school_net = '72' WHERE building_id = 'cd-1-AAA'")
    raw = features.extract(con)
    con.close()
    snap = features.save_snapshot(raw, tmp_path / "snap")
    d = advisor.load_data(snapshot_dir=snap, ranked_path=tmp_path / "none.parquet",
                          growth_dir=tmp_path / "no-growth", model_dir=tmp_path / "no-model",
                          today=TODAY)
    assert d.stations == [("ADM", 22.2793, 114.1646)] and d.station_names == {"ADM": "Admiralty"}
    assert d.buildings.loc["cd-1-AAA", "school_net"] == "72"
    assert d.sale.empty and d.fair_model is None and d.district_index.empty
    assert "school_net" in d.rent_listings.columns
    # the overrides still win
    nets = pd.Series({"cd-1-BBB": "95"})
    d2 = advisor.load_data(snapshot_dir=snap, ranked_path=tmp_path / "none.parquet",
                           growth_dir=tmp_path / "no-growth", model_dir=tmp_path / "no-model",
                           stations_csv=FIX / "mtr_stations.csv", school_nets=nets, today=TODAY)
    assert [c for c, _, _ in d2.stations] == ["ADM", "WAC", "TUM"]
    assert d2.buildings.loc["cd-1-BBB", "school_net"] == "95"
    assert pd.isna(d2.buildings.loc["cd-1-AAA", "school_net"])


def test_load_data_builds_the_commute_graph_from_the_snapshot_lines(tmp_path):
    path = make_db(tmp_path / "t.duckdb", n_deals=60)
    con = db.connect(path)
    con.execute("INSERT INTO mtr_line_station VALUES ('TWL', 'UT', 'ADM', 1.0), "
                "('TWL', 'UT', 'TST', 2.0)")
    raw = features.extract(con)
    con.close()
    snap = features.save_snapshot(raw, tmp_path / "snap")
    d = advisor.load_data(snapshot_dir=snap, ranked_path=tmp_path / "none.parquet",
                          growth_dir=tmp_path / "no-growth", model_dir=tmp_path / "no-model",
                          today=TODAY)
    assert ("ADM", "TWL") in d.graph and ("TST", "TWL") in d.graph
