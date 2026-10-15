"""RVD spreadsheet parsing. Fixtures are trimmed rows of the real RVD sheets saved as CSV
(public statistics), so the parser sees the real column layout."""

import csv
from datetime import date
from pathlib import Path

import pytest

from hkestate import db
from hkestate.sources import rvd

FIX = Path(__file__).parent / "fixtures" / "rvd"


def rows(name):
    with open(FIX / name, encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def by_key(points):
    return {(p.period, p.cls, p.region): p for p in points}


def test_price_index_by_class_monthly():
    pts = by_key(rvd.parse_sheet(rows("price_index_monthly.csv"), rvd.FILES["price_index"]))
    aug = pts[(date(2026, 8, 1), "A", "ALL")]
    assert aug.value == pytest.approx(340.3)
    assert aug.provisional is True
    assert pts[(date(2026, 8, 1), "ALL", "ALL")].value == pytest.approx(320.5)
    assert pts[(date(2026, 8, 1), "DE", "ALL")].value == pytest.approx(288.4)
    jan = pts[(date(2026, 1, 1), "ALL", "ALL")]
    assert (jan.value, jan.provisional) == (pytest.approx(302.8), False)
    # 1993 rows at the top of the fixture carry their year forward month by month
    assert pts[(date(1993, 3, 1), "B", "ALL")].value == pytest.approx(83.9)
    assert {p.series for p in pts.values()} == {"rvd_price_index"}
    assert {p.freq for p in pts.values()} == {"month"}


def test_popular_developments_by_region():
    pts = by_key(rvd.parse_sheet(rows("popular_monthly.csv"), rvd.FILES["popular"]))
    assert pts[(date(2026, 8, 1), "ABC", "URBAN")].value == pytest.approx(273.3)
    assert pts[(date(2026, 8, 1), "DE", "NT")].value == pytest.approx(250.7)
    assert pts[(date(2026, 8, 1), "ALL", "ALL")].value == pytest.approx(280.4)


def test_average_rents_mark_fewer_than_20_deals():
    pts = by_key(rvd.parse_sheet(rows("avg_rents_monthly.csv"), rvd.FILES["avg_rent"]))
    feb = pts[(date(1999, 2, 1), "D", "KLN")]
    assert (feb.value, feb.few_deals) == (196.0, True)
    assert pts[(date(1999, 2, 1), "A", "HK")].few_deals is False
    assert pts[(date(1999, 1, 1), "E", "NT")].value == 191.0


def test_completions_annual():
    pts = by_key(rvd.parse_sheet(rows("completions_annual.csv"), rvd.FILES["completions"]))
    assert pts[(date(2024, 1, 1), "A", "ALL")].value == 10794
    assert pts[(date(2024, 1, 1), "ALL", "ALL")].value == 24261
    assert pts[(date(2026, 1, 1), "ALL", "ALL")].provisional is True
    assert {p.freq for p in pts.values()} == {"year"}


def test_layout_change_is_detected():
    sheet = rows("price_index_monthly.csv")
    for r in sheet:  # move the class header one column: the loader must refuse
        if len(r) > 7 and r[7] == "A":
            r[7], r[6] = "", "A"
    with pytest.raises(rvd.LayoutChanged):
        rvd.parse_sheet(sheet, rvd.FILES["price_index"])


def test_store_replaces_series(tmp_path):
    with db.session(tmp_path / "t.duckdb") as con:
        db.init_db(con)
        pts = rvd.parse_sheet(rows("price_index_monthly.csv"), rvd.FILES["price_index"])
        n1 = rvd.store(con, rvd.FILES["price_index"], pts, "https://x")
        n2 = rvd.store(con, rvd.FILES["price_index"], pts, "https://x")
        assert n1 == n2 == con.execute("SELECT count(*) FROM price_index").fetchone()[0]
