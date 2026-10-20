"""Centadata per-flat sale history: flat codes, batching, paging, storing, resuming.

Everything here is synthetic: invented flat codes, prices and estate names.
"""

import json
from datetime import date, datetime

import pytest

from hkestate import db, locks
from hkestate.fetch import FetchError, SourceBlocked
from hkestate.sources import centadata_history as ch


def master_row(tid, code, bid, eid, floor, flat, day, price=5_000_000):
    """A latest-deal row as the master pass / cross-check stores it."""
    return {
        "source": "centadata", "source_id": tid, "fetched_at": datetime(2026, 10, 17, 9, 0),
        "estate_id": eid, "building_id": bid, "estate_name": "測試苑", "phase_name": "",
        "building_name": bid[-2:] + "座", "floor_label": floor, "flat": flat, "deal_date": day,
        "instrument_date": day, "registration_date": day, "deal_type": "registry",
        "price_hkd": price, "raw_json": json.dumps({"transID": tid, "cuntcode": code}),
    }


def api_row(tid, code, ins, price, *, gain=None, reg=None, extra=None):
    row = {
        "id": tid, "cuntcode": code, "insDate": f"{ins}T00:00:00",
        "regDate": f"{reg or ins}T00:00:00", "transactionPrice": float(price),
        "nArea": 500, "gArea": 600, "nUnitPrice": 1, "gUnitPrice": 1,
        "yAxis": "5樓", "xAxis": "A室", "buildingName": "測試閣", "estateName": "測試苑",
        "bigEstateName": "", "firstOrSecondHand": "SecondHand", "dataSource": "Land",
        # fields that must not be kept
        "picture": {"thumbnailPath": "x"}, "floorPlans": ["y"], "mediaInfo": {"hasMedia": True},
        "scope": {"hmaDesc": "text"}, "detailUrl": "z",
    }
    if gain is not None:
        row.update(gainPercent=gain, heldDay=100, prevInsDate="2000-01-01T00:00:00",
                   prevRegDate="2000-01-20T00:00:00", prevTransactionPrice=1)
    row.update(extra or {})
    return row


class FakeFetcher:
    """Answers Transaction/Search from a table of rows per flat code, with real paging."""

    def __init__(self, sales, fail=None):
        self.sales = sales  # code -> list of api rows
        self.calls = []
        self.fail = fail or (lambda n: None)

    def post_json(self, url, body, page_type):
        self.calls.append(body)
        err = self.fail(len(self.calls))
        if err:
            raise err
        rows = [r for c in body["cuntcodes"] for r in self.sales.get(c, [])]
        rows.sort(key=lambda r: r["insDate"] or "", reverse=True)
        page = rows[body.get("offset", 0): body.get("offset", 0) + body["size"]]
        return json.dumps({"count": len(rows), "data": page})


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    # lock files under tmp_path: a real listing job's lock must not pause these tests
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
        # a pilot estate (group) and a territory estate
        con.execute("INSERT INTO estate (estate_id, name_zh, group_name, source, source_key) "
                    "VALUES ('h28-1', '甲苑', 'alpha', 'hse28', '1'), "
                    "('cd-2-BIG', '乙苑', NULL, 'centadata', '2-BIG'), "
                    "('cd-2-SMALL', '丙苑', NULL, 'centadata', '2-SMALL')")
        rows = [
            # alpha: one building, 2 flats
            master_row("t1", "CODEA1", "h28-1-b1", "h28-1", "5", "A", date(2011, 3, 4)),
            master_row("t2", "CODEA2", "h28-1-b1", "h28-1", "5", "B", date(2012, 3, 4)),
            # big estate: two buildings, 3 + 1 flats
            master_row("t3", "CODEB1", "cd-1-B1", "cd-2-BIG", "1", "A", date(2015, 1, 1)),
            master_row("t4", "CODEB2", "cd-1-B1", "cd-2-BIG", "1", "B", date(2015, 1, 2)),
            master_row("t5", "CODEB3", "cd-1-B1", "cd-2-BIG", "2", "A", date(2015, 1, 3)),
            master_row("t6", "CODEB4", "cd-1-B2", "cd-2-BIG", "1", "A", date(2015, 1, 4)),
            # small estate: 1 flat
            master_row("t7", "CODES1", "cd-1-S1", "cd-2-SMALL", "3", "C", date(2016, 6, 1)),
            # a Land Registry deal from another source is not a flat-code source
            {**master_row("h1", "X", "h28-1-b1", "h28-1", "5", "A", date(2019, 1, 1)),
             "source": "hse28", "raw_json": "{}"},
        ]
        db.insert_txn_raw(con, rows)
    return path


# ---- parsing and rows ---------------------------------------------------------------------


def test_parse_response_reads_count_and_rows():
    count, rows = ch.parse_response(json.dumps({"count": 3, "data": [{"id": "1"}]}))
    assert count == 3 and rows == [{"id": "1"}]


@pytest.mark.parametrize("text", ["not json", "[]", '{"count": 1}', '{"data": 5, "count": 1}'])
def test_parse_response_rejects_other_shapes(text):
    with pytest.raises(ValueError):
        ch.parse_response(text)


def test_keep_only_the_listed_fields_and_no_personal_data():
    kept = ch.keep_fields(api_row("1", "C", "2001-01-01", 1, gain=5))
    assert set(kept) <= set(ch.KEEP_FIELDS)
    assert {"id", "insDate", "regDate", "transactionPrice", "gainPercent", "prevInsDate",
            "heldDay", "cuntcode"} <= set(kept)
    for dropped in ("picture", "floorPlans", "mediaInfo", "scope", "detailUrl"):
        assert dropped not in kept


# ---- flat codes and order ----------------------------------------------------------------


def test_flats_group_by_building_and_estates_run_largest_first(db_path):
    with db.session(db_path) as con:
        estates = ch.load_flats(con)
    assert [e.estate_id for e in estates] == ["cd-2-BIG", "h28-1", "cd-2-SMALL"]
    big = estates[0]
    assert [(b.building_id, len(b.flats)) for b in big.buildings] == [
        ("cd-1-B1", 3), ("cd-1-B2", 1)]
    flat = big.buildings[0].flats[0]
    assert flat.code.startswith("CODEB") and flat.estate_id == "cd-2-BIG"
    assert (flat.floor_label, flat.flat) in {("1", "A"), ("1", "B"), ("2", "A")}


def test_only_named_groups_are_loaded(db_path):
    with db.session(db_path) as con:
        estates = ch.load_flats(con, ["alpha"])
    assert [e.estate_id for e in estates] == ["h28-1"]
    with db.session(db_path) as con:
        assert [e.estate_id for e in ch.load_flats(con, ["pilot"])] == ["h28-1"]


def test_quarantined_rows_are_not_used_for_placement(db_path):
    with db.session(db_path) as con:
        con.execute("INSERT INTO estate (estate_id, name_zh, group_name, source, source_key) "
                    "VALUES ('h28-9', 'LP', 'lohas-park', 'hse28', '9')")
        bad = master_row("q1", "CODEQ", "h28-9-b1", "h28-9", "1", "A", date(2015, 1, 1))
        bad["fetched_at"] = datetime(2026, 10, 16, 19, 30)
        db.insert_txn_raw(con, [bad])
        codes = {f.code for e in ch.load_flats(con) for b in e.buildings for f in b.flats}
    assert "CODEQ" not in codes


# ---- the run ---------------------------------------------------------------------------------


def sales():
    return {
        "CODEA1": [api_row("t1", "CODEA1", "2011-03-04", 5_860_000, gain=73),
                   api_row("h1", "CODEA1", "1996-09-20", 3_390_000)],
        "CODEA2": [api_row("t2", "CODEA2", "2012-03-04", 5_000_000, gain=10)],
        "CODEB1": [api_row("b1a", "CODEB1", "2015-01-01", 4_000_000),
                   api_row("b1b", "CODEB1", "2005-01-01", 2_000_000)],
        "CODEB2": [api_row("b2a", "CODEB2", "2015-01-02", 4_100_000)],
        "CODEB3": [api_row("b3a", "CODEB3", "2015-01-03", 4_200_000)],
        "CODEB4": [api_row("b4a", "CODEB4", "2015-01-04", 4_300_000),
                   api_row("b4b", "CODEB4", "2001-05-05", 1_300_000)],
        "CODES1": [api_row("s1a", "CODES1", "2016-06-01", 3_000_000)],
    }


def run(db_path, fetcher, **kw):
    return ch.run_history(fetcher, db_path=db_path, log=lambda m: None, wait=lambda s: None, **kw)


def stored(db_path, source_id):
    with db.session(db_path) as con:
        return con.execute(
            "SELECT building_id, estate_id, floor_label, flat, deal_date, instrument_date, "
            "registration_date, price_hkd, saleable_sqft, gain_pct, raw_json, deal_type, "
            "first_hand FROM txn_raw WHERE source = 'centadata' AND source_id = ?",
            [source_id]).fetchall()


def test_old_sale_is_stored_on_the_same_unit_as_the_latest_deal(db_path):
    stats = run(db_path, FakeFetcher(sales()))
    assert stats["errors"] == 0
    rows = stored(db_path, "h1")
    assert len(rows) == 1
    (bid, eid, floor, flat, deal, ins, reg, price, area, gain, raw, deal_type, first) = rows[0]
    assert (bid, eid, floor, flat) == ("h28-1-b1", "h28-1", "5", "A")
    assert deal == date(1996, 9, 20) == ins == reg
    assert price == 3_390_000 and area == 500 and deal_type == "registry" and not first
    assert set(json.loads(raw)) <= set(ch.KEEP_FIELDS)
    assert json.loads(raw)["cuntcode"] == "CODEA1"


def test_rows_already_stored_are_left_alone(db_path):
    run(db_path, FakeFetcher(sales()))
    with db.session(db_path) as con:
        n = con.execute("SELECT count(*) FROM txn_raw WHERE source_id = 't1'").fetchone()[0]
        assert n == 1  # the master pass's copy, not duplicated or versioned
        assert con.execute("SELECT raw_json FROM txn_raw WHERE source_id = 't1'"
                           ).fetchone()[0] == json.dumps({"transID": "t1", "cuntcode": "CODEA1"})


def test_every_new_sale_is_added_once(db_path):
    stats = run(db_path, FakeFetcher(sales()))
    # new: h1, b1a, b1b, b2a, b3a, b4a, b4b, s1a  (t1, t2 already stored)
    assert stats["deals"] == 8
    with db.session(db_path) as con:
        n = con.execute("SELECT count(*) FROM txn_raw WHERE source = 'centadata'").fetchone()[0]
    assert n == 7 + 8


def test_run_is_resumable_and_a_rerun_does_nothing(db_path):
    first = FakeFetcher(sales())
    run(db_path, first)
    again = FakeFetcher(sales())
    stats = run(db_path, again)
    assert again.calls == [] and stats["deals"] == 0 and stats["buildings"] == 0


def test_limit_counts_buildings_and_the_next_run_continues(db_path):
    f1 = FakeFetcher(sales())
    s1 = run(db_path, f1, limit=2)
    assert s1["buildings"] == 2
    sent = {c for call in f1.calls for c in call["cuntcodes"]}
    assert sent == {"CODEB1", "CODEB2", "CODEB3", "CODEB4"}  # the biggest estate first
    f2 = FakeFetcher(sales())
    s2 = run(db_path, f2)
    assert s2["buildings"] == 2
    sent2 = {c for call in f2.calls for c in call["cuntcodes"]}
    assert sent2 == {"CODEA1", "CODEA2", "CODES1"}


def test_named_estate_only(db_path):
    f = FakeFetcher(sales())
    run(db_path, f, estates=["alpha"])
    assert {c for call in f.calls for c in call["cuntcodes"]} == {"CODEA1", "CODEA2"}
    assert stored(db_path, "b1a") == []


def test_batches_hold_at_most_batch_size_codes_and_cross_buildings(db_path, monkeypatch):
    monkeypatch.setattr(ch, "BATCH_CODES", 2)
    f = FakeFetcher(sales())
    run(db_path, f)
    assert max(len(c["cuntcodes"]) for c in f.calls) == 2
    big = [c["cuntcodes"] for c in f.calls if c["cuntcodes"][0].startswith("CODEB")]
    assert [len(c) for c in big] == [2, 2]
    assert "CODEB4" in big[1]  # building 2's flat rides in the second batch with building 1's

def test_all_pages_of_a_batch_are_read(db_path, monkeypatch):
    monkeypatch.setattr(ch, "PAGE_SIZE", 2)
    f = FakeFetcher(sales())
    run(db_path, f, estates=["alpha"])  # 3 sales, page size 2
    assert [c["offset"] for c in f.calls] == [0, 2]
    assert all(c["size"] == 2 for c in f.calls)
    assert len(stored(db_path, "h1")) == 1


def test_sale_without_date_or_price_or_unknown_flat_is_skipped(db_path):
    s = sales()
    s["CODEA2"] = [api_row("bad1", "CODEA2", "2001-01-01", 0),
                   {**api_row("bad2", "CODEA2", "2001-01-01", 5), "insDate": None, "regDate": None},
                   api_row("bad3", "NOCODE", "2001-01-01", 5)]
    run(db_path, FakeFetcher(s), estates=["alpha"])
    assert stored(db_path, "bad1") == stored(db_path, "bad2") == stored(db_path, "bad3") == []


def test_only_instrument_date_missing_uses_registration_date(db_path):
    s = sales()
    s["CODEA2"] = [{**api_row("r1", "CODEA2", "2001-02-03", 5_000_000), "insDate": None}]
    run(db_path, FakeFetcher(s), estates=["alpha"])
    row = stored(db_path, "r1")[0]
    assert row[4] == date(2001, 2, 3) and row[5] is None and row[6] == date(2001, 2, 3)


def test_errors_are_counted_and_the_failed_batch_is_retried_next_run(db_path):
    err = FetchError("u", 404, "gone")  # not throttling: the batch is left for the next run
    f = FakeFetcher(sales(), fail=lambda n: err if n == 1 else None)
    stats = run(db_path, f, estates=["alpha"])
    assert stats["errors"] == 1 and stats["buildings"] == 0
    stats2 = run(db_path, FakeFetcher(sales()), estates=["alpha"])
    assert stats2["buildings"] == 1 and stats2["errors"] == 0
    assert len(stored(db_path, "h1")) == 1


def test_a_failed_batch_keeps_its_building_unfinished_even_if_later_batches_work(
        db_path, monkeypatch):
    monkeypatch.setattr(ch, "BATCH_CODES", 1)
    err = FetchError("u", 404, "gone")
    f = FakeFetcher(sales(), fail=lambda n: err if n == 1 else None)
    stats = run(db_path, f, estates=["alpha"])  # batch 1 (CODEA1) fails, batch 2 works
    assert stats["buildings"] == 0 and stats["errors"] == 1
    with db.session(db_path) as con:
        assert not db.step_done(con, ch.COMMAND, "h28-1-b1")


def test_five_non_throttle_errors_in_a_row_stop_the_run_and_are_logged(db_path, monkeypatch):
    monkeypatch.setattr(ch, "BATCH_CODES", 1)
    err = FetchError("u", 404, "gone")
    f = FakeFetcher(sales(), fail=lambda n: err)
    with pytest.raises(RuntimeError, match="5 errors in a row"):
        run(db_path, f)
    assert len(f.calls) == 5
    with db.session(db_path) as con:
        status, msg = con.execute(
            "SELECT status, message FROM run_log WHERE command = 'cd-history'").fetchone()
    assert status == "failed" and "5 errors" in msg


@pytest.mark.parametrize("status", [429, 503])
def test_429s_do_not_stop_the_run_it_cools_down_and_retries_the_same_batch(
        db_path, monkeypatch, status):
    """2026-10-20: five 429s in a row ended the run. Now it waits and carries on."""
    monkeypatch.setattr(ch, "BATCH_CODES", 1)
    err = FetchError("u", status, "slow down")
    f = FakeFetcher(sales(), fail=lambda n: err if n <= 12 else None)
    waits, logs = [], []
    stats = ch.run_history(f, db_path=db_path, log=logs.append, wait=waits.append)
    assert stats["buildings"] == 4 and stats["deals"] == 8  # nothing lost
    assert f.calls[0] == f.calls[12]  # the same batch was asked again until it worked
    assert waits[:4] == [600, 1200, 1800, 1800]  # cool-downs (no fetcher cool-down here)
    assert any("cooling down 10 min" in m for m in logs)


def test_waits_out_the_fetchers_own_cool_down_without_sleeping_again(db_path, monkeypatch):
    monkeypatch.setattr(ch, "BATCH_CODES", 1)
    err = FetchError("https://hk.centanet.com/x", 429, "slow down")
    f = FakeFetcher(sales(), fail=lambda n: err if n <= 3 else None)
    f.cooldown_remaining = lambda url: 900.0  # the real fetcher waits before the next request
    waits, logs = [], []
    stats = ch.run_history(f, db_path=db_path, log=logs.append, wait=waits.append)
    assert stats["buildings"] == 4 and waits == []
    assert sum("waiting out the site's cool-down" in m for m in logs) == 3


def test_a_batch_failing_with_500_is_tried_three_times_then_skipped(db_path, monkeypatch):
    monkeypatch.setattr(ch, "BATCH_CODES", 1)
    err = FetchError("u", 500, "boom")
    f = FakeFetcher(sales(), fail=lambda n: err if n <= 3 else None)
    stats = run(db_path, f)
    assert f.calls[0] == f.calls[1] == f.calls[2] != f.calls[3]
    assert stats["errors"] == 3 and stats["buildings"] == 3  # its building waits for a rerun


def test_a_success_resets_the_error_streak(db_path, monkeypatch):
    monkeypatch.setattr(ch, "BATCH_CODES", 1)
    err = FetchError("u", 404, "gone")
    bad = {1, 2, 3, 4, 6, 7}  # never 5 in a row
    f = FakeFetcher(sales(), fail=lambda n: err if n in bad else None)
    stats = run(db_path, f)
    assert stats["errors"] == 6


def test_blocked_source_stops_the_run(db_path):
    f = FakeFetcher(sales(), fail=lambda n: SourceBlocked("403"))
    with pytest.raises(SourceBlocked):
        run(db_path, f)
    with db.session(db_path) as con:
        assert con.execute("SELECT status FROM run_log WHERE command = 'cd-history'"
                           ).fetchone()[0] == "failed"


def test_run_log_row_is_written_on_success(db_path):
    run(db_path, FakeFetcher(sales()))
    with db.session(db_path) as con:
        status, rows, msg = con.execute(
            "SELECT status, rows_added, message FROM run_log WHERE command = 'cd-history'"
        ).fetchone()
    assert status == "ok" and rows == 8 and json.loads(msg)["buildings"] == 4


def test_max_hours_stops_between_batches(db_path):
    clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0])
    f = FakeFetcher(sales())
    stats = ch.run_history(f, db_path=db_path, log=lambda m: None, wait=lambda s: None,
                           max_hours=1.0, clock=lambda: next(clock, 10_000.0))
    assert stats["stopped"] == "max-hours"
    assert len(f.calls) < 4


def test_runs_alongside_centanet_listings_without_pausing(db_path, monkeypatch):
    # since 2026-10-20 both run at once, each at its own pace (no pauses on the lock)
    monkeypatch.setattr(locks, "held_by",
                        lambda name: 1234 if name in ("centanet", "listings") else None)
    waits = []
    f = FakeFetcher(sales())
    stats = ch.run_history(f, db_path=db_path, log=lambda m: None, wait=waits.append,
                           estates=["alpha"])
    assert waits == [] and f.calls and stats["buildings"] == 1
