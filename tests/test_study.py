"""Tasks 5.3-5.4 end to end on the small synthetic database: `hkestate report` builds pairs,
runs the regressions and writes data/reports/findings_<date>.md with its charts."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from phase4_data import add_deal, make_db

from hkestate import cli, costs, db, features, model, study

TODAY = date(2026, 10, 17)


def _rvd(months, rent_psm, price_psm, cls="B", region="HK"):
    rows = []
    for m in months:
        rows.append({"kind": "rent", "series": "rvd_avg_rent_psm", "freq": "month",
                     "period": m, "class": cls, "region": region, "value": rent_psm})
        rows.append({"kind": "price", "series": "rvd_avg_price_psm", "freq": "month",
                     "period": m, "class": cls, "region": region, "value": price_psm})
    return pd.DataFrame(rows)


def _anchor_inputs(extra_deals):
    """25 buildings sold at HK$15,000/sq ft in 2026 with HK$30/sq ft asking rents (listing
    yield 2.4% vs RVD 3%: anchor 0.8), plus `extra_deals` rows."""
    months = pd.date_range("2026-01-01", "2026-08-01", freq="MS")
    y = costs.rvd_yields(_rvd(months, 300.0, 120_000.0))
    ids = [f"b{i}" for i in range(25)]
    deals = pd.DataFrame({
        "source": "centadata", "source_id": [f"d{i}" for i in range(25)], "building_id": ids,
        "agreement_date": pd.Timestamp("2026-06-01"), "psf_hkd": 15_000.0, "rvd_class": "B",
        "district": "東區", "exclude_reason": None})
    deals = pd.concat([deals, pd.DataFrame(extra_deals)], ignore_index=True)
    deals["agreement_date"] = pd.to_datetime(deals["agreement_date"])
    listings = pd.DataFrame({"kind": "rent", "building_id": ids, "saleable_sqft": 500.0,
                             "price_hkd": 15_000.0})
    raw = {"listings": listings, "meta": {"extracted_at": "2026-10-21T17:54:59"},
           "run_log": pd.DataFrame()}
    return raw, deals, y


def test_listing_anchor_window_ignores_a_far_future_deal_date():
    """Pre-release review 2026-10-21: the anchor's 12-month sales window ended at the latest
    deal date of any stored row, so one SRPE row dated 2055 (a register typo) emptied it and
    the yields silently fell back to RVD alone (factor 1, 0 buildings). The window now ends
    at the latest clean deal on or before the snapshot's extraction date."""
    typo = {"source": "srpe", "source_id": "typo", "building_id": None,
            "agreement_date": "2055-09-26", "psf_hkd": np.nan, "rvd_class": "B",
            "district": None, "exclude_reason": "first_hand"}
    # a clean-looking row dated after the snapshot must not move the window either
    soon = {"source": "centadata", "source_id": "soon", "building_id": "b0",
            "agreement_date": "2030-01-05", "psf_hkd": 15_000.0, "rvd_class": "B",
            "district": "東區", "exclude_reason": None}
    raw, deals, y = _anchor_inputs([typo, soon])
    a = study.listing_anchor(raw, deals, y)
    assert a["basis"] == "listings" and a["buildings"] == 25
    assert a["factor"] == pytest.approx(0.8)
    assert a["rent_listings"] == 25


def test_coverage_last_date_ignores_a_register_typo():
    typo = {"source": "srpe", "source_id": "typo", "building_id": None,
            "agreement_date": "2055-09-26", "psf_hkd": np.nan, "rvd_class": "B",
            "district": None, "exclude_reason": "first_hand"}
    raw, deals, _ = _anchor_inputs([typo])
    deals = deals.assign(is_pilot=False, estate_key="e", mtr_m=np.nan, rel_floor=np.nan,
                         facing="unknown", built_year=np.nan)
    cov = study.coverage(raw, deals)
    bs = cov["by_source"]
    assert bs.loc["srpe", "deals"] == 1  # still counted as stored
    assert pd.isna(bs.loc["srpe", "last"])  # but its date is not shown as the latest sale
    assert bs.loc["centadata", "last"] == pd.Timestamp("2026-06-01")
    assert cov["clean"] == 25


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
    # short holds inside the 2021-2024 fall (bought 2022, resold 1.2-2.5 years later): no
    # 3-year pair, but the 1-year sensitivity picks them up
    for i in range(30):
        b = ["cd-1-AAA", "cd-1-BBB"][i % 2]
        floor, flat = 2 + i % 28, f"S{i % 4}"
        d0 = date(2022, 3, 1) + timedelta(days=rng.randint(0, 120))
        d1 = d0 + timedelta(days=rng.randint(440, 910))
        p0 = int(600.0 * 8000 * rng.uniform(0.95, 1.05))
        add_deal(con, f"s{i}a", b, floor, flat, d0, p0, area=600.0, source="centadata")
        add_deal(con, f"s{i}b", b, floor, flat, d1, int(p0 * 0.95), area=600.0,
                 source="centadata")
        con.execute("INSERT OR IGNORE INTO unit (unit_id, building_id, floor_label, floor_num, "
                    "floor_kind, flat) VALUES (?, ?, ?, ?, 'number', ?)",
                    [db.unit_id(b, str(floor), flat), b, str(floor), floor, flat])
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
    # (no "Cross-check: estimated pairs" section here: the synthetic data has one estimated
    # pair, too few for a regression)
    for heading in ("## Verdicts by attribute and market phase", "## First-hand vs second-hand",
                    "## Limits", "## By district",
                    "## Growth, total return and drawdown by attribute"):
        assert heading in text
    # Task 8: the old provisional wording is gone (recorded pairs now span 1995-2026)
    for stale in ("PRELIMINARY", "pilot-heavy", "since about 2018",
                  "What changes when the backfill completes"):
        assert stale not in text
    assert "Yuen Long (元朗區)" in text  # districts carry the English name alongside
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


def test_short_hold_sensitivity_adds_pairs_held_one_to_three_years(data):
    """Decision 2026-10-21: the 2021-2024 fall holds no 3-year pairs, so it also runs with a
    1-year minimum hold, clearly flagged; the main settings are unchanged by it."""
    import pandas as pd

    _, raw = data
    tp = pd.read_parquet(model.model_dir() / "test_predictions.parquet")
    base = study.prepare(raw, model.load(), tp, log=lambda *_: None)
    assert (base.pairs["real"]["years"] >= 3).all()
    assert (base.short["real"]["years"] < 3).all() and (base.short["real"]["years"] >= 1).all()
    main = study.analyse(base, "2021-10-01", "2024-10-31", name="fall-2021-2024")
    assert main.pairs["real"].empty and main.enough is False
    assert main.min_hold_years == 3 and main.sensitivity is False
    s = study.analyse(base, "2021-10-01", "2024-10-31", name="fall-2021-2024-1y",
                      min_hold_years=1.0, sensitivity=True, label="Sensitivity")
    real = s.pairs["real"]
    assert len(real) >= 25
    assert real["years"].between(1, 3).all()
    assert (real["start_date"] >= pd.Timestamp("2021-10-01")).all()
    assert (real["end_date"] <= pd.Timestamp("2024-10-31")).all()
    # the short holds get costs and yields too, and the recorded set includes them
    assert {"net_ann_growth", "rental_yield", "total_return"} <= set(real.columns)
    assert len(s.pairs["recorded"]) == len(real) + len(s.pairs["ricacorp"])
    assert s.sensitivity and s.min_hold_years == 1.0 and s.label == "Sensitivity"
    assert any(n.startswith("Sensitivity") for n in s.notes)
    # the all-data study still holds only 3-year pairs
    assert (study.analyse(base).pairs["real"]["years"] >= 3).all()


def test_standard_settings_are_the_plans_list():
    names = [s.name for s in study.STANDARD]
    assert names == ["all", "since-2000", "crash-1997-2003", "recovery-2003-2008",
                     "boom-2008-2021", "fall-2021-2024", "recent-2024-now",
                     "bought-after-crash", "fall-2021-2024-1y", "recent-2024-now-1y"]
    for s in study.STANDARD:
        if s.start and s.end:
            assert s.start < s.end
        # the short-hold runs are flagged sensitivities; the main settings keep the 3-year rule
        assert s.sensitivity == s.name.endswith("-1y")
        assert s.min_hold_years == (1.0 if s.sensitivity else 3.0)
    by_name = {s.name: s for s in study.STANDARD}
    assert by_name["fall-2021-2024-1y"].start == by_name["fall-2021-2024"].start
    assert by_name["fall-2021-2024-1y"].end == by_name["fall-2021-2024"].end
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
    assert cli.main(["report", "--from-snapshot", "--settings",
                     "all,crash-1997-2003,fall-2021-2024-1y"]) == 0
    root = tmp_path / "reports" / "growth" / f"{date.today():%Y%m%d}"
    for name in ("all", "crash-1997-2003", "fall-2021-2024-1y"):
        for f in ("effects", "districts", "index", "returns"):
            assert (root / name / f"{f}.csv").exists(), (name, f)
    meta = json.loads((root / "settings.json").read_text("utf-8"))
    assert meta["order"] == ["all", "crash-1997-2003", "fall-2021-2024-1y"]
    allm = meta["settings"]["all"]
    assert allm["pairs"]["real"] >= 200 and allm["enough"] is True
    assert allm["start"] is None and allm["end"] is None and allm["seconds"] > 0
    assert allm["sensitivity"] is False and allm["min_hold_years"] == 3
    crash = meta["settings"]["crash-1997-2003"]
    assert crash["pairs"]["real"] == 0 and crash["enough"] is False
    assert "too few" in crash["note"].lower()
    assert crash["start"] == "1997-10-01"
    # the sensitivity is flagged for the dashboard and holds the short-hold pairs
    sens = meta["settings"]["fall-2021-2024-1y"]
    assert sens["sensitivity"] is True and sens["min_hold_years"] == 1
    assert sens["pairs"]["real"] >= 25
    text = (tmp_path / "reports" / f"findings_{date.today():%Y%m%d}.md").read_text("utf-8")
    assert "## Short-hold sensitivity" in text
    assert "| Attribute | all | crash-1997-2003 |" in text  # the sensitivity is not a column
    assert text.index("## Verdicts by attribute and market phase") < text.index(
        "## Short-hold sensitivity")
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
