# Plan 2026-10-23: "Check a price" page

**Goal.** A dashboard page where the owner picks a flat and a date (and optionally a price)
and gets the fair-price model's value at that date, its normal range, and whether the price
was below, within or above that range.

**Why.** The owner asked whether the app can judge any transaction, past or present. The
model already values any flat at any month (it learns building, estate, floor, facing, size
etc. and adjusts for the month by the price index); only a page is missing.

**Evidence behind the numbers (2026-10-23, scratchpad check on the saved model).** On 18,966
recent sales, the median gap between price and fair price was 6.4% for a model that never saw
them and 5.0% for the saved final model that trained on them (ratio 0.78, similar for every
building size). So for a sale the model learned from, the shown gap is about 0.78 of the real
one: multiply by 1.25. In-sample, 10% of sales sat more than 11% below fair and 10% more than
11% above.

**Constraints.** Direct instruction from the owner ("go"), 2026-10-23. Single user, private
Streamlit dashboard; no phone layout needed ("No phone app", decisions.md). The page reads the
saved model and snapshot only (`data/model/`), never the live database. Never store or show
personal data.

**Acceptance criteria.**
- C1. `advisor.fair_value` values a flat at any month (default today), unchanged for today.
- C2. A price check returns fair price, normal range, gap, gap corrected for sales the model
  learned from, and a plain verdict; tests check worked examples by hand.
- C3. A "Check a price" page (`?page=check`) works in the real dashboard on real data: pick a
  building, optionally a recorded sale of it, see the result.

## Phase 1 – Build (sonnet)

- [ ] **Task 1: value at a date, and the check**
  - Files: modify `src/hkestate/advisor.py` (`fair_value`); create `src/hkestate/price_check.py`;
    test `tests/test_advisor.py`, create `tests/test_price_check.py`.
  - Do (failing tests first):
    - `fair_value(..., on: date | None = None, facing: str | None = None,
      sea_view: bool = False)`: `on` sets `month_idx` (`features.month_index(on)`, but never
      later than the model's `valuation_month_idx`), the age (`on.year - built_year`) and the
      index move (deflator at `on` vs the model's reference month). `on=None` gives exactly
      today's result.
    - `price_check.check(building_id, area_sqft, on, data, *, price_hkd=None,
      floor_text=None, rooms=None, facing=None, sea_view=False) -> dict` with: `fair_hkd`,
      `low_hkd`, `high_hkd` (the normal range: the model's saved conformal interval,
      `model.q_for(meta["q"], group)`, as `fair * exp(-q)` and `fair * exp(q)`), `gap_pct`
      (price / fair - 1), `learned` (True when `on` is on or before the newest deal the saved
      model trained on), `gap_corrected_pct` (`gap_pct * IN_SAMPLE_GAP_FACTOR` when learned,
      else `gap_pct`), `verdict` ("below the normal range", "within the normal range", "above
      the normal range", judged on the corrected gap against the range) and `basis` text.
    - `IN_SAMPLE_GAP_FACTOR = 1.25`, with a comment citing this plan's evidence.
  - Done when: the new tests pass and `conda run -n hk-estate python -m pytest -q
    tests/test_advisor.py tests/test_price_check.py` is green.

- [ ] **Task 2: the page** (after Task 1)
  - Files: modify `app/dashboard.py` (new page and nav entry), `src/hkestate/dashboard_data.py`
    (data helpers only); test `tests/test_dashboard.py`.
  - Do: page "Check a price" (`?page=check`). Building search as on "Plan a move"
    (`dd.find_buildings`). Optional "pick a recorded sale" list from `data/model/deals.parquet`
    for that building (date, floor, flat, size, price; newest first), which fills the inputs.
    Inputs: date, saleable area, floor, price (optional). Output: fair price at that date,
    normal range, gap and verdict; when `learned`, one line saying the gap is corrected by
    1.25 because the model learned from sales of that period. Caption: estimates for research.
  - Done when: a Streamlit `AppTest` run on the real dashboard and real data (scratchpad
    script, as used for "Plan a move") picks a building and a recorded sale and shows a fair
    price and verdict with no exception; `pytest -q` and `ruff check .` pass.
