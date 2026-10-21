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



def test_window_drops_pairs_outside_and_adapts_the_time_split(data):
    """Task 6: `--start/--end` keep a pair only when both sales are inside; the time split
    then falls inside the window (median purchase year) instead of the fixed 2018 cut."""
    import re

    import pandas as pd

    from hkestate import growth

    _, raw = data
    tp = pd.read_parquet(model.model_dir() / "test_predictions.parquet")
    m = model.load()
    base = study.prepare(raw, m, tp, log=lambda *_: None)
    full = study.analyse(base)
    s = study.analyse(base, start="2012-01-01", end="2024-12-31")
    real = s.pairs["real"]
    assert 0 < len(real) < len(full.pairs["real"])
    assert (real["start_date"] >= pd.Timestamp("2012-01-01")).all()
    assert (real["end_date"] <= pd.Timestamp("2024-12-31")).all()
    assert s.pairs["estimated"].empty  # valued today, which is after the window
    assert not full.pairs["estimated"].empty
    assert s.window == ("2012-01-01", "2024-12-31")
    desc, first = growth.time_split(s.frames["recorded"])
    year = int(re.search(r"(\d{4})", desc).group(1))
    assert 2012 <= year <= 2024
    assert 0 < first.mean() < 1
    # the same through the one-call entry point
    s2 = study.run(raw, m, tp, log=lambda *_: None, start="2012-01-01", end="2024-12-31")
    assert len(s2.pairs["real"]) == len(real)


def test_standard_settings_are_the_plans_list():
    names = [s.name for s in study.STANDARD]
    assert names == ["all", "since-2000", "crash-1997-2003", "recovery-2003-2008",
                     "boom-2008-2021", "fall-2021-2024", "recent-2024-now",
                     "bought-after-crash"]
    for s in study.STANDARD:
        if s.start and s.end:
            assert s.start < s.end
    assert study.settings_named("standard") == list(study.STANDARD)
    assert [s.name for s in study.settings_named("all,crash-1997-2003")] == [
        "all", "crash-1997-2003"]
    with pytest.raises(ValueError):
        study.settings_named("no-such-setting")


def test_report_settings_saves_csvs_per_setting(data):
    """Task 7: `hkestate report --settings` saves effects/districts/index/returns per setting
    plus settings.json, and a setting with too few pairs says so instead of a verdict."""
    import json

    import pandas as pd

    tmp_path, _ = data
    assert cli.main(["report", "--from-snapshot", "--settings", "all,crash-1997-2003"]) == 0
    root = tmp_path / "reports" / "growth" / f"{date.today():%Y%m%d}"
    for name in ("all", "crash-1997-2003"):
        for f in ("effects", "districts", "index", "returns"):
            assert (root / name / f"{f}.csv").exists(), (name, f)
    meta = json.loads((root / "settings.json").read_text("utf-8"))
    assert meta["order"] == ["all", "crash-1997-2003"]
    allm = meta["settings"]["all"]
    assert allm["pairs"]["real"] >= 200 and allm["enough"] is True
    assert allm["start"] is None and allm["end"] is None and allm["seconds"] > 0
    crash = meta["settings"]["crash-1997-2003"]
    assert crash["pairs"]["real"] == 0 and crash["enough"] is False
    assert "too few" in crash["note"].lower()
    assert crash["start"] == "1997-10-01"
    eff = pd.read_csv(root / "all" / "effects.csv")
    assert {"setting", "pairs_type", "attribute", "label", "coef", "se", "p", "n_with",
            "robust", "verdict"} <= set(eff.columns)
    assert (eff["setting"] == "all").all() and "recorded" in set(eff["pairs_type"])
    assert pd.read_csv(root / "crash-1997-2003" / "effects.csv").empty
    idx = pd.read_csv(root / "all" / "index.csv")
    assert {"pairs_type", "group", "month", "level"} <= set(idx.columns)
    assert "all" in set(idx["group"])
    ret = pd.read_csv(root / "all" / "returns.csv")
    assert {"all", "first-hand", "second-hand"} & set(ret["group"])
    assert {"growth", "total_return", "drawdown"} <= set(ret.columns)
    dist = pd.read_csv(root / "all" / "districts.csv")
    assert {"district", "pairs", "growth", "total_return", "drawdown"} <= set(dist.columns)
    assert dist["pairs"].sum() > 0
    # the single findings report still comes out (from the `all` setting)
    assert (tmp_path / "reports" / f"findings_{date.today():%Y%m%d}.md").exists()


def test_explore_notebook_runs_and_has_no_outputs(data):
    """Task 5.5: the notebook's code reproduces the study (here from the test snapshot) and
    is stored without outputs."""
    import json
    from pathlib import Path

    nb = json.loads((Path(__file__).parents[1] / "notebooks" / "explore.ipynb").read_text(
        "utf-8"))
    code = [c for c in nb["cells"] if c["cell_type"] == "code"]
    assert all(not c.get("outputs") and c.get("execution_count") is None for c in code)
    src = "\n".join("".join(c["source"]) for c in code)
    src = src.replace("FROM_SNAPSHOT = False", "FROM_SNAPSHOT = True")
    scope: dict = {}
    exec(compile(src, "explore.ipynb", "exec"), scope)  # noqa: S102 - our own notebook
    assert "robust" in scope["s"].verdicts["recorded"].columns
