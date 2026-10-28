"""Watchdog: restart a long job, or today's daily listing run, that died.

Run every 15 minutes by the scheduled task `\\hk-estate\\watchdog` (scripts/watchdog.ps1).
It never opens the database: it reads lock files (data/locks/), the jobs' own logs
(data/logs/<job>_<YYYYmmdd_HHMMSS>.log) and a small state file (data/watchdog_state.json).

A job is **alive** if a live process holds its lock file (the same stale-lock rule as
locks.py), or its newest log was written in the last 45 minutes and comes from a run started
by older code (no "command line:" line; such runs may hold no lock of their own). A job
sleeping through a cool-down holds its lock, so it is alive. A job is **dead** if it is not
alive and its newest log (ignoring "not started" and "skipped" runs) has no normal ending:
it crashed, was killed, or was stopped by Ctrl+C.

Jobs watched:
- `backfill-territory` (lock hse28; ends "ok: {...}" / "partial: {...}", or "source blocked")
- `cd-history` (lock cd-history; ends "done: {...}", or a SourceBlocked line)
- `ricacorp` (lock ricacorp; ends "ok: {...}" / "partial: {...}"): only a pass of the
  current ISO week is resumed; the Sunday task starts new passes.
- `daily` (lock listings; ends "daily: ok|partial|skipped"): only today's (HK date) run, and
  only between 08:00 and midnight. A rerun doesn't read again what today's run already read.

While a live process holds `rebuild.lock` (`hkestate rebuild`), the watchdog logs one line
and restarts nothing: the rebuild runs these jobs itself.

Limits: at most 3 watchdog restarts per job per day; a job that died within 10 minutes of
its last watchdog restart 3 times in a row is left alone ("GIVING UP" in the log) until its
entry is removed from the state file or it is seen running or finished again.

Restarts are detached from any console (so Ctrl+C in a terminal can't reach them): the
env's python.exe `-m hkestate <same arguments as the dead run>`, output to
data/logs/<job>_watchdog_<time>.out/.err. Every decision is logged to
data/logs/watchdog_<YYYYmmdd>.log.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hkestate import locks
from hkestate.config import REPO_ROOT, get_data_dir

HK_TZ = ZoneInfo("Asia/Hong_Kong")
FRESH_LOG_MIN = 45  # a log written this recently means the job is still running
MAX_RESTARTS_PER_DAY = 3
QUICK_DEATH_MIN = 10
QUICK_DEATHS_LIMIT = 3
DAILY_FROM_HOUR, DAILY_UNTIL_HOUR = 8, 24  # a dead run is resumed until midnight

_TIME = r"^\d\d:\d\d:\d\d "


@dataclass(frozen=True)
class Job:
    name: str  # the hkestate command
    log_prefix: str  # data/logs/<log_prefix>_<YYYYmmdd_HHMMSS>.log
    lock_names: tuple[str, ...]
    finished: tuple[str, ...]  # regexes: any match in the log = a normal ending
    not_a_run: tuple[str, ...] = (r"not started: ",)  # logs of runs that never started
    weekly: bool = False
    daily: bool = False


JOBS = [
    Job("backfill-territory", "backfill_territory", ("hse28",),
        (_TIME + r"(ok|partial): \{", r"source blocked: ", r"no 28Hse estates known")),
    Job("cd-history", "cd-history", ("cd-history",),
        (_TIME + r"done: \{", r"SourceBlocked: ")),
    Job("ricacorp", "ricacorp", ("ricacorp",),
        (_TIME + r"(ok|partial): \{", r"SourceBlocked: "), weekly=True),
    Job("daily", "daily", ("listings",),
        (_TIME + r"daily: (ok|partial|skipped)",), not_a_run=(_TIME + r"daily: skipped",),
        daily=True),
]

_LOG_NAME = re.compile(r"_(\d{8})_(\d{6})\.log$")


@dataclass
class RunLog:
    path: Path
    started: datetime  # from the file name (local time)
    modified: datetime
    text: str


@dataclass
class Decision:
    job: str
    action: str  # none, restart, give_up, limit
    reason: str
    args: list[str] = field(default_factory=list)


# ---- reading the facts ---------------------------------------------------------------------


def run_logs(logs_dir: Path, job: Job) -> list[RunLog]:
    """The job's logs, newest first (by the start time in the name)."""
    out = []
    for p in logs_dir.glob(f"{job.log_prefix}_*.log"):
        m = _LOG_NAME.search(p.name)
        if not m or p.name[: m.start()] != job.log_prefix:
            continue  # e.g. daily_watchdog_..., another job sharing the prefix
        try:
            started = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            text = p.read_text(encoding="utf-8", errors="replace")
            modified = datetime.fromtimestamp(p.stat().st_mtime)
        except (OSError, ValueError):
            continue
        out.append(RunLog(p, started, modified, text))
    out.sort(key=lambda r: r.started, reverse=True)
    return out


def last_run(job: Job, logs: list[RunLog]) -> RunLog | None:
    """The newest log of a run that actually started."""
    for r in logs:
        if not any(re.search(p, r.text, re.MULTILINE) for p in job.not_a_run):
            return r
    return None


def finished(job: Job, run: RunLog) -> bool:
    return any(re.search(p, run.text, re.MULTILINE) for p in job.finished)


def lock_holder(job: Job) -> int | None:
    for name in job.lock_names:
        pid = locks.held_by(name)
        if pid is not None:
            return pid
    return None


def saved_args(job: Job, run: RunLog | None) -> list[str]:
    """The dead run's own arguments (its "command line:" log line), else the default."""
    if run is not None:
        m = re.search(r"command line: (\[.*\])\s*$", run.text, re.MULTILINE)
        if m:
            try:
                args = json.loads(m.group(1))
            except ValueError:
                args = None
            if (isinstance(args, list) and all(isinstance(a, str) for a in args)
                    and job.name in args):
                return args
    return [job.name]


# ---- the decision (pure) -------------------------------------------------------------------


def decide(job: Job, *, now: datetime, holder: int | None, run: RunLog | None,
           state: dict) -> Decision:
    """What to do about one job. `now` is local (HK) time; `state` is the job's entry of the
    state file and is updated in place (restart counts, quick deaths, given up)."""
    today = now.date().isoformat()
    if state.get("day") != today:
        state["day"], state["restarts"] = today, 0
    if holder is not None:
        state.pop("gave_up", None)
        state["quick_deaths"] = 0
        return Decision(job.name, "none", f"running (process {holder} holds its lock)")
    if run is None:
        return Decision(job.name, "none", "no log of a run")
    if finished(job, run):
        state.pop("gave_up", None)
        state["quick_deaths"] = 0
        return Decision(job.name, "none", f"finished normally ({run.path.name})")
    # Only a run started by older code (no "command line:" line) may be alive without its
    # lock; current code always holds it, so no holder means it died (e.g. it was killed).
    if (now - run.modified < timedelta(minutes=FRESH_LOG_MIN)
            and not re.search(r"command line: ", run.text)):
        return Decision(job.name, "none",
                        f"running (log written {run.modified:%H:%M}, no lock holder seen)")
    if job.daily:
        if run.started.date() != now.date():
            return Decision(job.name, "none", "today's run has not started")
        if not DAILY_FROM_HOUR <= now.hour < DAILY_UNTIL_HOUR:
            return Decision(job.name, "none",
                            f"died, but outside {DAILY_FROM_HOUR:02d}:00-{DAILY_UNTIL_HOUR}:00")
    if job.weekly and run.started.isocalendar()[:2] != now.date().isocalendar()[:2]:
        return Decision(job.name, "none",
                        "last pass is from an earlier week (the Sunday task starts a new one)")
    if state.get("gave_up"):
        return Decision(job.name, "none", "given up earlier (crash loop); not restarting")
    # it died: was that soon after our last restart?
    last = state.get("last_restart")
    if last and state.get("counted_death") != last:
        state["counted_death"] = last
        restarted = datetime.fromisoformat(last)
        if run.modified - restarted < timedelta(minutes=QUICK_DEATH_MIN):
            state["quick_deaths"] = state.get("quick_deaths", 0) + 1
        else:
            state["quick_deaths"] = 0
    if state.get("quick_deaths", 0) >= QUICK_DEATHS_LIMIT:
        state["gave_up"] = True
        return Decision(job.name, "give_up",
                        f"died within {QUICK_DEATH_MIN} min of each of the last "
                        f"{QUICK_DEATHS_LIMIT} restarts (last log {run.path.name})")
    if state.get("restarts", 0) >= MAX_RESTARTS_PER_DAY:
        return Decision(job.name, "limit",
                        f"died, but already restarted {MAX_RESTARTS_PER_DAY} times today")
    return Decision(job.name, "restart",
                    f"died: {run.path.name} has no normal ending "
                    f"(last written {run.modified:%Y-%m-%d %H:%M})",
                    saved_args(job, run))


# ---- acting ---------------------------------------------------------------------------------


DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def start_detached(args: list[str], name: str, logs_dir: Path) -> int:
    """Start `python -m hkestate <args>` with no console (Ctrl+C in a terminal can't reach
    it), out of the task's job object where Windows allows it. Returns the process id."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = open(logs_dir / f"{name}_watchdog_{stamp}.out", "ab")  # noqa: SIM115
    err = open(logs_dir / f"{name}_watchdog_{stamp}.err", "ab")  # noqa: SIM115
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable, "-m", "hkestate", *args]
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

    def popen(creationflags: int) -> int:
        return subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, creationflags=creationflags,
                                close_fds=True).pid

    try:
        if not flags:
            return popen(0)
        try:
            return popen(flags | CREATE_BREAKAWAY_FROM_JOB)
        except OSError:  # the task's job object doesn't allow breakaway
            return popen(flags)
    finally:
        out.close()
        err.close()


def state_path() -> Path:
    return get_data_dir() / "watchdog_state.json"


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def run(*, dry_run: bool = False, now: datetime | None = None, jobs: list[Job] | None = None,
        start=start_detached) -> list[Decision]:
    """One watchdog round: decide for every job, restart the dead ones, log it all."""
    now = now or datetime.now(HK_TZ).replace(tzinfo=None)
    logs_dir = get_data_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    wlog = logs_dir / f"watchdog_{now:%Y%m%d}.log"
    spath = state_path()
    state = load_state(spath)
    decisions = []
    with open(wlog, "a", encoding="utf-8") as fh:
        def log(msg: str) -> None:
            fh.write(f"{datetime.now():%H:%M:%S} {msg}\n")

        rebuild_pid = locks.held_by("rebuild")
        if rebuild_pid is not None:
            # a rebuild runs the long jobs itself, in its own order; restarting one beside it
            # would double the request rate on a site
            log(f"rebuild is running (process {rebuild_pid} holds rebuild.lock); "
                "restarting nothing")
            return [Decision(j.name, "none", f"rebuild running (process {rebuild_pid})")
                    for j in jobs or JOBS]
        for job in jobs or JOBS:
            entry = state.setdefault(job.name, {})
            d = decide(job, now=now, holder=lock_holder(job),
                       run=last_run(job, run_logs(logs_dir, job)), state=entry)
            decisions.append(d)
            if d.action == "give_up":
                log(f"{job.name}: GIVING UP: {d.reason}. Not restarting it until you fix the "
                    f"cause and remove '{job.name}' from {spath}.")
            elif d.action == "restart":
                if dry_run:
                    log(f"{job.name}: {d.reason}; would restart: hkestate {' '.join(d.args)}")
                    continue
                try:
                    pid = start(d.args, job.log_prefix, logs_dir)
                except OSError as e:
                    log(f"{job.name}: {d.reason}; restart FAILED: {e}")
                    continue
                entry["restarts"] = entry.get("restarts", 0) + 1
                entry["last_restart"] = now.isoformat(timespec="seconds")
                log(f"{job.name}: {d.reason}; restarted (process {pid}, restart "
                    f"{entry['restarts']} today): hkestate {' '.join(d.args)}")
            else:
                log(f"{job.name}: {d.reason}")
    if not dry_run:
        save_state(spath, state)
    return decisions
