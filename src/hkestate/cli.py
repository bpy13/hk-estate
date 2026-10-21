"""`hkestate` command line.

    hkestate backfill --estates taikoo-shing,goldwin-heights   (or --estates pilot)
    hkestate listings [--sources centanet,hse28,spacious]      listing snapshots
    hkestate daily                                              scheduled: listings + prune
    hkestate update                                             scheduled weekly: new deals
    hkestate backfill-territory [--discover]   28Hse, whole territory, largest first
    hkestate ricacorp [--max-hours N]          Ricacorp previous-deal pairs (weekly)
    hkestate link-ids                          match 28Hse ids to master ids
    hkestate train [--from-snapshot]           fair-price model + data/reports/model_eval.md
    hkestate rank                              ranked listings CSV in data/reports/
    hkestate report [--from-snapshot] [--start D] [--end D] [--settings standard]
                                               growth study, findings_<date>.md; with
                                               --settings also data/reports/growth/<date>/
    hkestate datacheck [--read-db] [--no-fetch]  data checks on the snapshot, datacheck_<date>.md
    hkestate check-blocked                      monthly: do blocked sites still refuse us?
    hkestate watchdog [--dry-run]              every 15 min: restart jobs that died
    hkestate status

Every run is logged to data/logs/<command>_<time>.log and to `run_log` in the database.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from hkestate import db, locks
from hkestate.config import get_data_dir

# Long resumable jobs: they wait for the database file for as long as it takes (db.connect)
# instead of giving up after 30 minutes.
LONG_JOBS = {"backfill", "backfill-territory", "cd-history", "ricacorp", "master", "srpe"}

_argv: list[str] = []  # this run's arguments, written to the log (the watchdog reuses them)


class _FetchLogHandler(logging.Handler):
    """Puts the fetcher's pacing messages (slowing down, cool-downs) in the job's log."""

    def __init__(self, log):
        super().__init__(logging.INFO)
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log(record.getMessage())
        except Exception:  # noqa: BLE001 - logging must never stop a job
            pass


def _logger(command: str):
    logs = get_data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"{command}_{datetime.now():%Y%m%d_%H%M%S}.log"
    fh = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the run

    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        fh.write(line + "\n")
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # a console that can't show Chinese
            print(line.encode("ascii", "replace").decode(), flush=True)

    log(f"log file: {path}")
    if _argv:
        log(f"command line: {json.dumps(_argv, ensure_ascii=False)}")
    fetch_log = logging.getLogger("hkestate.fetch")
    for h in [h for h in fetch_log.handlers if isinstance(h, _FetchLogHandler)]:
        fetch_log.removeHandler(h)
    fetch_log.addHandler(_FetchLogHandler(log))
    fetch_log.setLevel(logging.INFO)
    fetch_log.propagate = False
    return log


def cmd_backfill(args) -> int:
    from hkestate.backfill import PILOT_ESTATES, backfill_estates

    names = list(PILOT_ESTATES) if args.estates == "pilot" else args.estates.split(",")
    unknown = [n for n in names if n not in PILOT_ESTATES]
    if unknown:
        print(f"unknown estates {unknown}; known: {', '.join(PILOT_ESTATES)}", file=sys.stderr)
        return 2
    log = _logger("backfill")
    try:
        # the lock tells the daily listing job to leave 28Hse alone while this runs
        with locks.hold("hse28"):
            added = backfill_estates(
                {n: PILOT_ESTATES[n] for n in names},
                db_path=args.db,
                log=log,
                refresh=args.refresh,
                skip_flats=args.skip_flats,
                patient=True,  # wait out throttling instead of stopping
            )
    except locks.LockBusy as e:
        log(f"not started: {e}")
        return 1
    log(f"done: {added} rows added to txn_raw")
    return 0


def cmd_update(args) -> int:
    """Weekly transaction refresh (newest 28Hse deal-list pages)."""
    from hkestate.backfill import PILOT_ESTATES, update_estates

    names = list(PILOT_ESTATES) if args.estates == "pilot" else args.estates.split(",")
    log = _logger("update")
    try:
        with locks.hold("hse28"):
            added = update_estates({n: PILOT_ESTATES[n] for n in names}, db_path=args.db,
                                   log=log)
    except locks.LockBusy as e:
        # a backfill is reading 28Hse: it will pick up the newest deals itself
        log(f"skipped: {e}")
        with db.session(args.db) as con:
            db.init_db(con)
            run_id = db.start_run(con, "update", ",".join(names))
            db.finish_run(con, run_id, "skipped", 0, str(e))
        return 0
    log(f"done: {added} rows added to txn_raw")
    return 0


def _listings(args, command: str, sources: list[str], prune: bool) -> int:
    """Shared by `listings` and `daily`: one listing run, logged to run_log and a log file."""
    import json

    from hkestate import listing_run, maintenance
    from hkestate.fetch import _hk_today

    pilot = list(listing_run.PILOT)
    groups = {"pilot": pilot, "all": [*pilot, listing_run.TERRITORY],
              "territory": [listing_run.TERRITORY]}.get(args.estates) or args.estates.split(",")
    known = [*pilot, listing_run.TERRITORY]
    unknown = [g for g in groups if g not in known]
    if unknown:
        print(f"unknown estates {unknown}; known: pilot, all, territory, {', '.join(known)}",
              file=sys.stderr)
        return 2
    log = _logger(command)
    with db.session(args.db) as con:
        db.init_db(con)
        run_id = db.start_run(con, command, f"{','.join(groups)} / {','.join(sources)}")
    status, message, rows = "failed", "", 0
    try:
        with locks.hold("listings"):
            summary = listing_run.run_listings(
                groups, sources, db_path=args.db, log=log,
                centanet_budget=args.centanet_budget, spacious_budget=args.spacious_budget,
                parallel=not args.sequential)
        for src, per_group in summary["sources"].items():
            for g, s in per_group.items():
                log(f"{src} {g}: {s}")
        for g, mres in summary["match"].items():
            rate = mres["matched"] / mres["listings"] if mres["listings"] else 0
            log(f"match {g}: {mres['matched']}/{mres['listings']} ({rate:.0%}) {mres['methods']}")
        if prune:
            removed = maintenance.prune_listing_cache(
                get_data_dir() / "cache", today=_hk_today()
            )
            log(f"cache: {removed} listing pages older than "
                f"{maintenance.KEEP_DAYS} days deleted")
        rows = sum(s["new"] for pg in summary["sources"].values() for s in pg.values())
        skipped = [f"{src}/{g}: {s['note']}" for src, pg in summary["sources"].items()
                   for g, s in pg.items() if s.get("note")]
        status = "ok" if not skipped else "partial"
        message = json.dumps({"elapsed_s": summary["elapsed_s"], "notes": skipped[:5],
                              "match": {g: [m["matched"], m["listings"]]
                                        for g, m in summary["match"].items()}},
                             ensure_ascii=False)
    except locks.LockBusy as e:
        status, message = "skipped", str(e)
        log(f"skipped: {e}")
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        with db.session(args.db) as con:
            db.finish_run(con, run_id, status, rows, message)
    log(f"{command}: {status}")
    return 0 if status in ("ok", "partial", "skipped") else 1


def cmd_listings(args) -> int:
    return _listings(args, "listings", args.sources.split(","), prune=False)


def cmd_daily(args) -> int:
    """The scheduled daily job: every listing source, then prune old cached listing pages."""
    return _listings(args, "daily", ["centanet", "hse28", "spacious"], prune=True)


def cmd_prune(args) -> int:
    from hkestate import maintenance
    from hkestate.fetch import _hk_today

    removed = maintenance.prune_listing_cache(get_data_dir() / "cache", today=_hk_today(),
                                              keep_days=args.days)
    print(f"{removed} cached listing pages deleted")
    return 0


def cmd_rvd(args) -> int:
    from hkestate.fetch import Fetcher
    from hkestate.sources import rvd

    log = _logger("rvd")
    with db.session(args.db) as con:
        db.init_db(con)
        run_id = db.start_run(con, "rvd")
    status, message, total = "failed", "", 0
    try:
        with Fetcher() as f:
            loaded = rvd.load_all(f, db_path=args.db, log=log)
        total = sum(loaded.values())
        status = "ok"
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        with db.session(args.db) as con:
            db.finish_run(con, run_id, status, total, message)
    return 0


def cmd_crosscheck(args) -> int:
    from hkestate.fetch import Fetcher
    from hkestate.sources import centadata

    groups = list(centadata.PILOT_ENTRIES) if args.estates == "pilot" else args.estates.split(",")
    log = _logger("crosscheck")
    with db.session(args.db) as con:
        db.init_db(con)
        run_id = db.start_run(con, "crosscheck", ",".join(groups))
    status, message, added = "failed", "", 0
    try:
        with Fetcher() as f:
            summaries = centadata.run_crosscheck(groups, f, db_path=args.db, log=log)
        added = sum(s["added"] for s in summaries)
        path = get_data_dir() / "reports" / f"crosscheck_{datetime.now():%Y%m%d}.md"
        centadata.write_report(summaries, path)
        log(f"report: {path}")
        status = "ok"
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        with db.session(args.db) as con:
            db.finish_run(con, run_id, status, added, message)
    return 0


def cmd_master(args) -> int:
    """Territory-wide Centadata estate/building list and latest deal per flat (resumable)."""
    from hkestate.fetch import Fetcher
    from hkestate.sources import cd_master

    log = _logger("master")
    # 4 s between requests: hk.centanet.com is shared with the listings and cd-history,
    # which run at the same time
    with Fetcher(min_interval=cd_master.MIN_INTERVAL_S) as f:
        cd_master.run_master(f, db_path=args.db, log=log, limit=args.limit)
    return 0


def cmd_link_ids(args) -> int:
    """Match 28Hse ids to master ids (pilot, estate names, buildings already loaded)."""
    from hkestate import link

    log = _logger("link")
    with db.session(args.db) as con:
        res = link.link_all(con)
    log(f"linked: {res}")
    return 0


def cmd_backfill_territory(args) -> int:
    """Territory-wide 28Hse backfill, largest estates first, resumable (Task 3.2)."""
    from hkestate import hse28_territory as ht
    from hkestate.fetch import Fetcher

    log = _logger("backfill_territory")
    try:
        with locks.hold("hse28"):
            with db.session(args.db) as con:
                db.init_db(con)
                from hkestate import link
                link.ensure_tables(con)
                added = ht.add_estates(con, ht.read_csv(), "csv")
                added += ht.add_estates(con, ht.refs_from_listings(con), "listings")
            if args.discover:
                with Fetcher(min_interval=4.0) as f:
                    refs = ht.discover(f, log)
                with db.session(args.db) as con:
                    added += ht.add_estates(con, refs, "sitemap")
            with db.session(args.db) as con:
                total = con.execute("SELECT count(*) FROM hse28_estate").fetchone()[0]
            log(f"{added} estates added to the index, {total} in all")
            if total == 0:
                log("no 28Hse estates known: rerun with --discover, or put id,name rows in "
                    "data/manual/hse28_estates.csv")
                return 1
            ht.run_territory(db_path=args.db, log=log, order=args.order,
                             mapped_only=args.mapped_only, skip_flats=args.skip_flats,
                             max_estates=args.max_estates, max_hours=args.max_hours)
    except locks.LockBusy as e:
        log(f"not started: {e}")
        return 1
    return 0

def cmd_cd_history(args) -> int:
    """Centadata full per-flat sale history, largest estates first (resumable)."""
    from hkestate.fetch import Fetcher
    from hkestate.sources import centadata_history as ch

    log = _logger("cd-history")
    estates = None if args.estates == "all" else args.estates.split(",")
    try:
        with locks.hold("cd-history"):
            with Fetcher(min_interval=ch.MIN_INTERVAL_S) as f:
                ch.run_history(f, db_path=args.db, log=log, estates=estates, limit=args.limit,
                               max_hours=args.max_hours)
    except locks.LockBusy as e:
        log(f"not started: {e}")
        return 1
    return 0


def cmd_ricacorp(args) -> int:
    """Ricacorp estate pages: previous-deal pairs (Task 3.6); resumable within the week."""
    from hkestate.fetch import Fetcher
    from hkestate.sources import ricacorp

    log = _logger("ricacorp")
    if args.place_only:
        with db.session(args.db) as con:
            db.init_db(con)
            ricacorp.place_deals(con, log=log)
        return 0
    try:
        with locks.hold("ricacorp"), Fetcher(min_interval=ricacorp.MIN_INTERVAL_S) as f:
            ricacorp.run_ricacorp(f, db_path=args.db, log=log, limit=args.limit,
                                  max_hours=args.max_hours)
    except locks.LockBusy as e:
        log(f"not started: {e}")
        return 1
    return 0

def _logged(args, command: str, work) -> int:
    """Run `work(log)` with a log file and a run_log row (status ok/failed + its message)."""
    import json

    log = _logger(command)
    with db.session(args.db) as con:
        db.init_db(con)
        run_id = db.start_run(con, command)
    status, message, rows = "failed", "", 0
    try:
        rows, summary = work(log)
        status, message = "ok", json.dumps(summary, ensure_ascii=False, default=str)
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        log(message)
        raise
    finally:
        with db.session(args.db) as con:
            db.finish_run(con, run_id, status, rows, message)
    return 0


def cmd_check_blocked(args) -> int:
    """Monthly: request robots.txt of each blocked site once and log what it answers."""
    from hkestate import blocked_check
    from hkestate.fetch import Fetcher

    log = _logger("check-blocked")
    with Fetcher() as f:
        blocked_check.run_check(f, args.db, log)
    return 0

def cmd_train(args) -> int:
    """Fair-price model: snapshot the database, evaluate out of time, fit, write the report."""
    from hkestate import features, model

    def work(log):
        raw = features.load_snapshot() if args.from_snapshot else features.snapshot(args.db)
        log(f"snapshot: {raw['meta']}")
        out = model.train(raw, rounds=args.rounds, log=log, since=args.since,
                          compare_since=args.compare_since, previous=args.previous)
        mdape = {r["group"]: round(r["mdape"], 4) for r in out["meta"]["eval"]}
        return out["meta"]["n_train"], {"mdape": mdape, "report": str(out["report_path"])}

    return _logged(args, "train", work)


def cmd_rank(args) -> int:
    """Value every active sale listing and write the ranked CSV (needs `hkestate train`)."""
    from hkestate import features, model, rank

    if not (model.model_dir() / "fair_price.txt").exists():
        print("no trained model: run `hkestate train` first", file=sys.stderr)
        return 1

    def work(log):
        raw = features.load_snapshot() if args.from_snapshot else features.snapshot(args.db)
        r = rank.rank(raw, model.load())
        paths = rank.write(r)
        log(f"ranked {len(r.table)} listings: {paths['csv']}")
        log(f"counts: {r.counts}")
        return len(r.table), {**r.counts, "csv": str(paths["csv"])}

    return _logged(args, "rank", work)


def cmd_report(args) -> int:
    """Growth study and findings report (Phase 5): data/reports/findings_<date>.md.

    `--start/--end` set the window (a pair counts when both sales fall inside; default all
    data); `--settings standard` (or a comma-separated list of names) runs the preset
    windows from one snapshot and saves each under data/reports/growth/<date>/<setting>/,
    writing the findings file from the `all` setting (plan 2026-10-21, Phase 4)."""
    import pandas as pd

    from hkestate import features, model, report, study

    for name in ("start", "end"):
        value = getattr(args, name, None)
        if value is not None:
            try:
                pd.Timestamp(value)
            except (ValueError, TypeError):
                print(f"--{name} must be a date like 2003-07-01, not {value!r}", file=sys.stderr)
                return 2
    settings = None
    if getattr(args, "settings", None):
        try:
            settings = study.settings_named(args.settings)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2

    def work(log):
        raw = features.load_snapshot() if args.from_snapshot else features.snapshot(args.db)
        m, tp = None, None
        if (model.model_dir() / "fair_price.txt").exists():
            m = model.load()
            tp_path = model.model_dir() / "test_predictions.parquet"
            tp = pd.read_parquet(tp_path) if tp_path.exists() else None
        studies = None
        if settings:
            studies = study.run_settings(raw, m, tp, settings, log=log)
            s = studies.get("all") or next(iter(studies.values()))
            summary = {"settings": {k: {"pairs": {t: len(v) for t, v in st.pairs.items()},
                                        "enough": st.enough, "seconds": round(st.seconds)}
                                    for k, st in studies.items()},
                       "dir": str(study.growth_dir())}
        else:
            s = study.run(raw, m, tp, log=log, start=args.start, end=args.end)
            summary = {}
        path = report.write(s, studies)
        log(f"report ({s.setting}): {path}")
        counts = {k: len(v) for k, v in s.pairs.items()}
        robust = {k: [a for a in v.index if v.loc[a, "robust"]] for k, v in s.verdicts.items()
                  if len(v)}
        return counts.get("recorded", 0), {"pairs": counts, "robust": robust,
                                          "report": str(path), **summary}

    return _logged(args, "report", work)


def cmd_datacheck(args) -> int:
    """Data checks on the snapshot: data/reports/datacheck_<date>.md (plan 2026-10-21 Task 4).

    Reads `data/model/snapshot` unless `--read-db` asks for a fresh one; no run_log row, so
    by default it never opens the database (scrapers hold it)."""
    from hkestate import datacheck, features

    log = _logger("datacheck")
    raw = features.snapshot(args.db) if args.read_db else features.load_snapshot()
    log(f"snapshot: {raw['meta']}")
    registry = None
    if not args.no_fetch:
        from hkestate.fetch import Fetcher

        years = range(datacheck.LR_FIRST_YEAR, datetime.now().year + 1)
        with Fetcher() as f:
            registry = datacheck.land_registry_counts(f, years, log=log)
        log(f"Land Registry counts for {len(registry)} years ({f.requests_made} requests)")
    res = datacheck.run(raw, registry, log=log)
    path = datacheck.write_report(res, raw)
    for check, verdict in res["verdicts"].items():
        log(f"{check}: {verdict}")
    log(f"report: {path}")
    return 0


def cmd_status(args) -> int:
    with db.session(args.db) as con:
        db.init_db(con)
        print("last runs:")
        for row in con.execute(
            "SELECT run_id, command, status, started_at, finished_at, rows_added, message "
            "FROM run_log ORDER BY run_id DESC LIMIT 10"
        ).fetchall():
            print("  ", *row)
        print("deals per estate group (txn_raw / clean txn):")
        for row in con.execute(
            """
            SELECT coalesce(e.group_name, t.estate_id), count(*) AS raw,
                   count(*) FILTER (WHERE c.source_id IS NOT NULL) AS clean,
                   min(t.deal_date), max(t.deal_date)
            FROM txn_latest t LEFT JOIN estate e USING (estate_id)
            -- explicit ON: estate has a `source` column too, so USING would be ambiguous
            LEFT JOIN txn c ON c.source = t.source AND c.source_id = t.source_id
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall():
            print("  ", *row)
    return 0


def cmd_watchdog(args) -> int:
    """Every 15 minutes (scheduled): restart a long job or today's daily run that died."""
    from hkestate import watchdog

    watchdog.run(dry_run=args.dry_run)
    return 0


def _budgets(parser) -> None:
    from hkestate import listing_run

    parser.add_argument("--centanet-budget", type=int,
                        default=listing_run.CENTANET_TERRITORY_BUDGET,
                        help="optional cap on territory detail pages read per run (new "
                             "listings first); default: every due page")
    parser.add_argument("--spacious-budget", type=int,
                        default=listing_run.SPACIOUS_TERRITORY_BUDGET,
                        help="optional cap on territory Spacious building pages per run; "
                             "default: every building")
    parser.add_argument("--sequential", action="store_true",
                        help="read the sources one after another instead of in parallel")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hkestate")
    p.add_argument("--db", default=None, help="database file (default data/hkestate.duckdb)")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backfill", help="load transaction history from 28Hse")
    b.add_argument("--estates", required=True, help="comma-separated names, or 'pilot'")
    b.add_argument("--refresh", action="store_true", help="redo finished steps")
    b.add_argument("--skip-flats", action="store_true", help="skip the per-flat pages")
    b.set_defaults(func=cmd_backfill)

    r = sub.add_parser("rvd", help="load RVD indices, average prices/rents and stock")
    r.set_defaults(func=cmd_rvd)

    c = sub.add_parser("crosscheck", help="compare 28Hse deals with Centadata block pages")
    c.add_argument("--estates", required=True, help="comma-separated names, or 'pilot'")
    c.set_defaults(func=cmd_crosscheck)

    m = sub.add_parser("master", help="territory-wide estate/building list from Centadata")
    m.add_argument("--limit", type=int, default=None, help="only the next N estates (testing)")
    m.set_defaults(func=cmd_master)

    lk = sub.add_parser("link-ids", help="link 28Hse estates/buildings to master ids")
    lk.set_defaults(func=cmd_link_ids)

    bt = sub.add_parser("backfill-territory",
                        help="territory-wide 28Hse backfill, largest estates first (resumable)")
    bt.add_argument("--discover", action="store_true",
                    help="first read 28Hse's sitemap for the estate list (a few requests)")
    bt.add_argument("--order", choices=["flats", "deals", "id"], default="flats",
                    help="largest first by Centadata flats (default) or latest-deal count")
    bt.add_argument("--mapped-only", action="store_true",
                    help="only estates matched to the master list")
    bt.add_argument("--skip-flats", action="store_true", help="skip the 2018-2021 per-flat pass")
    bt.add_argument("--max-estates", type=int, default=None, help="stop after N estates")
    bt.add_argument("--max-hours", type=float, default=None, help="stop after N hours")
    bt.set_defaults(func=cmd_backfill_territory)
    rp = sub.add_parser("ricacorp", help="Ricacorp estate pages: previous-deal pairs (weekly)")
    rp.add_argument("--limit", type=int, default=None, help="only the next N estate pages")
    rp.add_argument("--max-hours", type=float, default=None, help="stop after N hours")
    rp.add_argument("--place-only", action="store_true",
                    help="no requests: place stored deals in buildings and write pairs")
    rp.set_defaults(func=cmd_ricacorp)
    ch = sub.add_parser("cd-history",
                        help="Centadata full per-flat sale history, largest estates first")
    ch.add_argument("--estates", default="all",
                    help="comma-separated estate groups (e.g. taikoo-shing), pilot, or all")
    ch.add_argument("--limit", type=int, default=None, help="only the next N buildings")
    ch.add_argument("--max-hours", type=float, default=None, help="stop after N hours")
    ch.set_defaults(func=cmd_cd_history)
    t = sub.add_parser("train", help="fair-price model: evaluate, fit, write model_eval.md")
    t.add_argument("--from-snapshot", action="store_true",
                   help="reuse data/model/snapshot instead of reading the database")
    t.add_argument("--rounds", type=int, default=800, help="boosting rounds")
    t.add_argument("--since", type=int, default=None, metavar="YEAR",
                   help="training window: sales from this year on (default: all years)")
    t.add_argument("--compare-since", type=int, default=None, metavar="YEAR",
                   help="also evaluate a model trained from this year and report both")
    t.add_argument("--previous", type=Path, default=None, metavar="DIR",
                   help="folder with an earlier fair_price.txt/.json to compare against")
    t.set_defaults(func=cmd_train)
    rk = sub.add_parser("rank", help="rank active sale listings by discount to fair price")
    rk.add_argument("--from-snapshot", action="store_true",
                    help="reuse data/model/snapshot instead of reading the database")
    rk.set_defaults(func=cmd_rank)
    rpt = sub.add_parser("report", help="growth study + findings report in data/reports/")
    rpt.add_argument("--from-snapshot", action="store_true",
                     help="reuse data/model/snapshot instead of reading the database")
    rpt.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                     help="only pairs bought on or after this date (default: all data)")
    rpt.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                     help="only pairs sold (or valued) on or before this date")
    rpt.add_argument("--settings", default=None, metavar="LIST",
                     help="run preset windows from one snapshot: 'standard' or a "
                          "comma-separated list of names (all, since-2000, crash-1997-2003, "
                          "recovery-2003-2008, boom-2008-2021, fall-2021-2024, "
                          "recent-2024-now, bought-after-crash); results go to "
                          "data/reports/growth/<date>/<setting>/")
    rpt.set_defaults(func=cmd_report)
    dc = sub.add_parser("datacheck", help="data checks on the snapshot: datacheck_<date>.md")
    dc.add_argument("--read-db", action="store_true",
                    help="take a fresh snapshot from the database first (default: reuse "
                         "data/model/snapshot, never opening the database)")
    dc.add_argument("--no-fetch", action="store_true",
                    help="skip the Land Registry download (no independent yearly count)")
    dc.set_defaults(func=cmd_datacheck)
    s = sub.add_parser("status", help="recent runs and row counts")
    s.set_defaults(func=cmd_status)

    li = sub.add_parser("listings", help="snapshot listings (Centanet, 28Hse, Spacious)")
    li.add_argument("--estates", default="pilot",
                    help="comma-separated groups, or pilot / territory / all")
    _budgets(li)
    li.add_argument("--sources", default="centanet,hse28,spacious")
    li.set_defaults(func=cmd_listings)

    d = sub.add_parser("daily", help="scheduled daily job: all listing sources + cache prune")
    d.add_argument("--estates", default="all",
                   help="comma-separated groups, or pilot / territory / all (default)")
    _budgets(d)
    d.set_defaults(func=cmd_daily)

    u = sub.add_parser("update", help="weekly refresh of the newest 28Hse deals")
    u.add_argument("--estates", default="pilot", help="comma-separated names, or 'pilot'")
    u.set_defaults(func=cmd_update)

    cb = sub.add_parser("check-blocked", help="monthly: is any blocked site's robots.txt open now?")
    cb.set_defaults(func=cmd_check_blocked)
    pr = sub.add_parser("prune-cache", help="delete cached listing pages older than N days")
    pr.add_argument("--days", type=int, default=7)
    pr.set_defaults(func=cmd_prune)

    wd = sub.add_parser("watchdog",
                        help="restart long jobs and today's daily run if they died (no DB)")
    wd.add_argument("--dry-run", action="store_true",
                    help="log the decisions, start nothing")
    wd.set_defaults(func=cmd_watchdog)

    argv = sys.argv[1:] if argv is None else list(argv)
    args = p.parse_args(argv)
    _argv[:] = argv
    # the long jobs wait for the database file for as long as it takes; others 30 minutes
    before = db.lock_wait()
    db.set_lock_wait(None if args.command in LONG_JOBS else db.LOCK_WAIT_S)
    try:
        return args.func(args)
    finally:
        db.set_lock_wait(before)


if __name__ == "__main__":
    sys.exit(main())
