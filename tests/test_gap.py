"""Task 4.3: asking-vs-deal gap. Pairs of a listing and a deal on the same unit within 90
days, counted per district; 30+ pairs: the district's median gap, else 5% marked assumed."""

import pandas as pd
import pytest

from hkestate import gap


def _listing(lid, *, building="b1", flat="A", floor_text="12樓", band="mid", price=10_000_000,
             area=500.0, first="2026-01-01", last="2026-02-01", district="東區"):
    return {"source": "centanet", "listing_id": lid, "kind": "sale", "building_id": building,
            "flat": flat, "floor_text": floor_text, "floor_band": band, "price_hkd": price,
            "saleable_sqft": area, "first_seen": pd.Timestamp(first),
            "last_seen": pd.Timestamp(last), "district": district}


def _deal(sid, *, building="b1", flat="A", floor="12", rel=0.5, price=9_500_000, area=500.0,
          day="2026-03-01", district="東區"):
    return {"source": "hse28", "source_id": sid, "building_id": building, "flat": flat,
            "floor_label": floor, "rel_floor": rel, "price_hkd": price, "area_sqft": area,
            "agreement_date": pd.Timestamp(day), "district": district, "exclude_reason": None}


def test_pairs_same_unit_within_90_days_of_last_seen():
    listings = pd.DataFrame([_listing("L1"), _listing("L2", flat="B"),
                             _listing("L3", flat="C")])
    deals = pd.DataFrame([
        _deal("D1", day="2026-03-01"),                 # 28 days after L1 was last seen
        _deal("D2", flat="B", day="2026-06-01"),       # 120 days after: too late
        _deal("D3", flat="C", floor="30", day="2026-01-20"),  # another floor
    ])
    p = gap.pairs(listings, deals)
    assert list(p["listing_id"]) == ["L1"]
    assert p.iloc[0]["gap"] == pytest.approx(0.05)  # asked 10.0m, sold 9.5m


def test_band_listing_pairs_on_band_flat_and_area_within_2pct():
    listings = pd.DataFrame([
        _listing("L1", floor_text="高層", band="high", area=500.0),
        _listing("L2", floor_text=None, band="high", flat="B", area=500.0),
        _listing("L3", floor_text=None, band="unknown", flat="C"),
    ])
    deals = pd.DataFrame([
        _deal("D1", floor="25", rel=0.85, area=505.0),  # high floor, area +1%
        _deal("D2", flat="B", floor="25", rel=0.85, area=530.0),  # area +6%: another flat type
        _deal("D3", flat="C", floor="5", rel=0.15),  # listing without a floor band: no pair
    ])
    p = gap.pairs(listings, deals)
    assert list(p["listing_id"]) == ["L1"]


def test_each_deal_pairs_once_with_the_nearest_listing():
    listings = pd.DataFrame([_listing("L1", last="2026-01-15"), _listing("L2", last="2026-02-20")])
    deals = pd.DataFrame([_deal("D1", day="2026-03-01")])
    p = gap.pairs(listings, deals)
    assert list(p["listing_id"]) == ["L2"]


def test_district_gap_needs_30_pairs_else_5pct_assumed():
    rows = [{"district": "東區", "gap": 0.03}] * 30 + [{"district": "元朗區", "gap": 0.10}] * 29
    g = gap.district_gaps(pd.DataFrame(rows)).set_index("district")
    assert g.loc["東區", "gap"] == pytest.approx(0.03)
    assert g.loc["東區", "basis"] == "measured"
    assert g.loc["元朗區", "gap"] == pytest.approx(0.05)
    assert g.loc["元朗區", "basis"] == "assumed"
    assert g.loc["元朗區", "n_pairs"] == 29
    assert gap.gap_for(g.reset_index(), "沙田區") == (0.05, "assumed")
    assert gap.gap_for(g.reset_index(), "東區") == (pytest.approx(0.03), "measured")


def test_no_pairs_at_all_gives_assumed_default():
    g = gap.district_gaps(pd.DataFrame(columns=["district", "gap"]))
    assert g.empty
    assert gap.gap_for(g, "東區") == (0.05, "assumed")
