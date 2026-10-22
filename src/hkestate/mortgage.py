"""Mortgage rules: how much a bank may lend and whether a buyer's income covers it (Task 16).

Rules are the HKMA's countercyclical macroprudential measures for property mortgage loans,
kept as a dated table like the stamp duty tables in costs.py. Read 2026-10-23 on hkma.gov.hk.

Sources:
- [HKMA-2024-10] HKMA press release, "Countercyclical Macroprudential Measures for Property
  Mortgage Loans", 16 Oct 2024, https://www.hkma.gov.hk/eng/news-and-media/press-releases/2024/10/20241016-4/
  Effective 16 Oct 2024 for provisional sale and purchase agreements signed on or after that
  date (also uncompleted flats whose agreement was signed earlier but complete on or after it):
  maximum LTV 70% whatever the value or whether self-use, net-worth-based LTV 60% -> 70%,
  DSR limit 50% for self-use and non-self-use, the 10-point cut for applicants with other
  outstanding mortgages lifted.
- [HKMA-2024-02] HKMA press release and circular "Prudential Measures for Property Mortgage
  Loans and Other Related Supervisory Requirements", 28 Feb 2024,
  https://www.hkma.gov.hk/eng/news-and-media/press-releases/2024/02/20240228-3/ and the table
  https://www.hkma.gov.hk/media/eng/doc/other-information/FAQ_J1_Table_Eng.pdf (Tables 1-2,
  "with effect from 28 February 2024"): self-use LTV 70% up to HK$30m, 60%-70% from HK$30m to
  HK$35m subject to a HK$21m loan cap, 60% above HK$35m; net-worth-based 60%; 10 points lower
  for applicants who already have another outstanding mortgage; DSR 50% self-use, 40% with
  another mortgage; the 200-basis-point interest rate stress test suspended from that date.

Not modelled (one line each):
- Higher LTV through the HK Mortgage Corporation's Mortgage Insurance Programme (80%, 90% in
  limited cases; for the price bands, see https://www.hkmc.com.hk/eng/our_business/mortgage_insurance_programme.html,
  read 2026-10-23 via a summary, not confirmed against the page text).
- A December 2024 HKMA special scheme for buyers of uncompleted first-hand flats (reported
  80% LTV, 60% DSR); seen only in a search summary of an HKMA letter, so not entered.

Buyer types follow costs.py: "first" owns no other flat; "additional" already owns one with
an outstanding mortgage (the Feb 2024 rule only cut the limits for borrowers with an
outstanding mortgage; an owner with no mortgage is treated as "first" under that rule).
Salaried or not: the caps do not depend on it. "dsr" income basis is salary-style lending
(income covers the DSR); "net_worth" is lending on the applicant's assets (no DSR test).
Rules before 28 Feb 2024 are not entered (the earlier history was not read on HKMA pages);
`rules_on` raises for earlier dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

M = 1_000_000


@dataclass(frozen=True)
class Rules:
    effective: date
    source: str
    # (upper value limit or None, LTV, loan cap or None), for DSR-based lending
    ltv_bands: tuple
    net_worth_ltv: float
    existing_mortgage_cut: float  # points taken off every LTV when the buyer is "additional"
    dsr_cap: float
    dsr_cap_additional: float
    stress_addon: float  # rate rise added when testing DSR (0 = no stress test)


RULES = [
    Rules(date(2024, 2, 28), "HKMA-2024-02",
          ((30 * M, 0.70, None), (35 * M, 0.70, 21 * M), (None, 0.60, None)),
          0.60, 0.10, 0.50, 0.40, 0.0),
    Rules(date(2024, 10, 16), "HKMA-2024-10",
          ((None, 0.70, None),), 0.70, 0.0, 0.50, 0.50, 0.0),
]


def rules_on(on: date) -> Rules:
    """The rules for a provisional agreement signed on `on`."""
    found = None
    for r in RULES:
        if r.effective <= on:
            found = r
    if found is None:
        raise ValueError(f"no mortgage rules recorded before {RULES[0].effective}")
    return found


def _check_buyer(buyer: str) -> None:
    if buyer not in ("first", "additional"):
        raise ValueError(f"buyer must be 'first' or 'additional', got {buyer!r}")


def max_loan(price: float, valuation: float | None = None, buyer: str = "first",
             on: date | None = None, income_basis: str = "dsr") -> float:
    """Largest loan the rules allow. Banks lend a share of the lower of price and their
    valuation (valuation defaults to price)."""
    _check_buyer(buyer)
    r = rules_on(on or date.today())
    value = price if valuation is None else min(price, valuation)
    cut = r.existing_mortgage_cut if buyer == "additional" else 0.0
    if income_basis == "net_worth":
        return value * (r.net_worth_ltv - cut)
    if income_basis != "dsr":
        raise ValueError("income_basis must be 'dsr' or 'net_worth'")
    for upper, ltv, cap in r.ltv_bands:
        if upper is None or value <= upper:
            loan = value * (ltv - cut)
            return min(loan, cap) if cap is not None else loan
    raise AssertionError("unreachable")


def monthly_payment(loan: float, annual_rate: float, years: float) -> float:
    """Standard annuity payment (equal monthly instalments, interest charged monthly)."""
    n = round(years * 12)
    r = annual_rate / 12
    if r == 0:
        return loan / n
    return loan * r / (1 - (1 + r) ** -n)


@dataclass(frozen=True)
class Affordability:
    loan: float
    payment: float
    dsr: float
    dsr_cap: float
    passes: bool


def affordable(price: float, monthly_income: float, annual_rate: float, years: float,
               buyer: str = "first", on: date | None = None, valuation: float | None = None,
               other_monthly_debt: float = 0.0) -> Affordability:
    """Take the largest allowed loan and test the debt-servicing ratio (all monthly debt
    payments / monthly income) against the cap. Any stress-test rate rise in the rules is
    added to the rate for the test."""
    _check_buyer(buyer)
    on = on or date.today()
    r = rules_on(on)
    loan = max_loan(price, valuation, buyer, on)
    payment = monthly_payment(loan, annual_rate, years)
    tested = monthly_payment(loan, annual_rate + r.stress_addon, years)
    dsr = (tested + other_monthly_debt) / monthly_income
    cap = r.dsr_cap_additional if buyer == "additional" else r.dsr_cap
    return Affordability(loan, payment, dsr, cap, dsr <= cap)
