"""Spot-check: a stored deal is compared field by field with its live per-flat page."""

from datetime import date
from pathlib import Path

from hkestate import spotcheck
from hkestate.sources import hse28

FIX = Path(__file__).parent / "fixtures" / "hse28"


def unit_page():
    return hse28.parse_unit_page((FIX / "unit_page.html").read_text(encoding="utf-8"))


def row(**kw):
    base = {
        "floor_label": "30",
        "flat": "C",
        "deal_date": date(2019, 4, 2),
        "price_hkd": 12_500_000,
        "saleable_sqft": 703.0,
    }
    base.update(kw)
    return base


def test_matching_row_has_no_differences():
    assert spotcheck.compare(row(), unit_page()) == []


def test_each_field_is_compared():
    diffs = spotcheck.compare(
        row(floor_label="31", flat="D", price_hkd=12_600_000, saleable_sqft=700.0), unit_page()
    )
    assert {d.split(":")[0] for d in diffs} == {"floor", "flat", "price", "area"}


def test_missing_deal_date_is_reported():
    diffs = spotcheck.compare(row(deal_date=date(2019, 4, 3)), unit_page())
    assert diffs and diffs[0].startswith("date")


def test_ground_floor_labels_compare_equal():
    page = unit_page()
    page.title = "太古城 安盛台 建安閣 地下 C室"
    assert spotcheck.compare(row(floor_label="G"), page) == []
