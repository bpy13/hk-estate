"""`hkestate rebuild`: order, resume, to-do reruns, deferred steps, subsets, request cap.

Every step function is a fake: no network, no real command runs.
"""

import json

import pytest

from hkestate import cli, db, fetch, locks, rebuild
from hkestate.rebuild import Context, Step


@pytest.fixture
def dbp(tmp_path):
    p = tmp_path / "t.duckdb"
    with db.session(p) as con:
        db.init_db(con)
    return p


class Recorder:
    """Fake step functions: record the call order; to-do counts from a script per step."""

    def __init__(self, todo=None):
        self.calls: list[str] = []
        self.todo = todo or {}  # step name -> list of counts, one per try

    def step(self, name, **kw):
        def run(ctx):
            self.calls.append(name)
            return 0

        def todo(ctx):
            return self.todo[name].pop(0)

        return Step(name, run, todo if name in self.todo else None, **kw)


def run(dbp, plan, logs=None, **kw):
    return rebuild.rebuild(dbp, log=(logs.append if logs is not None else lambda m: None),
                           plan=plan, **kw)


def done_keys(dbp):
    with db.session(dbp) as con:
        return {r[0] for r in con.execute(
            "SELECT step_key FROM run_progress WHERE command = 'rebuild'").fetchall()}


# ---- order and resume ------------------------------------------------------------------------


def test_steps_run_in_order_and_are_recorded(dbp):
    r = Recorder()
    plan = [r.step(n) for n in ("a", "b", "c")]
    assert run(dbp, plan) == rebuild.EXIT_OK
    assert r.calls == ["a", "b", "c"]
    assert done_keys(dbp) == {"a", "b", "c"}


def test_a_rerun_skips_the_steps_already_done(dbp):
    r = Recorder()
    run(dbp, [r.step("a"), r.step("b")])
    again = Recorder()
    assert run(dbp, [again.step("a"), again.step("b"), again.step("c")]) == rebuild.EXIT_OK
    assert again.calls == ["c"]


def test_the_real_plan_is_audit_steps_1_to_16_in_order():
    names = [s.name for s in rebuild.steps()]
    assert names == ["rvd", "backfill", "crosscheck", "master", "backfill-territory",
                     "link-ids", "cd-history", "srpe", "ricacorp", "geo", "schools", "daily",
                     "train", "rank", "report", "datacheck"]
    by = {s.name: s for s in rebuild.steps()}
    assert "crosscheck" not in by["master"].depends  # R14: the order rule is gone
    assert set(rebuild.DATA_STEPS) == set(by["train"].depends)
    assert by["link-ids"].depends == ("master", "backfill-territory")  # R8


# ---- done means to-do 0 ------------------------------------------------------------------------


def test_a_step_with_to_do_left_is_rerun_until_it_is_zero(dbp):
    r = Recorder(todo={"b": [5, 0]})
    assert run(dbp, [r.step("a"), r.step("b"), r.step("c")]) == rebuild.EXIT_OK
    assert r.calls == ["a", "b", "b", "c"]


def test_a_step_still_with_to_do_after_three_tries_holds_up_only_its_dependants(dbp):
    r, logs = Recorder(todo={"b": [5, 4, 3]}), []
    plan = [r.step("a"), r.step("b", what="estates"), r.step("c", depends=("b",)), r.step("d")]
    assert run(dbp, plan, logs) == rebuild.EXIT_STOPPED
    assert r.calls == ["a", "b", "b", "b", "d"]  # c waits for b; d goes on
    assert done_keys(dbp) == {"a", "d"}
    assert any("b: failed, still 3 estates to do after 3 tries" in m for m in logs)
    assert any("c (waits for b)" in m for m in logs)


def test_an_unknown_to_do_count_is_not_done(dbp):
    r = Recorder(todo={"a": [None, None, None]})
    assert run(dbp, [r.step("a")]) == rebuild.EXIT_STOPPED


def test_a_failing_try_counts_as_one_try(dbp):
    tries = []

    def flaky(ctx):
        tries.append(1)
        if len(tries) == 1:
            raise RuntimeError("8 errors in a row; stopping (rerun)")
        return 0

    assert run(dbp, [Step("a", flaky)]) == rebuild.EXIT_OK
    assert len(tries) == 2


def test_a_step_without_a_to_do_count_is_done_when_it_exits_normally(dbp):
    codes = iter([1, 1, 1])
    assert run(dbp, [Step("a", lambda ctx: next(codes))]) == rebuild.EXIT_STOPPED


# ---- host locks --------------------------------------------------------------------------------


def test_a_held_lock_defers_the_step_and_its_dependants_only(dbp, monkeypatch):
    monkeypatch.setattr(locks, "held_by", lambda name: 4321 if name == "hse28" else None)
    r, logs = Recorder(), []
    plan = [r.step("a"), r.step("b", locks=("hse28",)), r.step("c", depends=("b",)),
            r.step("d", depends=("a",)), r.step("e", depends=("c",))]
    assert run(dbp, plan, logs) == rebuild.EXIT_DEFERRED
    assert r.calls == ["a", "d"]
    last = [m for m in logs if m.startswith("deferred (3)")]
    assert last and "b (lock 'hse28' held by process 4321)" in last[0]
    assert "c (waits for b)" in last[0] and "e (waits for c)" in last[0]
    # the lock is free again: a rerun picks up the deferred steps only
    monkeypatch.setattr(locks, "held_by", lambda name: None)
    again = Recorder()
    plan = [again.step(n) for n in "abcde"]
    assert run(dbp, plan) == rebuild.EXIT_OK
    assert again.calls == ["b", "c", "e"]


def test_a_step_the_subset_skips_does_not_hold_up_its_dependants(dbp, monkeypatch):
    # a subset without pilot estates: backfill holds a busy lock but is skipped anyway, so
    # cross-check and cd-history (which wait for it) still run (2-estate test run, 2026-10-28)
    monkeypatch.setattr(locks, "held_by", lambda name: 4321 if name == "hse28" else None)
    r = Recorder()
    out = lambda ctx: "not in the subset"  # noqa: E731
    plan = [r.step("backfill", locks=("hse28",), skip=out),
            r.step("crosscheck", depends=("backfill",), skip=out),
            r.step("cd-history", depends=("crosscheck",))]
    assert run(dbp, plan) == rebuild.EXIT_OK
    assert r.calls == ["cd-history"]


def test_a_second_rebuild_is_not_started(dbp, monkeypatch):
    monkeypatch.setattr(locks, "held_by", lambda name: 99 if name == "rebuild" else None)
    r = Recorder()
    assert run(dbp, [r.step("a")]) == rebuild.EXIT_BUSY
    assert r.calls == []


# ---- request cap -------------------------------------------------------------------------------


def test_the_request_cap_stops_the_run_cleanly(dbp):
    def greedy(ctx):
        for _ in range(10):
            fetch.spend_request()
        return 0

    r, logs = Recorder(), []
    code = run(dbp, [r.step("a"), Step("b", greedy), r.step("c")], logs, max_requests=4)
    assert code == rebuild.EXIT_CAP
    assert r.calls == ["a"]
    assert any("request cap of 4 reached during step b (4 requests sent)" in m for m in logs)
    assert done_keys(dbp) == {"a"}
    with db.session(dbp) as con:
        assert con.execute("SELECT status FROM run_log WHERE command = 'rebuild'"
                           ).fetchone() == ("stopped",)
    fetch.spend_request()  # the cap is lifted once the rebuild ends


# ---- the real steps, with fake commands ----------------------------------------------------------


@pytest.fixture
def fake_commands(monkeypatch):
    """Every command call is recorded instead of run; every to-do count is 0."""
    called: list[list[str]] = []
    monkeypatch.setattr(rebuild, "_cli", lambda ctx, *argv: called.append(list(argv)) or 0)
    monkeypatch.setattr(rebuild, "_module",
                        lambda ctx, module, *argv: called.append([module, *argv]) or 0)
    for name in dir(rebuild):
        if name.startswith("todo_"):
            monkeypatch.setattr(rebuild, name, lambda ctx: 0)
    return called


def test_estates_reach_every_step_that_takes_a_subset(dbp, fake_commands):
    assert run(dbp, None, estates="太古城,爾巒") == rebuild.EXIT_OK
    by_first = {argv[0]: argv for argv in fake_commands}
    subset = ["--estates", "太古城,爾巒"]
    for cmd in ("master", "link-ids", "cd-history", "ricacorp", "hkestate.sources.srpe",
                "hkestate.geo"):
        assert by_first[cmd][-2:] == subset, cmd
    # R9: the fresh 28Hse index is empty, so the territory run discovers it first
    assert by_first["backfill-territory"] == ["backfill-territory", "--discover", *subset]
    # pilot-only steps take the subset's pilot estates
    assert by_first["backfill"] == ["backfill", "--estates", "taikoo-shing"]
    assert by_first["crosscheck"] == ["crosscheck", "--estates", "taikoo-shing"]
    assert by_first["listings"] == ["listings", "--estates", "taikoo-shing"]  # not `daily`
    # steps with no subset notion run whole
    assert by_first["rvd"] == ["rvd"] and by_first["train"] == ["train"]
    assert by_first["hkestate.sources.schools"] == ["hkestate.sources.schools"]
    assert [a[0] for a in fake_commands] == [
        "rvd", "backfill", "crosscheck", "master", "backfill-territory", "link-ids",
        "cd-history", "hkestate.sources.srpe", "ricacorp", "hkestate.geo",
        "hkestate.sources.schools", "listings", "train", "rank", "report", "datacheck"]
    assert "rvd|太古城,爾巒" in done_keys(dbp)  # a subset has its own progress keys


def test_a_subset_without_pilot_estates_skips_the_pilot_only_steps(dbp, fake_commands):
    logs = []
    assert run(dbp, None, logs, estates="爾巒") == rebuild.EXIT_OK
    firsts = [a[0] for a in fake_commands]
    assert "backfill" not in firsts and "crosscheck" not in firsts
    assert "daily" not in firsts and "listings" not in firsts
    assert any(m.startswith("backfill: skipped, not in the subset") for m in logs)
    assert any(m.startswith("daily: skipped, not in the subset") for m in logs)
    assert "master" in firsts and "train" in firsts  # a skipped step does not defer others


def test_the_whole_territory_runs_daily_and_the_pilot_steps(dbp, fake_commands):
    assert run(dbp, None) == rebuild.EXIT_OK
    by_first = {argv[0]: argv for argv in fake_commands}
    assert by_first["daily"] == ["daily"]
    assert by_first["backfill"] == ["backfill", "--estates", "pilot"]
    assert by_first["master"] == ["master"]
    assert by_first["report"] == ["report", "--settings", "standard", "--from-snapshot"]
    assert by_first["rank"] == ["rank", "--from-snapshot"]


def test_territory_discovers_only_while_the_index_is_empty(dbp, fake_commands):
    from hkestate import link

    with db.session(dbp) as con:
        link.ensure_tables(con)
        con.execute("INSERT INTO hse28_estate (id, name, found_by) VALUES (1, 'x', 'sitemap')")
    rebuild.run_territory(Context(db=dbp))
    assert fake_commands == [["backfill-territory"]]


def test_gain_pct_is_filled_after_the_cross_check(dbp, fake_commands):  # R14
    with db.session(dbp) as con:
        con.execute("INSERT INTO txn_raw (source, source_id, fetched_at, row_hash, raw_json) "
                    "VALUES ('centadata', 'c1', now(), 'h', ?)",
                    [json.dumps({"gainPercent": 12.5})])
    run(dbp, None)
    with db.session(dbp) as con:
        assert con.execute("SELECT gain_pct FROM txn_raw").fetchone() == (12.5,)


# ---- to-do counts --------------------------------------------------------------------------------


def test_left_is_read_from_the_newest_ok_run(dbp):
    ctx = Context(db=dbp)
    assert rebuild.last_left(ctx, "master") is None  # never ran
    with db.session(dbp) as con:
        rid = db.start_run(con, "master")
        db.finish_run(con, rid, "ok", 0, json.dumps({"estates": 3, "left": 2}))
        rid = db.start_run(con, "ricacorp")
        db.finish_run(con, rid, "ok", 0, "{'pages': 3, 'left': 0} {}")  # a dict as Python prints it
        rid = db.start_run(con, "srpe")
        db.finish_run(con, rid, "failed", 0, "RuntimeError: x")
    assert rebuild.last_left(ctx, "master") == 2
    assert rebuild.last_left(ctx, "ricacorp") == 0
    assert rebuild.last_left(ctx, "srpe") is None  # a failed run: unknown


def test_pilot_backfill_to_do_counts_unfinished_pilot_estates(dbp):
    ctx = Context(db=dbp, tokens=["太古城"])
    assert rebuild.todo_backfill(ctx) == 1
    with db.session(dbp) as con:
        db.mark_step(con, "backfill", "hse28:1140:flats", 1)
    assert rebuild.todo_backfill(ctx) == 0


def test_daily_is_done_once_a_listing_run_ended_ok(dbp):
    ctx = Context(db=dbp)
    assert rebuild.todo_daily(ctx) == 1
    with db.session(dbp) as con:
        db.finish_run(con, db.start_run(con, "daily"), "partial", 0, "")
    assert rebuild.todo_daily(ctx) == 0


# ---- the command line ----------------------------------------------------------------------------


def test_cli_rebuild_logs_its_command_line_and_passes_the_options(dbp, monkeypatch):
    seen = {}

    def fake(db_path, estates, max_requests, log):
        seen.update(db=db_path, estates=estates, max_requests=max_requests)
        log("fake rebuild")
        return rebuild.EXIT_DEFERRED

    monkeypatch.setattr(rebuild, "rebuild", fake)
    code = cli.main(["--db", str(dbp), "rebuild", "--estates", "爾巒", "--max-requests", "5"])
    assert code == rebuild.EXIT_DEFERRED
    assert seen == {"db": str(dbp), "estates": "爾巒", "max_requests": 5}
    from hkestate.config import get_data_dir

    logs = list((get_data_dir() / "logs").glob("rebuild_*.log"))
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert 'command line: ["--db"' in text and "--max-requests" in text
