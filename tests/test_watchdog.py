"""Watchdog decisions over fake log files, lock files and state: no real processes."""

import json
import os
from datetime import datetime, timedelta

import pytest

from hkestate import locks, watchdog

NOW = datetime(2026, 10, 20, 12, 0)  # a Tuesday, HK time
JOBS = {j.name: j for j in watchdog.JOBS}


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    (tmp_path / "logs").mkdir()
    return tmp_path


def write_log(data, prefix, started: datetime, lines, *, modified: datetime | None = None):
    path = data / "logs" / f"{prefix}_{started:%Y%m%d_%H%M%S}.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    t = (modified or started + timedelta(minutes=5)).timestamp()
    os.utime(path, (t, t))
    return path


def hold_lock(name, pid, alive, monkeypatch):
    path = locks.lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    monkeypatch.setattr(locks, "pid_alive", lambda p: alive and p == pid)


def decide(data, name, state=None, now=NOW):
    job = JOBS[name]
    run = watchdog.last_run(job, watchdog.run_logs(data / "logs", job))
    state = {} if state is None else state
    return watchdog.decide(job, now=now, holder=watchdog.lock_holder(job), run=run,
                           state=state), state


CRASHED_CD = ["10:00:00 log file: x", '10:00:00 command line: ["cd-history", "--limit", "5"]',
              "10:01:00   cd-2-X: FetchError 429", "10:01:00 RuntimeError: 5 errors in a row"]


def test_crashed_cd_history_is_restarted_with_its_own_arguments(data):
    write_log(data, "cd-history", NOW - timedelta(hours=3), CRASHED_CD)
    d, state = decide(data, "cd-history")
    assert d.action == "restart" and d.args == ["cd-history", "--limit", "5"]


def test_a_live_lock_holder_means_running_even_in_a_long_cool_down(data, monkeypatch):
    # cd-history sleeping 2 h in a cool-down writes nothing, but holds its lock
    write_log(data, "cd-history", NOW - timedelta(hours=5), CRASHED_CD[:3])
    hold_lock("cd-history", 4242, True, monkeypatch)
    d, _ = decide(data, "cd-history")
    assert d.action == "none" and "4242" in d.reason


def test_a_stale_lock_does_not_count(data, monkeypatch):
    write_log(data, "cd-history", NOW - timedelta(hours=5), CRASHED_CD)
    hold_lock("cd-history", 4242, False, monkeypatch)
    d, _ = decide(data, "cd-history")
    assert d.action == "restart"


def test_a_recent_log_means_running_when_no_lock_is_held(data):
    write_log(data, "ricacorp", NOW - timedelta(hours=5), ["11:50:00 1000/15000 {...}"],
              modified=NOW - timedelta(minutes=10))
    d, _ = decide(data, "ricacorp")
    assert d.action == "none" and "running" in d.reason


@pytest.mark.parametrize(("name", "prefix", "last"), [
    ("cd-history", "cd-history", "11:00:00 done: {'buildings': 3}"),
    ("backfill-territory", "backfill_territory", "11:00:00 ok: {'estates': 3}, 5 deal rows"),
    ("ricacorp", "ricacorp", "11:00:00 partial: {'pages': 3}"),
    ("backfill-territory", "backfill_territory", "11:00:00 source blocked: 403"),
])
def test_a_normal_ending_is_not_restarted(data, name, prefix, last):
    write_log(data, prefix, NOW - timedelta(hours=3), ["10:00:00 log file: x", last])
    d, _ = decide(data, name)
    assert d.action == "none" and "finished" in d.reason


def test_backfill_failed_after_db_busy_is_restarted(data):
    write_log(data, "backfill_territory", NOW - timedelta(hours=20),
              ["10:00:00 [39/11910] x", "20:25:44 failed: {'estates': 39}, 42532 deal rows"])
    d, _ = decide(data, "backfill-territory")
    assert d.action == "restart" and d.args == ["backfill-territory"]  # old log: defaults


def test_a_not_started_run_is_skipped_for_the_one_before_it(data):
    write_log(data, "cd-history", NOW - timedelta(hours=4), CRASHED_CD)
    write_log(data, "cd-history", NOW - timedelta(hours=2),
              ["10:00:00 not started: lock 'cd-history' is held by process 1"])
    d, _ = decide(data, "cd-history")
    assert d.action == "restart"


def test_ricacorp_from_an_earlier_week_is_left_for_the_sunday_task(data):
    write_log(data, "ricacorp", datetime(2026, 10, 18, 10), ["10:00:00 1000/15000"])  # Sunday
    d, _ = decide(data, "ricacorp")
    assert d.action == "none" and "earlier week" in d.reason
    write_log(data, "ricacorp", datetime(2026, 10, 19, 21), ["21:00:00 1000/15000"])  # Monday
    d, _ = decide(data, "ricacorp")
    assert d.action == "restart"


def test_daily_restarted_only_today_from_8_until_midnight(data):
    lines = ["08:00:09 log file: x", "10:01:55   spacious territory: 1600/6000 buildings"]
    write_log(data, "daily", datetime(2026, 10, 20, 8, 0, 9), lines,
              modified=datetime(2026, 10, 20, 10, 2))
    assert decide(data, "daily")[0].action == "restart"
    assert decide(data, "daily", now=datetime(2026, 10, 20, 20, 30))[0].action == "restart"
    assert decide(data, "daily", now=datetime(2026, 10, 21, 12))[0].action == "none"


def test_a_killed_current_run_is_restarted_although_its_log_is_fresh(data):
    write_log(data, "cd-history", NOW - timedelta(hours=1),
              ['10:00:00 command line: ["cd-history"]', "11:50:00 cooling down 15 min"],
              modified=NOW - timedelta(minutes=5))
    d, _ = decide(data, "cd-history")
    assert d.action == "restart"


def test_finished_daily_is_left_alone_even_after_a_skipped_one(data):
    write_log(data, "daily", datetime(2026, 10, 20, 8), ["17:38:57 daily: partial"])
    write_log(data, "daily", datetime(2026, 10, 20, 9), ["09:00:01 daily: skipped"])
    d, _ = decide(data, "daily")
    assert d.action == "none" and "finished" in d.reason


def test_live_listings_lock_means_daily_is_running(data, monkeypatch):
    write_log(data, "daily", datetime(2026, 10, 20, 8), ["08:00:00 log file"])
    hold_lock("listings", 777, True, monkeypatch)
    assert decide(data, "daily")[0].action == "none"


def test_at_most_three_restarts_a_day(data):
    write_log(data, "cd-history", NOW - timedelta(hours=3), CRASHED_CD)
    state = {"day": NOW.date().isoformat(), "restarts": 3}
    d, _ = decide(data, "cd-history", state)
    assert d.action == "limit"
    d, _ = decide(data, "cd-history", {"day": "2026-10-19", "restarts": 3})
    assert d.action == "restart"  # a new day


def test_crash_loop_guard_gives_up_after_three_quick_deaths(data):
    state = {}
    for k in range(3):
        restarted = NOW - timedelta(hours=6 - k)
        state["last_restart"] = restarted.isoformat()
        state["restarts"] = 0
        for p in (data / "logs").glob("cd-history_*.log"):
            p.unlink()
        write_log(data, "cd-history", restarted, CRASHED_CD,
                  modified=restarted + timedelta(minutes=2))  # died 2 min after the restart
        d, state = decide(data, "cd-history", state, now=restarted + timedelta(minutes=60))
    assert d.action == "give_up" and state["gave_up"]
    d, _ = decide(data, "cd-history", state)
    assert d.action == "none" and "given up" in d.reason


def test_a_slow_death_resets_the_quick_death_count(data):
    restarted = NOW - timedelta(hours=5)
    state = {"last_restart": restarted.isoformat(), "quick_deaths": 2}
    write_log(data, "cd-history", restarted, CRASHED_CD, modified=restarted + timedelta(hours=2))
    d, state = decide(data, "cd-history", state)
    assert d.action == "restart" and state["quick_deaths"] == 0


def test_run_logs_decisions_starts_detached_and_keeps_state(data):
    write_log(data, "cd-history", NOW - timedelta(hours=3), CRASHED_CD)
    started = []
    decisions = watchdog.run(now=NOW, start=lambda args, name, logs: started.append(args) or 99)
    assert started == [["cd-history", "--limit", "5"]]
    assert {d.job: d.action for d in decisions}["cd-history"] == "restart"
    log = (data / "logs" / "watchdog_20261020.log").read_text(encoding="utf-8")
    assert "cd-history: died" in log and "restarted (process 99" in log
    assert "backfill-territory: no log of a run" in log
    state = json.loads((data / "watchdog_state.json").read_text(encoding="utf-8"))
    assert state["cd-history"]["restarts"] == 1


def test_dry_run_starts_nothing_and_saves_no_state(data):
    write_log(data, "cd-history", NOW - timedelta(hours=3), CRASHED_CD)
    started = []
    watchdog.run(now=NOW, dry_run=True, start=lambda *a: started.append(a) or 1)
    assert started == [] and not (data / "watchdog_state.json").exists()
    assert "would restart" in (data / "logs" / "watchdog_20261020.log").read_text("utf-8")


def test_the_cli_logs_its_command_line_for_the_watchdog(data):
    from hkestate import cli
    cli._argv[:] = ["cd-history", "--limit", "2"]
    try:
        log = cli._logger("cd-history")
        log("x")
    finally:
        cli._argv[:] = []
    text = next((data / "logs").glob("cd-history_*.log")).read_text(encoding="utf-8")
    run = watchdog.RunLog(data / "x.log", NOW, NOW, text)
    assert watchdog.saved_args(JOBS["cd-history"], run) == ["cd-history", "--limit", "2"]
