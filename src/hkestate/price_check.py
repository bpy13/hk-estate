"""Check a price: what the fair-price model says a flat was (or is) worth on a date, the
normal range around that, and whether a given price sat below, within or above it.

Reads only what `advisor.load_data` loads (the saved model and snapshot); never the database.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from hkestate import advisor, model

# The saved model trained on the sales it is asked about, so it fits them too well. On 18,966
# recent sales (scratchpad check, 2026-10-23, docs/plans/2026-10-23-check-a-price.md) the
# median gap between price and fair price was 6.4% for a model that never saw them and 5.0%
# for the saved model: a ratio of 0.78, the same for every building size. So the gap shown for
# a sale the model learned from is about 0.78 of the real one; multiply by 1/0.78 = 1.25.
IN_SAMPLE_GAP_FACTOR = 1.25

BELOW, WITHIN, ABOVE = ("below the normal range", "within the normal range",
                        "above the normal range")


def _newest_trained_deal(meta: dict) -> pd.Timestamp | None:
    end = (meta.get("split") or {}).get("end")
    return pd.Timestamp(end) if end else None


def check(building_id: str, area_sqft: float, on: date, data: advisor.AdvisorData, *,
          price_hkd: float | None = None, floor_text: str | None = None,
          rooms: float | None = None, facing: str | None = None,
          sea_view: bool = False) -> dict:
    """The fair price of a flat on `on`, its normal range and, when `price_hkd` is given, the
    gap and a verdict. Gaps are fractions (0.05 = 5% above fair). `learned`: the saved model
    trained on sales up to its newest deal, so for a date up to then the gap is corrected by
    IN_SAMPLE_GAP_FACTOR before it is judged against the range. When the flat cannot be
    valued, `fair_hkd` is NaN, the range and verdict are empty and `basis` says why."""
    fair, basis = advisor.fair_value(building_id, area_sqft, data, floor_text, rooms, on=on,
                                     facing=facing, sea_view=sea_view)
    out = {"fair_hkd": fair, "low_hkd": np.nan, "high_hkd": np.nan, "gap_pct": None,
           "learned": False, "gap_corrected_pct": None, "verdict": None, "basis": basis}
    if not pd.notna(fair):
        return out
    meta = data.fair_model.meta
    is_pilot = data.buildings["is_pilot"].get(building_id, False) \
        if "is_pilot" in data.buildings else False
    q = model.q_for(meta["q"], "pilot" if bool(is_pilot) else "territory")
    out["low_hkd"], out["high_hkd"] = fair * float(np.exp(-q)), fair * float(np.exp(q))
    newest = _newest_trained_deal(meta)
    out["learned"] = newest is not None and pd.Timestamp(on) <= newest
    if price_hkd is None or not price_hkd > 0:
        return out
    gap = float(price_hkd) / fair - 1
    corrected = gap * IN_SAMPLE_GAP_FACTOR if out["learned"] else gap
    out["gap_pct"], out["gap_corrected_pct"] = gap, corrected
    out["verdict"] = BELOW if corrected < np.exp(-q) - 1 else \
        ABOVE if corrected > np.exp(q) - 1 else WITHIN
    return out
