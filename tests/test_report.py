"""Task 8: the findings report's attribute x setting reading, without a database. The
end-to-end report on the synthetic snapshot is in test_study.py."""

import re
from datetime import date
from types import SimpleNamespace

import pandas as pd
from test_study import data  # noqa: F401  (fixture: a trained synthetic snapshot)

from hkestate import charts, cli, report


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
    assert "No market phase had enough flats sold twice" in report.phase_reading(
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


# ---- Task 12: "pairs" is not a word the owner relays ---------------------------------------------

CAVEAT = ("For flats whose last sale was before 2018, we compare that sale with the model's "
          "estimate of the flat's value today. The model learned partly from that same old "
          "sale, so its estimate leans back towards the old price. This hides part of any real "
          "difference between kinds of flat: when this check shows 'no effect', trust it less "
          "than the result from flats sold twice.")
SUFFIX = "(this check tends to understate differences; see How it was done)"


def _readable(text: str) -> str:
    """The text a person reads: backticked names (settings, files, columns) and link targets
    are code, so they may keep the word."""
    text = re.sub(r"`[^`]*`", "", text)
    return re.sub(r"\]\([^)]*\)", "]", text)


def _pair_words(text: str) -> list[str]:
    return re.findall(r".{0,40}\bpairs?\b.{0,40}", _readable(text), flags=re.I)


def test_plain_words_replaces_pair_in_study_labels_and_notes():
    assert report.plain_words("All data (every pair, 1995 on)") == \
        "All data (every flat sold twice, 1995 on)"
    assert report.plain_words("Too few recorded pairs in this window (1; a regression needs "
                              "200): no verdicts. Pairs are held 3+ years") == \
        ("Too few flats sold twice in this window (1; a regression needs 200): no verdicts. "
         "Flats sold twice are held 3+ years")
    assert report.plain_words("adds 5 recorded pairs held 1-3 years") == \
        "adds 5 flats sold twice held 1-3 years"
    assert report.plain_words("12 estimated-pair candidates in buildings") == \
        "12 model-valued flat candidates in buildings"
    assert report.plain_words("n Ricacorp pairs") == "n Ricacorp reported gains"
    assert report.plain_words("`pairs.py` stays") == "`pairs.py` stays"


def test_type_names_use_the_new_words():
    assert not _pair_words(" ".join(report.TYPE_NAMES.values()))
    assert "Flats sold twice" in report.TYPE_NAMES["recorded"]
    assert "Model-valued flats" in report.TYPE_NAMES["estimated"]
    assert "Ricacorp reported gains" in report.TYPE_NAMES["ricacorp"]


def test_crosscheck_summary_line_carries_the_understatement_suffix():
    v = pd.DataFrame({"coef": [0.01], "robust": [True]}, index=["floor_high"])
    s = SimpleNamespace(verdicts={"recorded": v, "estimated": v})
    line = report.crosscheck_line(s)
    assert line.endswith(SUFFIX)
    assert "model-valued" in line and not _pair_words(line)
    assert report.crosscheck_line(SimpleNamespace(verdicts={"recorded": v})) is None


def test_phase_reading_has_no_pair_word():
    assert not _pair_words(report.phase_reading(
        "sea_view", {"all": c(-0.003, significant=False), "crash-1997-2003": None}))
    cells = {"all": c(0.022, True), "boom-2008-2021": c(0.015, True)}
    assert not _pair_words(report.phase_reading("size_xl", cells))


def test_dashboard_labels_use_the_new_words():
    from hkestate import dashboard_data as dd

    assert not _pair_words(" ".join(dd.PAIRS_TYPES.values()))
    assert dd.PAIRS_TYPES["recorded"].startswith("Flats sold twice")
    assert dd.PAIRS_TYPES["estimated"].startswith("Model-valued flats")


def test_rendered_report_has_no_pair_word_and_the_caveat(data):  # noqa: F811
    """The report rendered from the synthetic snapshot with every standard setting: no
    "pair" in any case outside code (backticks), in the markdown or in the charts."""
    tmp_path, _ = data
    assert cli.main(["report", "--from-snapshot", "--settings", "standard"]) == 0
    out = tmp_path / "reports" / f"findings_{date.today():%Y%m%d}.md"
    text = out.read_text("utf-8")
    assert not _pair_words(text), _pair_words(text)[:5]
    for svg in (tmp_path / "reports" / out.stem).glob("*.svg"):
        assert not _pair_words(svg.read_text("utf-8")), svg.name
    assert CAVEAT in text
    assert "flats sold twice" in text and "sold twice" in text
    assert "Ricacorp reported gains" in text
