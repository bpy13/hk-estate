"""Mortgage rules (Task 16). Every expected number is worked by hand from the HKMA rules
cited in src/hkestate/mortgage.py."""

from datetime import date

import pytest

from hkestate import mortgage

M = 1_000_000


def test_rules_on_picks_the_regime_in_force():
    # 16 Oct 2024 relaxation applies from that day; the day before is the Feb 2024 regime
    assert mortgage.rules_on(date(2024, 10, 16)).effective == date(2024, 10, 16)
    assert mortgage.rules_on(date(2024, 10, 15)).effective == date(2024, 2, 28)
    assert mortgage.rules_on(date(2026, 10, 23)).effective == date(2024, 10, 16)
    assert mortgage.rules_on(date(2026, 10, 23)).dsr_cap == 0.50


def test_rules_before_first_recorded_date_raise():
    with pytest.raises(ValueError):
        mortgage.rules_on(date(2024, 2, 27))


@pytest.mark.parametrize("price, buyer, on, loan", [
    # Feb 2024 regime: 70% up to $30m -> 10m x 0.70 = 7m
    (10 * M, "first", date(2024, 6, 1), 7 * M),
    # owns another flat with a mortgage: 10 points lower -> 10m x 0.60 = 6m
    (10 * M, "additional", date(2024, 6, 1), 6 * M),
    # $30m-$35m band: 70% but loan cap $21m -> min(32m x 0.70 = 22.4m, 21m) = 21m
    (32 * M, "first", date(2024, 6, 1), 21 * M),
    # above $35m: 40m x 0.60 = 24m
    (40 * M, "first", date(2024, 6, 1), 24 * M),
    # from 16 Oct 2024 flat 70% whatever the value or buyer: 40m x 0.70 = 28m
    (40 * M, "first", date(2025, 1, 1), 28 * M),
    (10 * M, "additional", date(2025, 1, 1), 7 * M),
])
def test_max_loan_by_band_and_buyer(price, buyer, on, loan):
    assert mortgage.max_loan(price, buyer=buyer, on=on) == pytest.approx(loan)


def test_max_loan_uses_lower_of_price_and_valuation():
    # bank values the flat at 9m, price 10m: 9m x 0.70 = 6.3m
    assert mortgage.max_loan(10 * M, valuation=9 * M, buyer="first",
                             on=date(2025, 1, 1)) == pytest.approx(6.3 * M)
    # valuation above price does not help: 10m x 0.70 = 7m
    assert mortgage.max_loan(10 * M, valuation=12 * M, buyer="first",
                             on=date(2025, 1, 1)) == pytest.approx(7 * M)


def test_net_worth_basis_changed_with_the_rules():
    # net-worth-based lending (non-salaried): 60% until 15 Oct 2024 -> 6m; 70% after -> 7m
    assert mortgage.max_loan(10 * M, buyer="first", on=date(2024, 6, 1),
                             income_basis="net_worth") == pytest.approx(6 * M)
    assert mortgage.max_loan(10 * M, buyer="first", on=date(2025, 1, 1),
                             income_basis="net_worth") == pytest.approx(7 * M)


def test_monthly_payment_annuity():
    # 1,000,000 at 6% for 30 years: r = 0.005, n = 360, (1.005)^360 = 6.022575
    # payment = 1e6 x 0.005 x 6.022575 / (6.022575 - 1) = 5,995.51
    assert mortgage.monthly_payment(1_000_000, 0.06, 30) == pytest.approx(5995.51, abs=0.01)
    # zero rate: 1.2m over 10 years = 120 payments of 10,000
    assert mortgage.monthly_payment(1_200_000, 0.0, 10) == pytest.approx(10_000)


def test_affordable_pass_and_fail():
    # 10m flat, first home, 2025: loan 7m. At 4% for 30 years the payment factor is
    # 0.00477415 per dollar -> 7m x 0.00477415 = 33,419. Income 100,000: DSR 33.4% <= 50%.
    ok = mortgage.affordable(10 * M, 100_000, 0.04, 30, buyer="first", on=date(2025, 1, 1))
    assert ok.loan == pytest.approx(7 * M)
    assert ok.payment == pytest.approx(33_419, rel=1e-3)
    assert ok.dsr == pytest.approx(0.33419, rel=1e-3)
    assert ok.passes is True
    # Income 60,000: 33,419 / 60,000 = 55.7% > 50%
    bad = mortgage.affordable(10 * M, 60_000, 0.04, 30, buyer="first", on=date(2025, 1, 1))
    assert bad.dsr == pytest.approx(0.5570, rel=1e-3)
    assert bad.passes is False


def test_affordable_other_debt_counts_in_dsr():
    # 10,000 other monthly debt on 100,000 income adds 10 points: 43.4%
    r = mortgage.affordable(10 * M, 100_000, 0.04, 30, buyer="first", on=date(2025, 1, 1),
                            other_monthly_debt=10_000)
    assert r.dsr == pytest.approx(0.43419, rel=1e-3)
