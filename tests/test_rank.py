"""Task 4.4: `hkestate rank` writes data/reports/ranked_listings_<date>.csv sorted by %
discount, with fair price, interval, confidence (low if <10 comparables in 24 months), flags
and the top 3 SHAP reasons."""

from datetime import date

import pandas as pd
import pytest
from phase4_data import add_listing, make_db

from hkestate import cli, db, features, model, rank

TODAY = date(2026, 10, 17)


def test_discount_is_against_the_expected_deal_price():
    # asking 10m, usual gap 5% -> expected deal 9.5m; fair 10m -> 5% below fair
    assert rank.discount(10_000_000, 10_000_000, 0.05) == pytest.approx(0.05)
    assert rank.discount(12_000_000, 10_000_000, 0.05) == pytest.approx(-0.14)


@pytest.fixture
def ranked(tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    path = make_db(tmp_path / "hkestate.duckdb", n_deals=700)
    con = db.connect(path)
    add_listing(con, "centanet", "CHEAP", "cd-1-AAA", price=6_500_000, area=520.0, flat="B",
                floor_text="高層", band="high", facing="東南", dup_group="g1")
    add_listing(con, "spacious", "CHEAP-DUP", "cd-1-AAA", price=6_600_000, area=520.0,
                flat="B", band="high", dup_group="g1")
    # Task 2 checks: one listing per rule (areas follow the fixture's flat letters:
    # A 500, B 520, C 610, D 700, others 450)
    add_listing(con, "centanet", "TYPO", "cd-1-BBB", price=90_000_000, area=6000.0, flat="A")
    add_listing(con, "centanet", "GROSS", "cd-1-AAA", price=9_000_000, area=730.0, flat="C")
    add_listing(con, "centanet", "COMBINED", "cd-1-BBB", price=10_000_000, area=700.0, flat="D",
                features="相鄰單位")
    add_listing(con, "centanet", "TWOFLATS", "cd-1-BBB", price=16_000_000, area=1100.0)
    add_listing(con, "centanet", "DUPLEX", "cd-1-BBB", price=20_000_000, area=1100.0,
                features="複式")
    add_listing(con, "centanet", "TOOHIGH", "cd-1-AAA", price=30_000_000, area=520.0, flat="B")
    add_listing(con, "centanet", "TOOLOW", "cd-1-AAA", price=4_300_000, area=520.0, flat="B",
                band="low")
    add_listing(con, "centanet", "LOWEST", "cd-1-AAA", price=5_900_000, area=500.0, flat="A")
    # Task 3: same flat on one site twice (one with flat and band blank), and two different
    # flats of one size at one price
    add_listing(con, "centanet", "SAME1", "cd-1-BBB", price=7_000_000, area=450.0, flat="E",
                band="mid")
    add_listing(con, "centanet", "SAME2", "cd-1-BBB", price=7_030_000, area=450.0, band="mid")
    add_listing(con, "centanet", "DIFF1", "h28-1-b1", price=14_000_000, area=450.0, flat="G")
    add_listing(con, "centanet", "DIFF2", "h28-1-b1", price=14_000_000, area=450.0, flat="H")
    add_listing(con, "centanet", "DEAR", "h28-1-b1", price=40_000_000, area=610.0, flat="C",
                floor_text="18樓", features="分層單位|車位")
    add_listing(con, "centanet", "FEW", "cd-1-NEW", price=12_000_000, area=500.0, flat="A",
                floor_text="中層", band="mid")
    add_listing(con, "centanet", "NOMATCH", None, price=5_000_000, area=400.0)
    con.execute("INSERT INTO risk_flag (source, listing_id, flag, evidence) "
                "VALUES ('centanet', 'CHEAP', 'tenanted', '連租約')")
    raw = features.extract(con)
    con.close()
    model.train(raw, rounds=120, today=TODAY, log=lambda *_: None)
    return rank.rank(raw, model.load(), today=TODAY)


def test_ranked_listings_sorted_by_discount_with_all_columns(ranked):
    df = ranked.table
    clean = df[df["exclude_reason"].isna()]  # excluded rows follow, see the Task 2 tests
    assert list(clean["discount_pct"]) == sorted(clean["discount_pct"], reverse=True)
    assert df.iloc[0]["listing_id"] == "CHEAP"
    for col in ["fair_price_hkd", "fair_low_hkd", "fair_high_hkd", "confidence", "comps_24m",
                "flags", "reason_1", "reason_2", "reason_3", "gap_pct", "gap_basis", "url"]:
        assert col in df.columns, col
    assert (df["fair_low_hkd"] < df["fair_price_hkd"]).all()
    assert (df["fair_price_hkd"] < df["fair_high_hkd"]).all()
    assert df["reason_1"].str.contains("%").all()


def test_listings_are_valued_at_the_last_well_populated_month(ranked):
    m = model.load()
    # synthetic deals run to 2026-09: the valuation month is the last with 200+ deals or,
    # with fewer deals in total, the newest month with the most deals seen recently
    assert (ranked.table["month_idx"] == m.meta["valuation_month_idx"]).all()
    assert m.meta["valuation_month_idx"] <= features.month_index(TODAY)


def test_duplicates_across_sites_are_ranked_once(ranked):
    df = ranked.table
    assert "CHEAP-DUP" not in set(df["listing_id"])
    assert "spacious:CHEAP-DUP" in df.set_index("listing_id").loc["CHEAP", "also_listed"]


def test_listing_checks_give_each_rule_its_reason(ranked):
    df = ranked.table.set_index("listing_id")
    reason = df["exclude_reason"]
    assert reason["TYPO"] == "check_area"
    assert reason["TWOFLATS"] == "check_area"  # 1,100 sq ft vs nearest recorded 700 (1.57x)
    assert reason["GROSS"] == "check_area_basis"  # 730 vs flat C's recorded 610 (1.20x)
    assert reason["COMBINED"] == "check_combined"
    assert reason["TOOHIGH"] == "check_price"
    assert reason["TOOLOW"] == "check_price"
    for ok in ("CHEAP", "DEAR", "FEW", "DUPLEX", "LOWEST"):  # duplex: 1,100 allowed
        assert pd.isna(reason[ok]), ok


def test_excluded_rows_go_to_the_end_and_are_kept(ranked):
    df = ranked.table
    flagged = df["exclude_reason"].notna()
    assert flagged.any()
    first_excluded = flagged.to_numpy().argmax()
    assert flagged.iloc[first_excluded:].all()  # no clean row after an excluded one
    assert list(df["rank"]) == list(range(1, len(df) + 1))
    for part in (df[~flagged], df[flagged]):
        assert list(part["discount_pct"]) == sorted(part["discount_pct"], reverse=True)
    assert ranked.counts["excluded"]["check_area"] == 2


def test_below_all_recent_is_information_only(ranked):
    df = ranked.table.set_index("listing_id")
    assert bool(df.loc["LOWEST", "below_all_recent"])
    assert pd.isna(df.loc["LOWEST", "exclude_reason"])
    assert not bool(df.loc["DEAR", "below_all_recent"])


def test_same_flat_listed_twice_on_one_site_is_one_row(ranked):
    df = ranked.table.set_index("listing_id")
    # SAME2 has no flat: it matches SAME1 on building, floor band, area (1%) and price (1%)
    assert "SAME2" not in df.index
    assert "centanet:SAME2" in df.loc["SAME1", "also_listed"]
    # different flats of one size at one price stay two rows
    assert {"DIFF1", "DIFF2"} <= set(df.index)


def _frame(**cols):
    base = {"building_id": "b", "flat": None, "area_sqft": 500.0, "asking_psf": 10_000.0,
            "features": None, "floor_text": None, "estate_name": "某苑", "estate_key": "e",
            "district": "D", "match_method": "exact", "fair_psf": 10_000.0, "comps_24m": 20,
            "comps_median_psf": 10_000.0, "rvd_class": "B"}
    base.update(cols)
    return pd.DataFrame([base])


def _deals(n_areas=4, psf=10_000.0):
    rows = [{"building_id": "b", "flat": "ABCDE"[i], "area_sqft": 400.0 + 100 * i,
             "area_basis": "saleable", "psf_hkd": psf, "exclude_reason": None,
             "agreement_date": pd.Timestamp("2026-06-01"), "rvd_class": "B",
             "estate_key": "e", "district": "D"} for i in range(n_areas)]
    return pd.DataFrame(rows)


def _check(li, deals, subsidised=frozenset()):
    return rank.listing_checks(li, deals, pd.DataFrame(), TODAY, subsidised=subsidised)


def test_check_model_needs_five_comps_and_fair_over_1_25x():
    d = _deals()
    assert _check(_frame(fair_psf=12_600.0, comps_24m=5), d)["exclude_reason"][0] == "check_model"
    assert pd.isna(_check(_frame(fair_psf=12_400.0, comps_24m=5), d)["exclude_reason"][0])
    assert pd.isna(_check(_frame(fair_psf=15_000.0, comps_24m=4), d)["exclude_reason"][0])


def test_area_band_and_nearest_area_rules():
    d = _deals()  # recorded areas 400, 500, 600, 700
    assert _check(_frame(area_sqft=149.0), d)["exclude_reason"][0] == "check_area"
    assert _check(_frame(area_sqft=5001.0), d)["exclude_reason"][0] == "check_area"
    assert _check(_frame(area_sqft=1050.0), d)["exclude_reason"][0] == "check_area"  # 1.5x of 700
    assert pd.isna(_check(_frame(area_sqft=1050.0, features="天台"), d)["exclude_reason"][0])
    assert pd.isna(_check(_frame(area_sqft=1050.0), _deals(n_areas=2))["exclude_reason"][0])

def test_subsidised_names_and_the_stop_gap_flag():
    d = _deals()
    assert _check(_frame(estate_name="銀蔚苑"), d, subsidised={"銀蔚苑"})[
        "exclude_reason"][0] == "subsidised"
    assert "銀蔚苑|離島區" in rank.subsidised_estates()
    # estate median under 0.7x its district's, 3+ deals, name ends 苑: stop-gap flag
    cheap = _deals(psf=5_000.0).assign(estate_key="e")
    other = _deals(psf=10_000.0).assign(estate_key="f", building_id="c")
    deals = pd.concat([cheap, other])
    assert _check(_frame(), deals)["exclude_reason"][0] == "check_subsidised"
    assert pd.isna(_check(_frame(estate_name="某大廈"), deals)["exclude_reason"][0])


def test_subsidised_list_holds_the_official_names():
    # the real file: HA HOS courts, HA TPS estates, HKHS Flat-for-Sale and Sandwich Class
    keys = rank.subsidised_estates()
    assert {"銀蔚苑|離島區", "東濤苑|東區", "富景花園|東區",      # HOS, PSPS
            "長發邨|葵青區", "翠林邨|西貢區",                   # TPS
            "健康村|東區", "浩景台|葵青區", "疊翠軒|西貢區"} <= keys  # HKHS FFSS, SCHS
    assert len(keys) > 300
    # HKHS dedicated rehousing estates are rental, not subsidised sale
    assert not any("專用安置屋邨" in k for k in keys)


def test_subsidised_match_needs_the_same_district():
    d = _deals()
    keys = frozenset({"富景花園|東區", "浩景台|葵青區", "某邨"})

    def reason(**cols):
        return _check(_frame(**cols), d, subsidised=keys)["exclude_reason"][0]

    assert reason(estate_name="富景花園", district="東區") == "subsidised"
    # the private 富景花園 in 中西區 shares the name: not marked
    assert pd.isna(reason(estate_name="富景花園", district="中西區"))
    # 臺 and 台 are one name; no district on the listing: the name alone decides
    assert reason(estate_name="浩景臺", district="葵青區") == "subsidised"
    assert reason(estate_name="富景花園", district=None) == "subsidised"
    # a list row with no district matches any district
    assert reason(estate_name="某邨", district="北區") == "subsidised"


def test_check_match_is_not_a_listing_check():
    # match_building returns no building for a 'check_match', so such a listing never reaches
    # the ranking (it is "no building match"); the reason is not in the list
    assert "check_match" not in rank.CHECK_ORDER
    assert pd.isna(_check(_frame(match_method="check_match"), _deals())["exclude_reason"][0])


def _dups(rows):
    base = {"_excluded": False, "building_id": "b", "area_sqft": 500.0, "asking_hkd": 5e6,
            "flat": None, "floor_band": "unknown", "also_listed": None}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_a_folded_row_carries_its_own_also_listed_keys():
    li = _dups([{"key": "a", "asking_hkd": 5.0e6, "flat": "B", "floor_band": "high"},
                {"key": "b", "asking_hkd": 5.01e6, "flat": "B", "floor_band": "high",
                 "also_listed": "x:1, a"}])
    kept = rank._collapse_same_flat(li)
    assert list(kept["key"]) == ["a"]
    # b's own list ("x:1, a") comes along, without the kept row's own key and without repeats
    assert kept["also_listed"].iloc[0].split(", ") == ["b", "x:1"]


def test_blank_flat_needs_same_band_and_one_percent():
    same = {"flat": None, "floor_band": "mid"}
    # blank flat, same band, within 1%: merged
    kept = rank._collapse_same_flat(_dups([
        {"key": "a", "flat": "E", "floor_band": "mid"},
        {"key": "b", "asking_hkd": 5.04e6, "area_sqft": 502.0, **same}]))
    assert list(kept["key"]) == ["a"]
    # blank band on the blank-flat side, or a 1.5% price gap: kept apart
    for other in ({"floor_band": "unknown", "flat": None},
                  {"asking_hkd": 5.075e6, **same}, {"area_sqft": 508.0, **same}):
        kept = rank._collapse_same_flat(_dups([
            {"key": "a", "flat": "E", "floor_band": "mid"}, {"key": "b", **{**same, **other}}]))
        assert len(kept) == 2, other
    # both flats named and equal: the old 2% rule still holds
    kept = rank._collapse_same_flat(_dups([
        {"key": "a", "flat": "E"}, {"key": "b", "flat": "E", "asking_hkd": 5.09e6}]))
    assert len(kept) == 1


def test_one_row_absorbs_at_most_three_blank_flat_rows():
    rows = [{"key": "a", "flat": "E", "floor_band": "mid"}]
    rows += [{"key": f"z{i}", "flat": None, "floor_band": "mid"} for i in range(6)]
    kept = rank._collapse_same_flat(_dups(rows))
    assert kept[kept["key"] == "a"]["also_listed"].iloc[0].count("z") == 3


def test_floor_text_never_says_band_band():
    def text(basis, band, num=12.0):
        return rank._floor_text({"floor_basis": basis, "floor_band": band, "floor_num": num})
    assert text("exact", "mid") == "12/F"
    assert text("band", "high") == "high band"
    assert text("band", "unknown") == "not given"
    assert text("none", "unknown") == "not given"


def test_gross_area_band_for_a_flat_letter_and_without_one():
    d = _deals()  # flat A 400, B 500, C 600, D 700
    assert _check(_frame(area_sqft=600.0, flat="B"), d)["exclude_reason"][0] == (
        "check_area_basis")  # 600 / 500 = 1.20
    assert pd.isna(_check(_frame(area_sqft=510.0, flat="B"), d)["exclude_reason"][0])
    # no flat letter: against the nearest recorded area (840 / 700 = 1.20)
    assert _check(_frame(area_sqft=840.0), d)["exclude_reason"][0] == "check_area_basis"


def test_gap_assumed_flags_and_confidence(ranked):
    df = ranked.table.set_index("listing_id")
    assert (df["gap_pct"] - 0.05).abs().max() < 1e-9
    assert set(df["gap_basis"]) == {"assumed"}
    assert df.loc["CHEAP", "flags"] == "tenanted"
    assert "with_parking" in df.loc["DEAR", "flags"]
    assert df.loc["FEW", "confidence"] == "low"  # its 6 deals are first-hand, none comparable
    assert df.loc["CHEAP", "confidence"] == "normal"
    assert ranked.counts["not_valued"] == {"no building match": 1}


def test_shortlist_is_clean_normal_confidence_rows_only(ranked):
    top, left_out = rank.shortlist(ranked.table)
    assert len(top) <= rank.SHORTLIST_SIZE
    assert top["exclude_reason"].isna().all() and (top["confidence"] == "normal").all()
    assert list(top["discount_pct"]) == sorted(top["discount_pct"], reverse=True)
    assert {"CHEAP", "DEAR"} <= set(top["listing_id"])
    assert not {"TYPO", "TOOHIGH", "FEW"} & set(top["listing_id"])  # FEW: low confidence
    assert left_out["check_area"] == 2 and left_out["low confidence"] >= 1
    assert len(rank.shortlist(ranked.table, n=2)[0]) == 2


def test_shortlist_file_is_plain_english_with_every_field(ranked):
    text = rank.shortlist_markdown(ranked.table, TODAY)
    assert text.startswith("# Shortlist 2026-10-17")
    head = text.split("## 1.")[0]
    for word in ("Discount", "Fair range", "Reasons", "Below all recent", "Also listed",
                 "Left out"):
        assert word in head, word
    entry = text.split("## 1.")[1].split("## 2.")[0]
    for part in ("Flat:", "Asking HK$", "fair HK$", "Discount:", "Reasons:", "Flags:",
                 "below all recent:", "also listed:", "http"):
        assert part in entry, part
    assert "spacious:CHEAP-DUP" in text  # CHEAP's also_listed
    assert "TYPO" not in text


def test_write_makes_the_dated_shortlist(tmp_path, ranked):
    paths = rank.write(ranked, today=TODAY)
    assert paths["shortlist"] == tmp_path / "reports" / "shortlist_20261017.md"
    assert paths["shortlist"].read_text(encoding="utf-8").startswith("# Shortlist")


def test_rank_command_writes_dated_csv(tmp_path, monkeypatch, ranked):
    # the fixture trained a model in tmp_path; the command snapshots and ranks again
    assert cli.main(["--db", str(tmp_path / "hkestate.duckdb"), "rank"]) == 0
    out = list((tmp_path / "reports").glob("ranked_listings_*.csv"))
    assert len(out) == 1
    df = pd.read_csv(out[0], encoding="utf-8-sig")
    assert len(df) == len(ranked.table)
    assert (tmp_path / "model" / "ranked_latest.parquet").exists()
    con = db.connect(tmp_path / "hkestate.duckdb")
    status = con.execute("SELECT status FROM run_log WHERE command = 'rank'").fetchall()
    con.close()
    assert status == [("ok",)]
