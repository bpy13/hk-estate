"""`hkestate rebuild`: every data step from an empty data folder, in order, resumable.

The steps are steps 1-16 of the audit (plan 2026-10-28, Notes "Audit", table 1). Each step
calls the existing command in this process (`hkestate <command>` or a module's `main`), so it
writes its own log and run_log row as when run by hand. Steps run one at a time.

- **Done means its to-do count is 0**, not that it exited (audit 4, "Done is not the exit
  code"): a step with a `todo` is run again until the count is 0, at most `MAX_TRIES` times;
  then the rebuild stops and names what is left. Steps with no to-do notion (rvd, cross-check,
  link-ids, schools, train, rank, report, datacheck) are done when they exit normally.
- **Progress** is in `run_progress` (command `rebuild`, one step key per step), so a rerun skips
  the steps already done. A test subset has its own keys (`<step>|<estates>`).
- **Host locks**: a step whose host lock (`hse28`, `cd-history`, `ricacorp`, `listings`) is held
  by another live process is deferred, and so is every step that depends on it ("Depends on" in
  the audit table). The other steps go on; the deferred ones are listed at the end and a rerun
  picks them up.
- **Hand repairs built in** (audit 2): R9, the first `backfill-territory` gets `--discover` while
  the 28Hse estate index is empty; R8, `link-ids` runs after the master list and the territory
  backfill; R14, `backfill_gain_pct` runs after the cross-check too, so the master list no
  longer has to wait for it.
- **Options** (test only): `--estates` (estate names, Centadata `2-`/`3-` codes or pilot group
  names), passed to every step that takes a subset; the pilot-only steps (backfill,
  cross-check) and the listing run take only pilot estates and are skipped as "not in the
  subset" without one. `--max-requests N` caps the requests of the whole process
  (`fetch.set_request_cap`); when it is used up the rebuild stops cleanly.

Exit codes: 0 every step done; 1 not started (another rebuild holds `rebuild.lock`); 2 stopped,
a step still had to-do after `MAX_TRIES` tries; 3 the request cap was reached; 4 the steps that
could run are done, some were deferred.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from hkestate import db, locks

COMMAND = "rebuild"
LOCK = "rebuild"
MAX_TRIES = 3
EXIT_OK, EXIT_BUSY, EXIT_STOPPED, EXIT_CAP, EXIT_DEFERRED = 0, 1, 2, 3, 4


@dataclass
class Context:
    db: str | None = None
    tokens: list[str] | None = None  # `--estates` as typed; None = the whole territory
    log: Callable[[str], None] = print
    current: str = ""  # the step running now (for the request-cap message)

    @property
    def subset(self) -> bool:
        return self.tokens is not None

    def estates_arg(self) -> list[str]:
        return ["--estates", ",".join(self.tokens)] if self.tokens is not None else []

    def keys(self, con) -> set[str] | None:
        """The subset as keys (`link.resolve_estates`); resolved when asked, because names
        resolve through the master list, which an earlier step loads."""
        if self.tokens is None:
            return None
        from hkestate import link

        return link.resolve_estates(con, self.tokens)

    def pilot_groups(self, known) -> list[str] | None:
        """The pilot groups (of `known`) in the subset; None without a subset."""
        if self.tokens is None:
            return None
        from hkestate import link

        groups = set(known) if "pilot" in self.tokens else {
            g for t in self.tokens if (g := link.pilot_group(t))}
        return [g for g in known if g in groups]


@dataclass
class Step:
    name: str
    run: Callable[[Context], int | None]
    todo: Callable[[Context], int | None] | None = None  # None: done when it exits normally
    locks: tuple[str, ...] = ()
    depends: tuple[str, ...] = ()
    skip: Callable[[Context], str | None] | None = None  # a reason to skip (subset only)
    after: Callable[[Context], None] | None = None  # runs once the step is done
    what: str = ""  # what the to-do count counts, for the log


# ---- calling the commands ------------------------------------------------------------------


def _cli(ctx: Context, *argv: str) -> int:
    """`hkestate <argv>` in this process (its own log, run_log row and lock wait)."""
    from hkestate import cli

    return cli.main([*(["--db", str(ctx.db)] if ctx.db else []), *argv])


def _module(ctx: Context, module: str, *argv: str) -> int:
    """`python -m <module> <argv>` in this process. Its log names the command it ran."""
    import importlib

    from hkestate import cli

    cli._argv[:] = ["-m", module, *argv]
    main = importlib.import_module(module).main
    return main([*argv, *(["--db", str(ctx.db)] if ctx.db else [])])


def _session(ctx: Context):
    return db.session(ctx.db)


_LEFT = re.compile(r"""['"]left['"]:\s*(\d+)""")


def last_left(ctx: Context, command: str) -> int | None:
    """`left` from the newest run_log row of `command` (master, srpe, ricacorp write it in
    their message); None when that run did not end 'ok' or says nothing."""
    with _session(ctx) as con:
        db.init_db(con)
        row = con.execute("SELECT status, message FROM run_log WHERE command = ? "
                          "ORDER BY run_id DESC LIMIT 1", [command]).fetchone()
    if not row or row[0] != "ok":
        return None
    m = _LEFT.search(row[1] or "")
    return int(m.group(1)) if m else None


# ---- the steps -------------------------------------------------------------------------------


def _pilot_backfill_groups(ctx: Context) -> list[str] | None:
    from hkestate.backfill import PILOT_ESTATES

    return ctx.pilot_groups(list(PILOT_ESTATES))


def _pilot_crosscheck_groups(ctx: Context) -> list[str] | None:
    from hkestate.sources.centadata import PILOT_ENTRIES

    return ctx.pilot_groups(list(PILOT_ENTRIES))


def _listing_groups(ctx: Context) -> list[str] | None:
    from hkestate import listing_run

    return ctx.pilot_groups(list(listing_run.PILOT))


def skip_pilot_only(ctx: Context) -> str | None:
    if ctx.subset and not _pilot_backfill_groups(ctx):
        return "not in the subset (pilot estates only)"
    return None


def skip_listings(ctx: Context) -> str | None:
    if ctx.subset and not _listing_groups(ctx):
        return ("not in the subset: listing runs take only pilot estates or the whole "
                "territory")
    return None


def run_rvd(ctx):
    return _cli(ctx, "rvd")


def run_backfill(ctx):
    groups = _pilot_backfill_groups(ctx)
    return _cli(ctx, "backfill", "--estates", "pilot" if groups is None else ",".join(groups))


def todo_backfill(ctx) -> int:
    """Pilot 28Hse estates (of the subset) whose last backfill step is not recorded."""
    from hkestate import hse28_territory as ht
    from hkestate.backfill import PILOT_ESTATES

    groups = _pilot_backfill_groups(ctx)
    ids = [r.id for g in (PILOT_ESTATES if groups is None else groups)
           for r in PILOT_ESTATES[g]]
    with _session(ctx) as con:
        db.init_db(con)
        finished = ht.finished_ids(con)
    return sum(i not in finished for i in ids)


def run_crosscheck(ctx):
    groups = _pilot_crosscheck_groups(ctx)
    return _cli(ctx, "crosscheck", "--estates", "pilot" if groups is None else ",".join(groups))


def after_crosscheck(ctx) -> None:
    """R14: the cross-check stores no gain_pct; fill it now instead of relying on a later
    master run."""
    from hkestate.sources import cd_master

    with _session(ctx) as con:
        db.init_db(con)
        n = cd_master.backfill_gain_pct(con)
    ctx.log(f"crosscheck: gain_pct filled on {n} Centadata rows")


def run_master(ctx):
    return _cli(ctx, "master", *ctx.estates_arg())


def todo_master(ctx):
    return last_left(ctx, "master")


def _hse28_index_empty(ctx) -> bool:
    from hkestate import link

    with _session(ctx) as con:
        db.init_db(con)
        link.ensure_tables(con)
        return con.execute("SELECT count(*) FROM hse28_estate").fetchone()[0] == 0


def run_territory(ctx):
    # R9: on a fresh database the estate index is empty; only the sitemap fills it
    discover = ["--discover"] if _hse28_index_empty(ctx) else []
    return _cli(ctx, "backfill-territory", *discover, *ctx.estates_arg())


def todo_territory(ctx):
    from hkestate import hse28_territory as ht

    with _session(ctx) as con:
        db.init_db(con)
        return ht.todo_count(con, ctx.keys(con))


def run_link(ctx):
    return _cli(ctx, "link-ids", *ctx.estates_arg())


def run_cd_history(ctx):
    return _cli(ctx, "cd-history", *ctx.estates_arg())


def todo_cd_history(ctx):
    from hkestate.sources import centadata_history as ch

    with _session(ctx) as con:
        db.init_db(con)
        return ch.todo_count(con, ctx.keys(con))


def run_srpe(ctx):
    return _module(ctx, "hkestate.sources.srpe", *ctx.estates_arg())


def todo_srpe(ctx):
    return last_left(ctx, "srpe")


def run_ricacorp(ctx):
    return _cli(ctx, "ricacorp", *ctx.estates_arg())


def todo_ricacorp(ctx):
    return last_left(ctx, "ricacorp")


def run_geo(ctx):
    return _module(ctx, "hkestate.geo", *ctx.estates_arg())


def todo_geo(ctx):
    from hkestate import geo

    with _session(ctx) as con:
        db.init_db(con)
        return geo.todo_count(con, ctx.keys(con))


def run_schools(ctx):
    return _module(ctx, "hkestate.sources.schools")


def run_daily(ctx):
    groups = _listing_groups(ctx)
    if groups is None:
        return _cli(ctx, "daily")
    # a subset: the same listing run for its pilot groups, without the daily cache prune
    return _cli(ctx, "listings", "--estates", ",".join(groups))


def todo_daily(ctx) -> int:
    """0 once the newest listing run ended ok or partial (a source note, e.g. a budget), else 1."""
    with _session(ctx) as con:
        db.init_db(con)
        row = con.execute("SELECT status FROM run_log WHERE command IN ('daily', 'listings') "
                          "ORDER BY run_id DESC LIMIT 1").fetchone()
    return 0 if row and row[0] in ("ok", "partial") else 1


def run_train(ctx):
    return _cli(ctx, "train")


def run_rank(ctx):
    return _cli(ctx, "rank", "--from-snapshot")


def run_report(ctx):
    return _cli(ctx, "report", "--settings", "standard", "--from-snapshot")


def run_datacheck(ctx):
    return _cli(ctx, "datacheck")


DATA_STEPS = ("rvd", "backfill", "crosscheck", "master", "backfill-territory", "link-ids",
              "cd-history", "srpe", "ricacorp", "geo", "schools", "daily")


def steps() -> list[Step]:
    """Steps 1-16 of the audit table, in order. Built when called, so the functions named
    here can be replaced in tests."""
    g = globals()
    return [
        Step("rvd", g["run_rvd"]),
        Step("backfill", g["run_backfill"], g["todo_backfill"], locks=("hse28",),
             skip=g["skip_pilot_only"], what="pilot estates"),
        Step("crosscheck", g["run_crosscheck"], depends=("backfill",),
             skip=g["skip_pilot_only"], after=g["after_crosscheck"]),
        # no longer after the cross-check: R14 fills its gain_pct itself
        Step("master", g["run_master"], g["todo_master"], what="estates"),
        Step("backfill-territory", g["run_territory"], g["todo_territory"], locks=("hse28",),
             depends=("backfill", "master"), what="28Hse estates"),
        Step("link-ids", g["run_link"], depends=("master", "backfill-territory")),
        Step("cd-history", g["run_cd_history"], g["todo_cd_history"], locks=("cd-history",),
             depends=("crosscheck", "master"), what="buildings"),
        Step("srpe", g["run_srpe"], g["todo_srpe"], depends=("master",),
             what="developments"),
        Step("ricacorp", g["run_ricacorp"], g["todo_ricacorp"], locks=("ricacorp",),
             depends=("master",), what="estate pages"),
        Step("geo", g["run_geo"], g["todo_geo"], depends=("backfill", "master"),
             what="buildings"),
        Step("schools", g["run_schools"], depends=("geo",)),
        Step("daily", g["run_daily"], g["todo_daily"], locks=("listings", "hse28"),
             depends=("master", "backfill-territory"), skip=g["skip_listings"],
             what="listing runs"),
        Step("train", g["run_train"], depends=DATA_STEPS),
        Step("rank", g["run_rank"], depends=("train",)),
        Step("report", g["run_report"], depends=("train",)),
        Step("datacheck", g["run_datacheck"], depends=("train",)),
    ]


# ---- the run ---------------------------------------------------------------------------------


def step_key(ctx: Context, name: str) -> str:
    return name if ctx.tokens is None else f"{name}|{','.join(ctx.tokens)}"


def _held(step: Step) -> list[tuple[str, int]]:
    return [(name, pid) for name in step.locks if (pid := locks.held_by(name)) is not None]


def run_steps(ctx: Context, plan: list[Step], run_id: int = 0) -> int:
    """Run `plan` in order (see the module docstring). Returns an exit code."""
    log = ctx.log
    with _session(ctx) as con:
        db.init_db(con)
        done = {r[0] for r in con.execute(
            "SELECT step_key FROM run_progress WHERE command = ?", [COMMAND]).fetchall()}
    deferred: dict[str, str] = {}
    for step in plan:
        key = step_key(ctx, step.name)
        if key in done:
            log(f"{step.name}: done in an earlier run")
            continue
        waiting = [d for d in step.depends if d in deferred]
        if waiting:
            deferred[step.name] = f"waits for {', '.join(waiting)}"
            log(f"{step.name}: deferred, {deferred[step.name]}")
            continue
        if held := _held(step):
            deferred[step.name] = ", ".join(f"lock {n!r} held by process {p}" for n, p in held)
            log(f"{step.name}: deferred, {deferred[step.name]}")
            continue
        reason = step.skip(ctx) if step.skip else None
        if reason:
            log(f"{step.name}: skipped, {reason}")
            with _session(ctx) as con:
                db.mark_step(con, COMMAND, key, run_id, f"skipped: {reason}")
            continue
        ctx.current = step.name
        left, started = None, time.monotonic()
        for attempt in range(1, MAX_TRIES + 1):
            log(f"{step.name}: start (try {attempt} of {MAX_TRIES})")
            t0, code, error = time.monotonic(), None, ""
            try:
                code = step.run(ctx)
            except (Exception, SystemExit) as e:  # one try failed; RequestCapReached and
                error = f"{type(e).__name__}: {e}"  # Ctrl-C are BaseException: they pass
            if step.todo is not None:
                left = step.todo(ctx)
            else:
                left = 0 if (code in (0, None) and not error) else 1
            log(f"{step.name}: try {attempt} took {time.monotonic() - t0:.0f} s, exit "
                f"{code}{', ' + error if error else ''}; to do: "
                f"{'unknown' if left is None else left} {step.what}".rstrip())
            if left == 0:
                break
        if left != 0:
            what = f"{left} {step.what}".strip() if left is not None else "an unknown amount"
            log(f"stopped: {step.name} still has {what} to do after {MAX_TRIES} tries; "
                "rerun `hkestate rebuild` once the cause is fixed")
            return EXIT_STOPPED
        if step.after:
            step.after(ctx)
        with _session(ctx) as con:
            db.mark_step(con, COMMAND, key, run_id,
                         f"ok in {time.monotonic() - started:.0f} s")
        log(f"{step.name}: done")
    ctx.current = ""
    if deferred:
        log(f"deferred ({len(deferred)}): "
            + "; ".join(f"{n} ({why})" for n, why in deferred.items()))
        log("rerun `hkestate rebuild` once those locks are free")
        return EXIT_DEFERRED
    log("rebuild complete: every step done")
    return EXIT_OK


def rebuild(db_path=None, estates: str | None = None, max_requests: int | None = None,
            log: Callable[[str], None] = print, plan: list[Step] | None = None) -> int:
    """The whole rebuild: holds `rebuild.lock`, sets the request cap, runs the steps."""
    from hkestate import fetch

    tokens = None
    if estates is not None:
        tokens = [t.strip() for t in estates.split(",") if t.strip()]
    ctx = Context(db=db_path, tokens=tokens, log=log)
    try:
        with locks.hold(LOCK):
            fetch.set_request_cap(max_requests)
            with _session(ctx) as con:
                db.init_db(con)
                run_id = db.start_run(con, COMMAND, json.dumps(
                    {"estates": tokens, "max_requests": max_requests}, ensure_ascii=False))
            status, code = "failed", EXIT_STOPPED
            try:
                code = run_steps(ctx, steps() if plan is None else plan, run_id)
                status = {EXIT_OK: "ok", EXIT_DEFERRED: "partial"}.get(code, "stopped")
            except fetch.RequestCapReached as e:
                log(f"stopped: {e} during step {ctx.current or '-'} "
                    f"({fetch.requests_counted()} requests sent); the step resumes on a rerun")
                status, code = "stopped", EXIT_CAP
            except KeyboardInterrupt:
                status = "interrupted"
                raise
            finally:
                with _session(ctx) as con:
                    db.finish_run(con, run_id, status, 0, f"exit {code}")
                fetch.set_request_cap(None)
            return code
    except locks.LockBusy as e:
        log(f"not started: {e}")
        return EXIT_BUSY
