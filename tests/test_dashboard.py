"""Task 4.5: the dashboard renders the ranked list, filters, a detail page and the model page,
and shows a banner when the last run failed. Rendered with Streamlit's AppTest on a
synthetic data folder (trained + ranked like the real one)."""

import json
import re
import shutil
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from phase4_data import add_listing, make_db
from streamlit.testing.v1 import AppTest

from hkestate import commute, db, features, model, rank
from hkestate import dashboard_data as dd

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


def test_excluded_rows_are_hidden_unless_asked():
    df = pd.DataFrame({"district": ["a", "b", "c"], "exclude_reason": [None, "check_area", None]})
    assert list(dd.filter_ranked(df)["district"]) == ["a", "c"]
    assert len(dd.filter_ranked(df, show_excluded=True)) == 3
    assert len(dd.filter_ranked(df.drop(columns="exclude_reason"))) == 3  # older file


def test_ranked_page_has_the_show_excluded_checkbox(data_dir, monkeypatch):
    at = _app(data_dir, monkeypatch)
    box = next(c for c in at.sidebar.checkbox if c.label.startswith("Show rows that failed"))
    assert box.value is False
    hidden = len(at.dataframe[0].value)
    at = box.check().run()
    assert not at.exception
    assert len(at.dataframe[0].value) >= hidden
    assert "exclude_reason" in at.dataframe[0].value.columns


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
    assert t.loc["size_small", "fall"] == "too few flats sold twice"
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
    at = _app(data, monkeypatch, page="growth", setting="fall")
    kind = next(r for r in at.sidebar.radio if r.label == "Which flats")
    assert kind.options == ["Flats sold twice (two recorded sales)",
                            "Model-valued flats (cross-check)"]
    assert any("Too few flats sold twice" in i.value for i in at.info)  # the setting's note
    shown = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert not re.search(r"\bpairs?\b", shown, flags=re.I)
    kind.set_value("estimated").run()  # the model-valued kind carries the caveat
    assert not at.exception
    assert any("trust it less than the result from flats sold twice" in w.value
               for w in at.warning)
    at = _app(tmp_path / "empty", monkeypatch, page="growth")
    assert not at.exception and any("No growth study" in i.value for i in at.info)


# ---- Market today (Task 22) ------------------------------------------------------------------


def test_market_loader_labels_and_table(data_dir, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(data_dir))
    m = dd.market_data()
    assert m is not None and dd.market.TERRITORY in m.trends
    assert dd.snapshot_stamp() > 0
    assert dd.market_header("2026-10-20", "2026-09-08") == (
        "Sales up to 20 Oct 2026; the last 6 weeks are incomplete "
        "(deals reach the register late).")
    t = dd.market_table(m.summary, "ALL")
    assert t.iloc[0]["District"] == dd.ALL_HK  # all of Hong Kong first
    text = " ".join(t.columns) + " " + " ".join(dd.SIZE_LABELS.values())
    assert "psf" not in text.lower() and "rvd" not in text.lower()
    assert set(t["Note"]) <= {"", "few sales"}
    assert dd.pct_text(0.032) == "+3.2%" and dd.pct_text(float("nan")) == "–"


def test_market_loader_none_without_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    assert dd.market_data() is None and dd.snapshot_stamp() == 0.0


def test_market_page_renders_tiles_pickers_table_and_charts(data_dir, monkeypatch):
    at = _app(data_dir, monkeypatch, page="market")
    assert not at.exception
    assert at.title[0].value == "Market today"
    assert at.caption[0].value.startswith("Sales up to ")
    assert [s.label for s in at.selectbox] == ["Where", "Flat size"]
    assert len(at.metric) == 4 and len(at.dataframe) == 1
    at.selectbox[1].select("B").run()
    assert not at.exception


# ---- Plan a move (Task 21) -------------------------------------------------------------------


@pytest.fixture
def plan():
    """The advisor's small fixture (tests/test_advisor.py) as the page's data."""
    import test_advisor as ta

    from hkestate import advisor
    stations, names = advisor.read_stations(ta.STATIONS_CSV)
    graph = commute.build_graph(ta.LINES_CSV, {c: (la, lo) for c, la, lo in stations})
    districts, effects = advisor.growth_tables(ta.FIX / "growth", "all")
    return advisor.AdvisorData(
        sale=ta.SALE, rent_listings=ta._rent_listings(), buildings=ta.BUILDINGS,
        district_growth=districts, effects=effects, stations=stations, station_names=names,
        graph=graph, rent_model=ta._rent_model(), today=ta.TODAY, growth_setting="all",
        district_index=advisor.growth_index(ta.FIX / "growth", "all"))


def test_plan_profile_turns_answers_into_a_profile(plan):
    p = dd.plan_profile({"intent": "buy", "goals": {"investment": 3, "near_place": 2,
                                                    "school": 2, "space": 0},
                         "budget": 9_000_000, "stations": ["ADM"], "max_commute": 40,
                         "school_net": None, "min_rooms": 0, "min_sqft": 0}, plan)
    assert p.weights["investment"] == 3 and p.weights["near_place"] == 2
    assert p.weights["school"] == 0  # no net chosen: the goal is dropped, not scored 0
    assert p.min_rooms is None and p.min_sqft is None and p.max_commute_min == 40
    # no place chosen: the near-a-place goal is dropped too
    p2 = dd.plan_profile({"goals": {"near_place": 3, "space": 1}}, plan)
    assert p2.weights["near_place"] == 0 and p2.weights["space"] == 1


def test_plan_cards_for_an_investor(plan):
    p = dd.plan_profile({"intent": "buy", "goals": {"investment": 3}, "budget": 10_000_000}, plan)
    out = dd.plan_cards(p, plan)
    assert [c.title for c in out.estates] == ["Harbour Court", "Sunny Garden", "Mid Tower"]
    first = out.estates[0]
    assert [g["label"] for g in first.goals] == ["Investment"]
    assert "district grew 5.0%/yr" in first.goals[0]["reason"]
    assert 0 < first.score <= 100
    l1 = out.listings[0]
    assert l1.link == "https://example.test/L1" and l1.title.startswith("Harbour Court")
    # three cases in plain words, from the buy-vs-rent figures
    assert len(l1.money) == 3
    assert l1.money[0].startswith("Low growth: over 10 years, buying comes out about HK$")
    assert all(" if prices grow " in m and m.endswith(" a year.") for m in l1.money)
    assert "ahead" in l1.money[1] or "behind" in l1.money[1]
    assert any("rent" in line.lower() for line in l1.facts)
    assert out.notes == [] or all("station" not in n for n in out.notes)


def test_plan_cards_use_the_trusted_rent(plan):
    """Buy-vs-rent on a card uses the same trusted rent as the advisor (changed 2026-10-23
    after the pre-release review): Mid Tower's district rent (24,000) is capped at 灣仔區's
    median yield (16,250); with only government averages there are no figures."""
    from dataclasses import replace

    import test_advisor as ta

    from hkestate import rent
    p = dd.plan_profile({"intent": "buy", "goals": {"investment": 3}, "budget": 10_000_000}, plan)
    mid = [c for c in dd.plan_cards(p, plan).listings if c.title.startswith("Mid Tower")][0]
    assert any("about HK$16,250 a month" in f and "capped at the district's median yield" in f
               for f in mid.facts), mid.facts
    assert len(mid.money) == 3
    raw = pd.DataFrame(ta._rent_rows("b-harbour", 40.0, 5))
    li = rent.clean_rent_listings(raw).assign(month=pd.Timestamp("2026-08-01"))
    only = rent.RentModel.from_frames(li, ta.BUILDINGS[["estate_key", "district"]], ta._rvd())
    out = dd.plan_cards(p, replace(plan, rent_model=only))
    sunny = [c for c in out.listings if c.title.startswith("Sunny Garden")][0]
    assert sunny.money == []
    assert any("no reliable rent figure for this building" in f for f in sunny.facts)


def test_plan_cards_for_a_family(plan):
    p = dd.plan_profile({"intent": "buy", "goals": {"near_place": 3, "school": 2, "space": 3},
                         "budget": 10_000_000, "min_sqft": 600, "min_rooms": 3,
                         "stations": ["ADM"]}, plan)
    out = dd.plan_cards(p, plan)
    assert [c.title for c in out.estates] == ["Sunny Garden", "Mid Tower"]
    labels = [g["label"] for g in out.listings[0].goals]
    assert labels == ["Near a place", "Space"]  # no net chosen, so no school line
    near = out.listings[0].goals[0]["reason"]
    assert "min to Admiralty" in near
    assert out.listings[0].goals[0]["score"] is not None
    # Mid Tower has no coordinates: shown as unknown, never as good
    mid = [c for c in out.listings if c.title.startswith("Mid Tower")][0]
    assert mid.goals[0]["score"] is None and "unknown" in mid.goals[0]["reason"]


def test_plan_cards_without_station_data_say_so_and_do_not_fail(plan):
    from dataclasses import replace
    bare = replace(plan, stations=[], graph={})
    p = dd.plan_profile({"goals": {"near_place": 3, "space": 1}, "stations": ["ADM"],
                         "max_commute": 30, "min_sqft": 500}, bare)
    assert p.max_commute_min is None  # a limit on an unknowable value would drop every flat
    out = dd.plan_cards(p, bare)
    assert out.estates  # still answers
    assert any("commute times appear once station data is loaded" in n for n in out.notes)


def test_plan_cards_for_an_owner_selling_or_letting(plan):
    owner = {"building_id": "b-harbour", "area_sqft": 450, "floor_text": "18樓",
             "value_hkd": 9_000_000}
    for intent in ("sell", "let"):
        p = dd.plan_profile({"intent": intent, "years": 10}, plan)
        out = dd.plan_cards(p, plan, owner=owner)
        assert out.estates == [] and out.listings == []
        card = out.owner
        assert card.title.startswith("Harbour Court")
        assert any("HK$9.00m" in f for f in card.facts)  # the value given
        assert len(card.money) == 3
        assert card.money[0].startswith("Low growth: over 10 years, letting comes out about "
                                        "HK$")
        assert "selling now if prices grow" in card.money[1]
    # no fair-price model and no value given: say why, no figures
    out = dd.plan_cards(dd.plan_profile({"intent": "sell"}, plan), plan,
                        owner={"building_id": "b-harbour", "area_sqft": 450,
                               "floor_text": "中層"})
    assert out.owner.money == [] and any("no fair-price model" in n for n in out.notes)


def test_find_buildings_searches_estate_and_building_names(plan):
    r = dd.find_buildings(plan, "sunny")
    assert list(r["building_id"]) == ["b-sunny"]
    assert dd.find_buildings(plan, "zzz").empty and dd.find_buildings(plan, " ").empty
    assert set(dd.find_buildings(plan, "tower")["building_id"]) == {"b-harbour", "b-mid"}

def test_plan_page_opens_answers_and_shows_the_assumptions(data_dir, monkeypatch):
    at = _app(data_dir, monkeypatch, page="plan")
    assert not at.exception
    assert at.title[0].value == "Plan a move"
    assert "not financial or legal advice" in at.caption[0].value
    assert [e.label for e in at.expander] == ["What the figures assume"]
    # the test database has no station or school data: the page says so and still works
    assert any("appear once station data is loaded" in c.value for c in at.caption)
    at.number_input[0].set_value(100.0)  # HK$100m: no price limit in effect
    at.button[0].click().run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    # the test database may hold no valued listing: then the page says so instead of failing
    assert "Overall score" in text or any("No listings" in i.value for i in at.info), \
        [i.value for i in at.info]


def test_building_sales_lists_one_buildings_clean_sales_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    (tmp_path / "model").mkdir()
    rows = [("b1", None, "2026-03-12", "18", "B", 520.0, 8_200_000),
            ("b1", None, "2025-01-05", "7", "A", 480.0, 6_000_000),
            ("b1", "duplicate", "2026-05-01", "9", "A", 480.0, 6_100_000),
            ("b2", None, "2026-06-01", "3", "C", 300.0, 3_000_000)]
    pd.DataFrame(rows, columns=["building_id", "exclude_reason", "agreement_date",
                                "floor_label", "flat", "area_sqft", "price_hkd"]
                 ).assign(agreement_date=lambda d: pd.to_datetime(d["agreement_date"])
                          ).to_parquet(tmp_path / "model" / "deals.parquet")
    s = dd.building_sales("b1")
    assert list(s["price_hkd"]) == [8_200_000, 6_000_000]  # newest first; the duplicate is out
    assert s["label"][0] == "12 Mar 2026, floor 18, flat B, 520 sq ft, HK$8.20m"
    assert dd.building_sales("nowhere").empty


def test_price_check_lines_use_plain_words():
    nan = float("nan")
    base = {"fair_hkd": 7_500_000, "low_hkd": 6_786_000, "high_hkd": 8_289_000,
            "gap_pct": 0.0667, "gap_corrected_pct": 0.0667, "learned": False,
            "verdict": "within the normal range", "basis": "x"}
    lines = dd.price_check_lines(base, date(2026, 10, 23), 8_000_000)
    assert lines[0] == "Fair price on 23 Oct 2026: HK$7.50m."
    assert lines[1] == "Normal range: HK$6.79m to HK$8.29m (about ±11%)."
    assert lines[2] == "The price of HK$8.00m is 6.7% above the fair price."
    assert lines[3] == "That is within the normal range."
    assert len(lines) == 4
    # learned: the raw gap is stated as it is, then the corrected gap the verdict uses
    learned = dict(base, learned=True, gap_pct=-0.0933, gap_corrected_pct=-0.1167,
                   verdict="below the normal range")
    lines = dd.price_check_lines(learned, date(2026, 3, 1), 6_800_000)
    assert "9.3% below the fair price" in lines[2]
    assert "Corrected (x 1.25), the gap is -11.7%" in lines[3]
    assert lines[4] == "That is below the normal range."
    assert len(dd.price_check_lines(dict(base, verdict=None), date(2026, 3, 1), None)) == 2
    no = dict(fair_hkd=nan, basis="building not in the snapshot")
    assert dd.price_check_lines(no, date(2026, 3, 1), None) == [
        "We cannot value this flat: building not in the snapshot."]


def test_check_page_picks_a_building_and_a_recorded_sale(data_dir, monkeypatch):
    at = _app(data_dir, monkeypatch, page="check")
    assert not at.exception and at.title[0].value == "Check a price"
    deals = dd.deals()
    deals = deals[deals["exclude_reason"].isna()]
    bid = deals["building_id"].value_counts().index[0]
    at.text_input[0].set_value(bid).run()
    assert not at.exception, at.exception
    sale_box = next(s for s in at.selectbox if s.label.startswith("Or pick a recorded sale"))
    sale_box.set_value(0).run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Fair price on" in text and "Normal range:" in text
    assert "normal range." in text  # the verdict, as the sale's own price is filled in


def test_plan_page_sell_asks_for_the_building_first(data_dir, monkeypatch):
    at = _app(data_dir, monkeypatch, page="plan")
    at.radio[0].set_value("sell").run()
    assert not at.exception and any(t.label.startswith("Your estate") for t in at.text_input)
    at.button[0].click().run()
    assert any("Search for your building" in w.value for w in at.warning)
