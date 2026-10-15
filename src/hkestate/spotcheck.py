"""Compare stored 28Hse deals with the live site (Task 1.5 spot-check).

Each sampled deal is looked up on its flat's live 28Hse page; floor and flat come from the
page title, area from the flat's details, price from the deal with the same date.
"""

from __future__ import annotations

from hkestate.normalise import parse_floor
from hkestate.sources.hse28 import UnitPage, split_location


def compare(row: dict, page: UnitPage) -> list[str]:
    """Differences between a stored deal and the flat page, as 'field: stored vs live'."""
    diffs = []
    _, floor_text, flat = split_location(page.title, "")
    live_floor = parse_floor(floor_text).label
    if live_floor != row["floor_label"]:
        diffs.append(f"floor: {row['floor_label']} vs {live_floor}")
    if flat != row["flat"]:
        diffs.append(f"flat: {row['flat']} vs {flat}")
    if (row.get("saleable_sqft") or None) != (page.saleable_sqft or None):
        diffs.append(f"area: {row.get('saleable_sqft')} vs {page.saleable_sqft}")
    same_day = [d for d in page.deals if d.deal_date == row["deal_date"]]
    if not same_day:
        diffs.insert(0, f"date: no deal on {row['deal_date']} (live: "
                        f"{[d.deal_date.isoformat() for d in page.deals]})")
    elif all(d.price_hkd != row["price_hkd"] for d in same_day):
        diffs.append(f"price: {row['price_hkd']} vs {[d.price_hkd for d in same_day]}")
    return diffs
