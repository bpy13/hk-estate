"""Price check: fair price at a date, its normal range, the gap and the verdict."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest
from test_advisor import data  # noqa: F401  (the shared fixture)

from hkestate import model, price_check

# The stand-in model says HK$15,000 a sq ft whatever the flat or date (no price index loaded),
# so a 500 sq ft flat is worth 7,500,000. Its normal range is +-0.1 in log price for the
# territory (0.05 for pilot estates); it trained on deals up to 2026-10-20.
FAIR = 7_500_000
LOW, HIGH = FAIR * np.exp(-0.1), FAIR * np.exp(0.1)


def _with_model(d, pilot: bool = False):
    fake = model.FairPrice(
        booster=SimpleNamespace(predict=lambda X: np.full(len(X), np.log(15_000))),
        meta={"categories": {c: ["b-harbour"] for c in model.CAT_FEATURES},
              "valuation_month_idx": 320, "index_reference_month": "2026-08-01",
              "q": {"all": 0.1, "territory": 0.1, "pilot": 0.05},
              "split": {"end": "2026-10-20"}})
    b = d.buildings.assign(is_pilot=pilot)
    return replace(d, fair_model=fake, buildings=b)


def test_range_and_verdict_for_a_sale_after_training(data):  # noqa: F811
    d = _with_model(data)
    r = price_check.check("b-harbour", 500, date(2026, 10, 23), d, price_hkd=8_000_000)
    assert r["fair_hkd"] == pytest.approx(FAIR)
    assert r["low_hkd"] == pytest.approx(LOW) and r["high_hkd"] == pytest.approx(HIGH)
    assert r["gap_pct"] == pytest.approx(8_000_000 / FAIR - 1)
    assert r["learned"] is False
    assert r["gap_corrected_pct"] == pytest.approx(r["gap_pct"])
    assert r["verdict"] == "within the normal range"
    assert price_check.check("b-harbour", 500, date(2026, 10, 23), d,
                             price_hkd=6_500_000)["verdict"] == "below the normal range"
    assert price_check.check("b-harbour", 500, date(2026, 10, 23), d,
                             price_hkd=8_400_000)["verdict"] == "above the normal range"


def test_gap_is_corrected_for_sales_the_model_learned_from(data):  # noqa: F811
    """6.8m against 7.5m is 9.33% below fair: inside the 9.52% lower edge. The model trained
    on March 2026 sales, so the gap is multiplied by 1.25 to 11.67% below: outside."""
    d = _with_model(data)
    r = price_check.check("b-harbour", 500, date(2026, 3, 1), d, price_hkd=6_800_000)
    assert r["learned"] is True
    assert r["gap_pct"] == pytest.approx(6.8 / 7.5 - 1)
    assert r["gap_corrected_pct"] == pytest.approx((6.8 / 7.5 - 1) * 1.25)
    assert price_check.IN_SAMPLE_GAP_FACTOR == 1.25
    assert r["verdict"] == "below the normal range"
    # the newest deal the model trained on counts as learned; the day after does not
    assert price_check.check("b-harbour", 500, date(2026, 10, 20), _with_model(data))["learned"]
    assert not price_check.check("b-harbour", 500, date(2026, 10, 21),
                                 _with_model(data))["learned"]


def test_pilot_estates_use_their_own_range(data):  # noqa: F811
    r = price_check.check("b-harbour", 500, date(2026, 10, 23), _with_model(data, pilot=True))
    assert r["low_hkd"] == pytest.approx(FAIR * np.exp(-0.05))


def test_no_price_gives_a_range_but_no_verdict(data):  # noqa: F811
    r = price_check.check("b-harbour", 500, date(2026, 10, 23), _with_model(data))
    assert r["fair_hkd"] == pytest.approx(FAIR)
    assert r["gap_pct"] is None and r["gap_corrected_pct"] is None and r["verdict"] is None
    assert r["basis"].startswith("fair-price model")


def test_a_flat_that_cannot_be_valued_says_why(data):  # noqa: F811
    r = price_check.check("nowhere", 500, date(2026, 10, 23), _with_model(data),
                          price_hkd=8_000_000)
    assert np.isnan(r["fair_hkd"]) and r["verdict"] is None
    assert r["basis"] == "building not in the snapshot"
