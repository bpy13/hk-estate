"""Tasks 5.3-5.4 end to end on the small synthetic database: `hkestate report` builds pairs,
runs the regressions and writes data/reports/findings_<date>.md with its charts."""

from datetime import date, timedelta

import pytest
from phase4_data import add_deal, make_db

from hkestate import cli, db, features, model, study

TODAY = date(2026, 10, 17)


def _add_history(con):
    """Repeat sales: 300 flats bought 2010-2016 and resold 2019-2026 (held 3+ years), and RVD
    average rent / price series for the yields."""
    import random

    rng = random.Random(5)
    for i in range(300):
        b = ["cd-1-AAA", "cd-1-BBB", "h28-1-b1"][i % 3]
        floor, flat = 2 + i % 28, "ABCDEFGH"[i % 8]
        area = 300.0 if flat in "EF" else 600.0
        d0 = date(2010, 1, 1) + timedelta(days=rng.randint(0, 2500))
        d1 = date(2019, 6, 1) + timedelta(days=rng.randint(0, 2400))
        p0 = int(area * 8000 * rng.uniform(0.95, 1.05))
        growth = 1.03 + (0.01 if floor > 20 else 0)
        p1 = int(p0 * growth ** ((d1 - d0).days / 365.25) * rng.uniform(0.97, 1.03))
        add_deal(con, f"r{i}a", b, floor, f"R{flat}", d0, p0, area=area, source="centadata")
        add_deal(con, f"r{i}b", b, floor, f"R{flat}", d1, p1, area=area)
        con.execute("INSERT OR IGNORE INTO unit (unit_id, building_id, floor_label, floor_num, "
                    "floor_kind, flat) VALUES (?, ?, ?, ?, 'number', ?)",
                    [db.unit_id(b, str(floor), f"R{flat}"), b, str(floor), floor, f"R{flat}"])
    # a flat last sold in 2008: the start of an estimated pair
    add_deal(con, "old_z", "cd-1-AAA", 5, "Z", date(2008, 3, 1), 3_000_000, area=600.0,
             source="centadata")
    con.execute("INSERT OR IGNORE INTO unit (unit_id, building_id, floor_label, floor_num, "
                "floor_kind, flat) VALUES ('cd-1-AAA|5|Z', 'cd-1-AAA', '5', 5, 'number', 'Z')")
    rows = []
    d = date(2008, 1, 1)
    while d <= date(2026, 8, 1):
        for cls in "ABCDE":
            for region in ("HK", "KLN", "NT"):
                rows.append(("rvd_avg_rent_psm", d, cls, region, 300.0, "rent_index"))
                rows.append(("rvd_avg_price_psm", d, cls, region, 110_000.0, "price_index"))
        d = (d.replace(day=28) + timedelta(days=5)).replace(day=1)
    for series, period, cls, region, value, table in rows:
        con.execute(f"INSERT INTO {table} (series, freq, period, class, region, value) "
                    "VALUES (?, 'month', ?, ?, ?, ?)", [series, period, cls, region, value])


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    path = make_db(tmp_path / "hkestate.duckdb", n_deals=600)
    con = db.connect(path)
    _add_history(con)
    raw = features.extract(con)
    con.close()
    features.save_snapshot(raw)
    model.train(raw, rounds=80, today=TODAY, log=lambda *_: None)
    return tmp_path, raw


def test_study_builds_every_pair_type_and_verdicts(data):
    _, raw = data
    import pandas as pd

    tp = pd.read_parquet(model.model_dir() / "test_predictions.parquet")
    s = study.run(raw, model.load(), tp, log=lambda *_: None)
    real = s.pairs["real"]
    assert len(real) >= 200  # some synthetic flats share a unit or a bulk day
    assert (real["years"] >= 3).all()
    assert "cd-1-AAA|5|Z" in set(s.pairs["estimated"]["unit_id"])
    assert {"net_ann_growth", "rental_yield", "total_return"} <= set(real.columns)
    assert (real["net_ann_growth"] < real["ann_growth"]).all()
    assert real["rental_yield"].between(0.02, 0.06).all()
    v = s.verdicts["recorded"]
    assert "floor_high" in v.index
    assert v.loc["floor_high", "coef"] > 0
    assert {"robust", "time_holds", "district_holds"} <= set(v.columns)


def test_hkestate_report_writes_the_findings_file(data):
    tmp_path, _ = data
    assert cli.main(["report", "--from-snapshot"]) == 0
    out = tmp_path / "reports" / f"findings_{date.today():%Y%m%d}.md"
    text = out.read_text("utf-8")
    assert text.index("Summary in plain language") < text.index("## Data coverage")
    for heading in ("PRELIMINARY", "## Verdicts by attribute", "## First-hand vs second-hand",
                    "## Limits", "## Growth, total return and drawdown by attribute"):
        assert heading in text
    assert (tmp_path / "reports" / f"findings_{date.today():%Y%m%d}" / "effects.svg").exists()
    with db.session(tmp_path / "hkestate.duckdb") as con:
        status = con.execute("SELECT status FROM run_log WHERE command = 'report'").fetchone()[0]
    assert status == "ok"

