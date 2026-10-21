"""Task 8: the findings report's attribute x setting reading, without a database. The
end-to-end report on the synthetic snapshot is in test_study.py."""

import pandas as pd

from hkestate import charts, report


def c(coef, robust=False, significant=None):
    return {"coef": coef, "robust": robust,
            "significant": robust if significant is None else significant, "p": 0.01}


def test_cell_text_marks_the_verdict():
    assert report.cell_text(c(0.022, robust=True)) == "**+2.20**"
    assert report.cell_text(c(0.0061, significant=True)) == "+0.61"
    assert report.cell_text(c(0.0003, significant=False)) == "+0.03 n.s."
    assert report.cell_text(None) == "–"


def test_reading_holds_in_rising_and_falling_markets():
    cells = {"all": c(0.022, True), "crash-1997-2003": c(0.03, True),
             "recovery-2003-2008": c(0.02, True), "boom-2008-2021": c(0.015, True),
             "fall-2021-2024": None, "recent-2024-now": None}
    text = report.phase_reading("size_xl", cells)
    assert text.startswith("**Very large flats (1,076+ sq ft).** All data: +2.20 pp a year, "
                           "robust.")
    assert "every market phase tested" in text
    assert "rising and falling markets" in text
    assert "the 1997-2003 crash (+3.00)" in text
    assert "2021-2024 fall" not in text  # untested phases are not listed


def test_reading_only_in_some_phases_names_them():
    cells = {"all": c(0.006, significant=True), "crash-1997-2003": c(0.002, significant=False),
             "recovery-2003-2008": c(-0.004, True), "boom-2008-2021": c(0.009, True)}
    text = report.phase_reading("size_large", cells)
    assert "All data: +0.60 pp a year, significant but not robust." in text
    assert "Robust only in the 2008-2021 boom (+0.90)" in text
    assert "not significant in the 1997-2003 crash" in text
    assert "the other way in the 2003-2008 recovery (-0.40)" in text


def test_reading_with_no_phase_tested_and_weak_lean():
    assert "No market phase had enough pairs" in report.phase_reading(
        "sea_view", {"all": c(-0.003, significant=False), "crash-1997-2003": None})
    cells = {"all": c(-0.007, significant=True), "crash-1997-2003": c(-0.005, significant=True),
             "boom-2008-2021": c(-0.006, significant=True)}
    text = report.phase_reading("mtr_far", cells)
    assert "Points the same way (slower) in every phase tested" in text
    assert "weak" in text
    assert "Not significant in any phase tested" in report.phase_reading(
        "facing_south", {"all": c(0.0, significant=False),
                         "boom-2008-2021": c(0.001, significant=False)})


def test_worst_fall_clause_takes_its_period_from_the_districts():
    """Pre-release review 2026-10-21: the summary hard-coded "(1997-2003)" after the median
    district drawdown, wrong for a window that does not cover the crash."""
    d = pd.DataFrame({
        "district": ["東區", "南區", "北區", "沙田區"], "drawdown": [0.60, 0.50, 0.40, None],
        "peak": ["1997-10-01", "1997-08-01", "2021-09-01", None],
        "trough": ["2003-07-01", "2003-08-01", "2024-10-01", None]})
    assert report.worst_fall_clause(d) == \
        "; the worst fall was about 50% in a typical district (peak 1997, low 2003)"
    recent = d[d["district"] == "北區"]
    assert report.worst_fall_clause(recent) == \
        "; the worst fall was about 40% in a typical district (peak 2021, low 2024)"
    assert report.worst_fall_clause(d[d["drawdown"].isna()]) == ""


def test_dot_chart_legend_wraps_for_many_groups(tmp_path):
    groups = ["all", "since-2000", "crash-1997-2003", "recovery-2003-2008", "boom-2008-2021",
              "bought-after-crash"]
    rows = pd.DataFrame([{"label": "x", "group": g, "est": i, "lo": i - 1, "hi": i + 1}
                         for i, g in enumerate(groups)])
    path = charts.dot_ci_chart(rows, tmp_path / "e.svg", "t", groups)
    svg = path.read_text("utf-8")
    assert all(g in svg for g in groups)
    assert svg.count("<circle") == 2 * len(groups)  # one legend dot and one estimate each
