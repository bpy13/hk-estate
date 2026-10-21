"""`hkestate datacheck` on a small synthetic snapshot: every check, the report, the command."""

from datetime import date, datetime

import pandas as pd
import pytest

from hkestate import cli, datacheck

TODAY = date(2026, 10, 21)


def _deal(source, sid, unit, day, price, area=500.0, basis="saleable", building="cd-1-B",
          floor="10", flat="A", floor_num=10, reg=None, **flags):
    row = {
        "source": source, "source_id": sid, "fetched_at": datetime(2026, 10, 1),
        "unit_id": unit, "building_id": building, "estate_id": "cd-2-E",
        "floor_label": floor, "flat": flat, "deal_date": pd.Timestamp(day),
        "instrument_date": pd.Timestamp(day), "registration_date": pd.Timestamp(reg or day),
        "deal_type": "registry", "price_hkd": price, "area_sqft": area, "area_basis": basis,
        "psf_hkd": price / area if area and basis in ("saleable", "converted") else None,
        "gain_pct": None, "rooms": None, "floor_num": floor_num, "floor_kind": "number",
        "excl_duplicate": False, "excl_low_price": price < 500_000, "excl_first_hand": False,
        "excl_non_market": False, "excl_bulk": False, "excl_outlier": False, "parking": False,
        "cd_feature": None,
    }
    row.update(flags)
    return row


@pytest.fixture
def raw():
    deals = [
        # 2024: three clean sales, one duplicate copy already flagged
        _deal("centadata", "c1", "cd-1-B|10|A", "2024-03-01", 6_000_000),
        _deal("hse28", "h1", "cd-1-B|10|A", "2024-03-10", 6_000_000, excl_duplicate=True),
        _deal("centadata", "c2", "cd-1-B|11|A", "2024-05-01", 6_500_000, floor="11", floor_num=11),
        _deal("centadata", "c3", "cd-1-B|12|A", "2024-07-01", 7_000_000, floor="12", floor_num=12),
        # 2025: a duplicate pair that excl_duplicate missed (same unit, 1% price, 20 days apart)
        _deal("centadata", "c4", "cd-1-B|20|B", "2025-02-01", 8_000_000, floor="20", flat="B"),
        _deal("hse28", "h2", "cd-1-B|20|B", "2025-02-21", 8_050_000, floor="20", flat="B"),
        # 2025: the same unit sold again 200 days later at the same price: a real resale
        _deal("hse28", "h3", "cd-1-B|20|B", "2025-09-10", 8_050_000, floor="20", flat="B"),
        # odd values, 2026 (year to date)
        _deal("srpe", "s1", "cd-1-B|30|A", "2055-09-26", 35_000_000, floor="30",
              deal_type="first_hand", excl_first_hand=True),  # date in the future
        _deal("centadata", "c5", "cd-1-B|1|A", "1988-03-14", 3_700_000, floor="1", floor_num=1),
        _deal("centadata", "c6", "cd-1-B|2|A", "2026-01-05", 300_000, floor="2", floor_num=2),
        _deal("centadata", "c7", "cd-1-B|3|A", "2026-01-06", 600_000_000, area=4_000.0,
              floor="3", floor_num=3),  # price over 500m; psf 150,000
        _deal("centadata", "c8", "cd-1-B|4|A", "2026-01-07", 5_000_000, area=50.0, floor="4",
              floor_num=4),  # area under 100 -> psf 100,000 is at the limit, not over
        _deal("centadata", "c9", "cd-1-B|5|A", "2026-01-08", 5_000_000, area=6_000.0,
              floor="5", floor_num=5),  # area over 5,000; psf 833 under 1,000
        _deal("centadata", "c10", "cd-1-B|6|A", "2026-02-01", 5_000_000, floor="6", floor_num=6),
        _deal("centadata", "c11", "cd-1-B|6|A", "2026-02-01", 5_100_000, floor="6", floor_num=6),
        # no building, no area, a band floor (coverage)
        _deal("hse28", "h4", None, "2026-03-01", 5_000_000, area=None, basis=None,
              building=None, floor="高層", floor_num=None),
        # a Ricacorp derived previous deal: not a recorded sale
        _deal("ricacorp", "r1:prev", "cd-1-B|7|A", "2010-01-01", 2_000_000, floor="7",
              floor_num=7, deal_type="unknown"),
        # 1997 and 2005, no registry file for 1997
        _deal("centadata", "c12", "cd-1-B|8|A", "1997-06-01", 4_000_000, floor="8", floor_num=8),
        _deal("centadata", "c13", "cd-1-B|9|A", "2005-06-01", 3_000_000, floor="9", floor_num=9),
    ]
    buildings = pd.DataFrame([{"building_id": "cd-1-B", "estate_name": "測試苑",
                               "building_name": "1座"}])
    return {"deals": pd.DataFrame(deals), "buildings": buildings,
            "meta": {"extracted_at": "2026-10-21T16:59:17", "quarantined_rows": 0}}


REGISTRY = {
    2005: dict.fromkeys(range(1, 13), 1),       # 12 a year; ours 1 -> 8%: flagged
    2024: {m: 0 for m in range(1, 13)} | {1: 3},  # 3; ours 3 -> 100%: ok
    2025: {m: 0 for m in range(1, 13)} | {1: 2},  # 2; ours 3 -> 150%: flagged
    2026: {1: 2, 2: 2, 3: 1},                    # published to March: compared to March
}


def test_parse_consideration_json_reads_the_total_row_with_commas_and_gaps():
    text = ('﻿[{"Range of Consideration ($ million)":"Less than 1","Jan":"27","Feb":"10"},'
            '{"Range of Consideration ($ million)":"Total","Jan":"3,250","Feb":"2,021",'
            '"Mar":"-","Apr":""}]')
    assert datacheck.parse_consideration_json(text) == {1: 3250, 2: 2021}
    with pytest.raises(ValueError):
        datacheck.parse_consideration_json('[{"x":"Less than 1","Jan":"1"}]')


def test_land_registry_counts_skips_a_failed_year():
    class F:
        def get(self, url, page_type):
            assert page_type == "static"
            if "2024" in url:
                return '[{"r":"Total","Jan":"10","Feb":"5"}]'
            raise RuntimeError("404")

    messages = []
    got = datacheck.land_registry_counts(F(), [2004, 2024, 2025], log=messages.append)
    assert got == {2024: {1: 10, 2: 5}}
    assert len(messages) == 1 and "2025" in messages[0]


def test_yearly_check_flags_low_and_high_years_and_compares_the_current_year_to_date(raw):
    d = datacheck.prepare(raw["deals"])
    y = datacheck.yearly_check(d, REGISTRY, TODAY).set_index("year")
    assert y.loc[1997, "verdict"] == "no independent count" and y.loc[1997, "clean"] == 1
    assert y.loc[2010, "verdict"] == "registry file missing"  # 2010 is in the registry's range
    assert y.loc[2005, "verdict"] == "look at this"  # 1 of 12
    assert (y.loc[2024, "stored"], y.loc[2024, "clean"]) == (3, 3)  # the flagged copy not counted
    assert y.loc[2024, "verdict"] == "ok"
    assert y.loc[2025, "verdict"] == "look at this"  # 3 of 2
    # 2026: registry published to March, so the sales after March are left out: c6-c9 and the
    # same-day pair (Jan-Feb) count, h4 (March) counts, the 2055 SRPE row and 1988 row don't
    assert y.loc[2026, "note"] == "to Mar"
    assert y.loc[2026, "stored"] == 7 and y.loc[2026, "clean"] == 6  # c6 is under HK$500k
    assert y.loc[2026, "verdict"] == "look at this"  # 6 of 5
    assert y.index.min() == 1995 and y.index.max() == 2026  # 1988 and 2055 are not listed


def test_duplicates_check_finds_the_pair_excl_duplicate_missed(raw):
    d = datacheck.prepare(raw["deals"])
    res = datacheck.duplicates_check(d)
    assert res["pairs"] == 1 and res["by_pair"] == {"centadata-hse28": 1}
    assert res["by_year"].loc[2025, "centadata-hse28"] == 1
    ex = res["examples"].iloc[0]
    assert {ex["id_a"], ex["id_b"]} == {"c4", "h2"}  # h3 is 200 days later: a real resale
    assert res["verdict"] == "look at this"  # 1 of 17 stored sales is far over 0.5%


def test_odd_values_check_counts_each_rule_by_source(raw):
    d = datacheck.prepare(raw["deals"])
    res = datacheck.odd_values_check(d, TODAY)
    r = res["rules"]
    assert r["date in the future"]["by_source"] == {"srpe": 1}
    assert r["date before 1990"]["rows"] == 1
    assert r["price under HK$500k"]["rows"] == 1
    assert r["price over HK$500m"]["rows"] == 1
    assert r["area under 100 sq ft"]["rows"] == 1
    assert r["area over 5,000 sq ft"]["rows"] == 1
    assert r["price per sq ft under 1,000"]["rows"] == 2  # c9 at 833 and the HK$300k sale at 600
    assert r["price per sq ft over 100,000"]["rows"] == 1  # c7 at 150,000; c8 is exactly 100,000
    same = r["two sales of one unit on one day"]
    assert same["rows"] == 2 and same["units"] == 1
    # the verdict is about odd rows that are still in the clean data: not the HK$300k sale
    # (excl_low_price) nor the 2055 SRPE row (first-hand)
    assert r["price under HK$500k"]["clean"] == 0
    assert r["date in the future"]["clean"] == 0
    assert r["price per sq ft under 1,000"]["clean"] == 1  # c9 only
    assert res["clean"] == 8 and res["verdict"] == "look at this"  # c5, c7 x2, c8, c9 x2, c10, c11
    assert set(r["date in the future"]["examples"]["source_id"]) == {"s1"}


def test_coverage_check_by_decade(raw):
    d = datacheck.prepare(raw["deals"])
    cov = datacheck.coverage_check(d).set_index("decade")
    assert cov.loc[2020, "sales"] == 13  # 3 (2024) + 3 (2025) + 7 (2026, h4 included)
    assert cov.loc[2020, "building"] == pytest.approx(12 / 13)
    assert cov.loc[2020, "area"] == pytest.approx(12 / 13)
    assert cov.loc[2020, "floor"] == pytest.approx(12 / 13)  # 高層 is a band, not a floor
    assert cov.loc[2050, "sales"] == 1  # the 2055 SRPE row shows up as its own decade
    assert list(cov.index) == [1980, 1990, 2000, 2020, 2050]
    assert (cov["verdict"] == "too few to judge").all()  # under 1,000 sales a decade
    by_src = datacheck.coverage_by_source(d).set_index("source")
    assert by_src.loc["hse28", "sales"] == 3
    assert by_src.loc["hse28", "floor"] == pytest.approx(2 / 3)
    assert by_src.loc["centadata", "building"] == 1.0
    assert datacheck.unparsed_floor_labels(d).to_dict() == {"高層": 1}


def test_run_writes_the_report_and_returns_the_numbers(raw, tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    res = datacheck.run(raw, REGISTRY, today=TODAY, log=lambda m: None)
    assert res["verdicts"] == {"sales per year": "look at this", "duplicates left": "look at this",
                               "odd values": "look at this", "coverage": "ok"}
    assert res["flagged_years"] == [2005, 2025, 2026]
    assert (res["deals"], res["stored"], res["clean"]) == (19, 17, 15)
    path = datacheck.write_report(res, raw)
    assert path == tmp_path / "reports" / "datacheck_20261021.md"
    text = path.read_text("utf-8")
    assert "| sales per year | **look at this** |" in text
    assert "| 2026 (to Mar) | 7 | 6 | 5 | 120% | look at this |" in text
    assert "測試苑 1座" in text  # examples name the building in Chinese
    assert "nan" not in text  # a missing building or area is never printed as NaN
    assert "centadata-hse28 1" in text
    assert "| hse28 | 3 | 66.7% | 66.7% | 66.7% |" in text
    assert "no floor number" in text and "高層 (1)" in text
    # without the registry every year is shown without a verdict
    res2 = datacheck.run(raw, None, today=TODAY, log=lambda m: None)
    assert res2["verdicts"]["sales per year"] == "no independent count"
    assert "not read in this run" in datacheck.write_report(res2, raw).read_text("utf-8")


def test_datacheck_command_reads_the_snapshot_and_never_the_database(raw, tmp_path, monkeypatch):
    from hkestate import features

    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(features, "load_snapshot", lambda *a, **k: raw)

    def no_db(*a, **k):
        raise AssertionError("the database must not be opened")

    monkeypatch.setattr(features, "snapshot", no_db)
    assert cli.main(["datacheck", "--no-fetch"]) == 0
    assert list((tmp_path / "reports").glob("datacheck_*.md"))
    assert not (tmp_path / "hkestate.duckdb").exists()
