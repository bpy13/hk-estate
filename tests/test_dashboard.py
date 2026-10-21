"""Task 4.5: the dashboard renders the ranked list, filters, a detail page and the model page,
and shows a banner when the last run failed. Rendered with Streamlit's AppTest on a
synthetic data folder (trained + ranked like the real one)."""

import json
import shutil
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from phase4_data import add_listing, make_db
from streamlit.testing.v1 import AppTest

from hkestate import dashboard_data as dd
from hkestate import db, features, model, rank

APP = str(Path(__file__).resolve().parents[1] / "app" / "dashboard.py")
TODAY = date(2026, 10, 17)


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("dash")
    mp = pytest.MonkeyPatch()
    mp.setenv("HKESTATE_DATA_DIR", str(d))
    path = make_db(d / "hkestate.duckdb", n_deals=600)
    con = db.connect(path)
    add_listing(con, "centanet", "L1", "cd-1-AAA", price=3_000_000, area=520.0, flat="B",
                floor_text="高層", band="high", facing="東南")
    add_listing(con, "centanet", "L2", "h28-1-b1", price=12_000_000, area=610.0, flat="C",
                floor_text="18樓", features="分層單位|車位")
    con.execute("INSERT INTO risk_flag (source, listing_id, flag, evidence) "
                "VALUES ('centanet', 'L1', 'tenanted', '連租約')")
    db.finish_run(con, db.start_run(con, "daily"), "ok", 1, "")
    raw = features.extract(con)
    con.close()
    features.save_snapshot(raw)
    model.train(raw, rounds=100, today=TODAY, log=lambda *_: None)
    rank.write(rank.rank(raw, model.load(), today=TODAY), today=TODAY)
    yield d
    mp.undo()


def _app(data_dir, monkeypatch, **params) -> AppTest:
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(data_dir))
    at = AppTest.from_file(APP, default_timeout=60)
    for k, v in params.items():
        at.query_params[k] = v
    return at.run()


def test_ranked_page_renders_list_and_filters(data_dir, monkeypatch):
    at = _app(data_dir, monkeypatch)
    assert not at.exception
    assert at.title[0].value.startswith("Listings ranked")
    assert len(at.dataframe) == 1
    labels = {w.label for w in at.sidebar.multiselect} | {w.label for w in at.sidebar.slider}
    assert {"District", "Asking price (HK$m)", "Saleable area (sq ft)",
            "Has any of these flags", "Confidence"} <= labels
    assert not at.error  # the last run succeeded


def test_detail_page_shows_reasons_history_comparables_and_link(data_dir, monkeypatch):
    at = _app(data_dir, monkeypatch, page="detail", listing="centanet:L1")
    assert not at.exception
    assert at.title[0].value == "Listing detail"
    assert at.selectbox[0].value == "centanet:L1"
    assert len(at.metric) == 5
    md = " ".join(m.value for m in at.markdown)
    assert "Why this fair price" in md and "Comparables" in md and "Price history" in md
    assert "tenanted" in md


def test_model_page_shows_the_evaluation(data_dir, monkeypatch):
    at = _app(data_dir, monkeypatch, page="model")
    assert not at.exception
    assert any("median absolute % error" in m.value for m in at.markdown)


def test_banner_when_the_last_run_failed(data_dir, monkeypatch):
    con = db.connect(data_dir / "hkestate.duckdb")
    run = db.start_run(con, "daily")
    db.finish_run(con, run, "failed", 0, "SourceBlocked: 403")
    con.close()
    try:
        at = _app(data_dir, monkeypatch)
        assert not at.exception
        assert any("last run failed" in e.value for e in at.error)
    finally:
        con = db.connect(data_dir / "hkestate.duckdb")
        db.finish_run(con, run, "ok", 0, "")  # leave the folder as the other tests expect
        con.close()


def test_filters_narrow_the_list():
    df = pd.DataFrame({
        "district": ["東區", "元朗區", "東區"], "asking_hkd": [5e6, 9e6, 20e6],
        "area_sqft": [400.0, 600.0, 900.0], "confidence": ["normal", "low", "normal"],
        "flag_list": [["tenanted"], [], ["sea_view", "renovated"]],
    })
    assert len(dd.filter_ranked(df, districts=["東區"])) == 2
    assert len(dd.filter_ranked(df, price_m=(4, 10))) == 2
    assert len(dd.filter_ranked(df, size=(500, 1000))) == 2
    assert len(dd.filter_ranked(df, need_flags=["sea_view"])) == 1
    assert len(dd.filter_ranked(df, hide_flags=["tenanted"])) == 2
    assert len(dd.filter_ranked(df, confidence=["low"])) == 1


def test_run_status_names_commands_whose_newest_run_failed():
    log = pd.DataFrame({"run_id": [1, 2, 3], "command": ["ricacorp", "daily", "daily"],
                        "status": ["failed", "failed", "ok"], "started_at": ["a", "b", "c"],
                        "message": ["403", "x", ""], "args": [""] * 3, "finished_at": [""] * 3})
    s = dd._status_from(log, "database")
    assert s.failed_last is None
    assert list(s.failing["command"]) == ["ricacorp"]


# ---- growth page (Task 9) ---------------------------------------------------------------


def _effects(setting, verdict="robust", coef=0.01):
    return pd.DataFrame({
        "setting": setting, "pairs_type": "recorded", "attribute": ["size_small", "sea_view"],
        "label": ["Small flat", "Sea view"], "coef": [coef, -0.002], "se": [0.001, 0.001],
        "p": [0.001, 0.2], "n": [500, 500], "verdict": [verdict, "not significant"]})


def _write_setting(folder, name, *, effects=True, districts=True, index=True):
    d = folder / name
    d.mkdir(parents=True)
    if effects:
        _effects(name).to_csv(d / "effects.csv", index=False, encoding="utf-8")
    if districts:
        pd.DataFrame({
            "setting": name, "pairs_type": "recorded", "district": ["東區", "unknown"],
            "pairs": [300, 20], "median_years": [5.0, 4.0], "growth": [0.05, 0.01],
            "net_growth": [0.04, 0.0], "rental_yield": [0.03, 0.02],
            "total_return": [0.07, 0.02], "drawdown": [0.3, None], "peak": ["2021-09-01"] * 2,
            "trough": ["2024-10-01"] * 2}).to_csv(d / "districts.csv", index=False,
                                                  encoding="utf-8")
    if index:
        pd.DataFrame({"setting": name, "pairs_type": "recorded",
                      "group": ["all", "district:東區", "district:東區"],
                      "month": ["2020-01-01", "2020-01-01", "2020-02-01"],
                      "level": [100.0, 100.0, 103.0]}).to_csv(d / "index.csv", index=False,
                                                              encoding="utf-8")


@pytest.fixture
def growth_root(tmp_path):
    root = tmp_path / "growth"
    old = root / "20261001"
    old.mkdir(parents=True)
    new = root / "20261021"
    _write_setting(new, "all")
    _write_setting(new, "boom")
    _write_setting(new, "fall", effects=False)  # too few pairs: header-less / missing file
    _write_setting(new, "fall-1y", index=False)
    (new / "fall").joinpath("effects.csv").write_text("", encoding="utf-8")
    (new / "settings.json").write_text(json.dumps({
        "date": "2026-10-21", "order": ["all", "boom", "fall", "fall-1y", "ghost"],
        "settings": {
            "all": {"label": "All data", "enough": True, "pairs": {"recorded": 900}},
            "boom": {"label": "Boom", "enough": True},
            "fall": {"label": "Fall", "enough": False, "note": "Too few recorded pairs (1)"},
            "fall-1y": {"label": "Fall, 1-year hold", "enough": True, "sensitivity": True,
                        "min_hold_years": 1},
            "ghost": {"label": "Listed but no folder", "enough": True},
        }}, ensure_ascii=False), encoding="utf-8")
    return root


def test_latest_growth_dir_picks_the_newest_dated_folder(growth_root, tmp_path):
    assert dd.latest_growth_dir(growth_root).name == "20261021"
    assert dd.latest_growth_dir(tmp_path / "nothing") is None


def test_load_growth_handles_missing_files_too_few_pairs_and_sensitivity(growth_root):
    study = dd.load_growth(dd.latest_growth_dir(growth_root))
    assert [s.name for s in study.settings] == ["all", "boom", "fall", "fall-1y", "ghost"]
    assert [s.name for s in study.main] == ["all", "boom", "fall", "ghost"]
    assert [s.name for s in study.sensitivities] == ["fall-1y"]
    assert study.get("fall-1y").min_hold_years == 1
    fall, ghost = study.get("fall"), study.get("ghost")
    assert fall.effects.empty and not fall.has_verdicts and fall.note.startswith("Too few")
    assert ghost.effects.empty and ghost.districts.empty and not ghost.has_verdicts
    assert study.get("fall-1y").index.empty  # missing index.csv


def test_load_growth_without_settings_json_uses_the_folders(tmp_path):
    folder = tmp_path / "20261021"
    _write_setting(folder, "b")
    _write_setting(folder, "a")
    study = dd.load_growth(folder)
    assert [s.name for s in study.settings] == ["a", "b"]
    assert all(s.has_verdicts and not s.sensitivity for s in study.settings)


def test_verdict_table_keeps_sensitivities_out_and_marks_too_few_pairs(growth_root):
    study = dd.load_growth(dd.latest_growth_dir(growth_root))
    t = dd.verdict_table(study.main)
    assert list(t.columns) == ["Attribute", "all", "boom", "fall", "ghost"]
    assert "fall-1y" not in t.columns
    assert t.loc["size_small", "all"] == "robust (+1.00)"
    assert t.loc["sea_view", "boom"] == "no effect found (-0.20)"
    assert t.loc["size_small", "fall"] == "too few pairs"
    s = dd.verdict_table(study.sensitivities)
    assert list(s.columns) == ["Attribute", "fall-1y"]


def test_effect_rows_and_attribute_sentence(growth_root):
    study = dd.load_growth(dd.latest_growth_dir(growth_root))
    ev = dd.effect_rows(study.main)
    assert set(ev["setting"]) == {"all", "boom"}
    row = ev[(ev["setting"] == "all") & (ev["attribute"] == "size_small")].iloc[0]
    assert row["effect"] == pytest.approx(1.0)
    assert row["low"] == pytest.approx(1.0 - 0.196)
    assert dd.effect_rows([study.get("fall")]).empty
    assert dd.attribute_sentence("size_small", study.main).startswith("Holds in every setting")
    assert dd.attribute_sentence("sea_view", study.main).startswith("No clear effect")
    assert dd.attribute_sentence("nope", study.main) == "No result for this attribute."


def test_district_names_carry_english_and_chinese(growth_root):
    study = dd.load_growth(dd.latest_growth_dir(growth_root))
    t = dd.district_table(study.get("all"))
    # the same label as the findings report (study.district_label, pre-release review
    # 2026-10-21: one definition, not two)
    assert list(t["District"]) == ["Eastern (東區)", "unknown"]
    idx = dd.district_index(study.get("all"))
    assert set(idx["District"]) == {"Eastern (東區)"} and len(idx) == 2
    assert dd.district_table(study.get("ghost")).empty
    assert dd.district_label("新界") == "新界"  # unmapped names pass through


def test_growth_page_opens_for_all_and_a_phase_setting(growth_root, tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "reports").mkdir(parents=True)
    shutil.copytree(growth_root, data / "reports" / "growth")
    for setting in ("all", "fall", "fall-1y"):
        at = _app(data, monkeypatch, page="growth", setting=setting)
        assert not at.exception, setting
        assert at.title[0].value.startswith("Which flat features")
        assert len(at.dataframe) >= 2
    at = _app(tmp_path / "empty", monkeypatch, page="growth")
    assert not at.exception and any("No growth study" in i.value for i in at.info)
