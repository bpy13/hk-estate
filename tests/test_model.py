"""Task 4.2: the fair-price model. Out-of-time split on the last 6 months, error by group,
same-building comparables check, conformal 80% intervals, SHAP values per prediction."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from phase4_data import make_db

from hkestate import db, features, model


@pytest.fixture(scope="module")
def raw(tmp_path_factory):
    path = make_db(tmp_path_factory.mktemp("m") / "t.duckdb", n_deals=900)
    con = db.connect(path)
    r = features.extract(con)
    con.close()
    return r


@pytest.fixture(scope="module")
def deals(raw):
    return features.deal_features(raw)


def test_time_split_holds_out_the_last_six_months(deals):
    tr = deals[deals["exclude_reason"].isna()]
    split = model.time_split(tr, test_months=6)
    end = tr["agreement_date"].max()
    assert split.test["agreement_date"].min() > end - pd.DateOffset(months=6)
    assert split.test["agreement_date"].max() == end
    assert split.cal["agreement_date"].max() <= split.test_start
    assert split.cal["agreement_date"].min() > split.cal_start
    assert split.fit["agreement_date"].max() <= split.cal_start
    assert len(split.fit) + len(split.cal) + len(split.test) == len(tr)


def test_deflator_is_frozen_after_the_known_month(raw):
    idx = raw["index"]
    months = pd.to_datetime(pd.Series(["2024-01-15", "2025-01-15", "2026-10-15"]))
    classes = pd.Series(["B", "B", "B"])
    full = model.deflator(idx, classes, months, known_until=None)
    frozen = model.deflator(idx, classes, months, known_until=pd.Timestamp("2025-01-31"))
    # the fixture index rises 0.5% a month: Jan 2025 is the reference when frozen there
    assert frozen[1] == pytest.approx(1.0)
    assert frozen[2] == pytest.approx(1.0)  # later months carry the frozen value
    assert frozen[0] == pytest.approx(1.005 ** -12, rel=1e-6)
    assert full[2] == pytest.approx(1.0)  # after the last published month (Aug 2026)
    assert full[0] < frozen[0]


def test_conformal_quantile_with_finite_sample_correction_and_group_fallback():
    res = pd.Series(np.arange(1, 101) / 100)  # |log residuals| 0.01 .. 1.00
    groups = pd.Series(["territory"] * 100)
    q = model.conformal_q(res, groups, coverage=0.8, min_n=50)
    # ceil((n + 1) * 0.8) = 81st smallest of 100
    assert q["territory"] == pytest.approx(0.81)
    assert q["all"] == pytest.approx(0.81)
    assert "pilot" not in q  # too few: the caller falls back to "all"
    assert model.q_for(q, "pilot") == q["all"]


def test_errors_are_absolute_percent_of_price():
    actual = np.log(np.array([100.0, 100.0, 100.0]))
    pred = np.log(np.array([110.0, 90.0, 100.0]))
    assert list(model.abs_pct_error(actual, pred)) == pytest.approx([0.10, 0.10, 0.0])


def test_evaluate_reports_error_by_group_and_comparables(deals, raw):
    ev = model.evaluate(deals, raw["index"], rounds=150)
    groups = ev.summary.set_index("group")
    assert {"all", "pilot", "territory"} <= set(groups.index)
    # synthetic prices follow floor and time closely: a few % error at most
    assert groups.loc["all", "mdape"] < 0.08
    assert 0 < groups.loc["all", "n_test"] < len(deals)
    assert 0.5 <= groups.loc["all", "interval_coverage"] <= 1.0
    assert groups.loc["all", "comps_covered"] > 0
    assert not np.isnan(groups.loc["all", "comps_mdape"])
    # SHAP values stored per prediction add up to the model's output
    pred = ev.test_predictions
    shap_cols = [f"shap_{f}" for f in model.FEATURES] + ["shap_base"]
    assert np.allclose(pred[shap_cols].sum(axis=1), pred["pred_adj"], atol=1e-6)


def test_trainable_window_drops_deals_before_the_start_year(deals):
    """`since` is the training window (plan 2026-10-21 Task 5: all years vs 2010+)."""
    all_years = model.trainable(deals)
    since = model.trainable(deals, since=2021)
    assert all_years["agreement_date"].min().year < 2021
    assert since["agreement_date"].min().year >= 2021
    assert len(since) == int((all_years["agreement_date"].dt.year >= 2021).sum()) < len(all_years)


def test_evaluate_reports_error_by_decade_and_by_district(deals, raw):
    """Error by district on the out-of-time test deals; by decade of sale on a random share of
    the earlier deals held out of the test-stage model (an old sale the model never saw)."""
    ev = model.evaluate(deals, raw["index"], rounds=150)
    assert {"decade", "n", "mdape", "within_10"} <= set(ev.by_decade.columns)
    assert "2020s" in set(ev.by_decade["decade"])
    assert ev.by_decade["n"].sum() == ev.counts["holdout"] > 0
    assert ev.counts["holdout"] + ev.counts["hist_fit"] == ev.counts["fit"] + ev.counts["cal"]
    assert {"district", "n", "mdape", "within_10"} <= set(ev.by_district.columns)
    assert ev.by_district["n"].sum() == ev.counts["test"]
    assert "district" in ev.test_predictions.columns


def test_train_compares_with_the_previous_model_and_a_training_window(tmp_path, monkeypatch,
                                                                       raw):
    import shutil

    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    first = model.train(raw, rounds=60, today=date(2026, 10, 17))
    prev_dir = tmp_path / "previous"
    prev_dir.mkdir()
    for name in ("fair_price.txt", "fair_price.json"):
        shutil.copy(model.model_dir() / name, prev_dir / name)

    out = model.train(raw, rounds=60, today=date(2026, 10, 17), since=2021, compare_since=2023,
                      previous=prev_dir)
    meta = out["meta"]
    assert meta["window_since"] == 2021
    assert meta["n_train"] < first["meta"]["n_train"]
    assert meta["previous"]["trained_at"] == first["meta"]["trained_at"]
    assert meta["compare"]["since"] == 2023
    ev = out["eval"]
    assert ev.by_decade["mdape_prev"].notna().any()
    assert ev.by_district["mdape_prev"].notna().any()
    text = out["report_path"].read_text("utf-8")
    assert "## Compared with the previous run" in text
    assert "## Training window" in text
    assert "from 2021" in text and "from 2023" in text
    assert "By decade" in text and "By district" in text


def test_reasons_name_the_three_biggest_effects():
    shap = pd.Series({"shap_floor_num": 0.05, "shap_area_sqft": -0.02, "shap_district": 0.10,
                      "shap_age_years": -0.001})
    values = {"floor_num": 30, "area_sqft": 450, "district": "東區", "age_years": 40}
    got = model.reasons(shap, values, n=3)
    assert len(got) == 3
    assert got[0].startswith("district 東區") and "+10.5%" in got[0]
    assert got[1].startswith("floor 30") and "+5.1%" in got[1]
    assert got[2].startswith("size 450") and "-2.0%" in got[2]


def test_train_writes_model_report_and_predictions(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    out = model.train(raw, rounds=120, today=date(2026, 10, 17))
    for p in (out["model_path"], out["meta_path"], out["report_path"], out["test_path"]):
        assert p.exists(), p
    text = out["report_path"].read_text("utf-8")
    assert "median absolute % error" in text.lower()
    assert "pilot" in text and "territory" in text
    assert "MTR distance" in text  # coverage table
    m = model.load()
    lst = features.listing_features(raw, today=date(2026, 10, 17))
    assert len(model.predict_adj(m, lst)) == len(lst)


def test_model_files_are_written_and_read_under_a_shared_lock(tmp_path, monkeypatch):
    """train swaps fair_price.txt/.json in while rank may be reading them: both take the
    `model` lock, so a reader never sees a new .txt with an old .json."""
    import os

    from hkestate import locks

    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(model, "MODEL_LOCK_WAIT_S", 0.3)

    class Booster:
        def save_model(self, path):
            Path(path).write_text("trees", "utf-8")

    # free lock: the pair is written, and the lock is released afterwards
    model.save_model_files(Booster(), {"q": 1}, model.model_dir())
    assert (model.model_dir() / "fair_price.txt").exists()
    assert (model.model_dir() / "fair_price.json").exists()
    assert locks.held_by("model") is None

    # another live process holds the lock (as a running train would): both sides wait, then
    # give up with LockBusy instead of touching the files
    path = locks.lock_path("model")
    path.write_text(str(os.getppid()))
    with pytest.raises(locks.LockBusy):
        model.save_model_files(Booster(), {"q": 2}, model.model_dir())
    with pytest.raises(locks.LockBusy):
        model.load()
    assert json.loads((model.model_dir() / "fair_price.json").read_text("utf-8")) == {"q": 1}
