"""Task 4.5: the dashboard renders the ranked list, filters, a detail page and the model page,
and shows a banner when the last run failed. Rendered with Streamlit's AppTest on a
synthetic data folder (trained + ranked like the real one)."""

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
