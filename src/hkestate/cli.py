"""`hkestate` command line.

    hkestate backfill --estates taikoo-shing,goldwin-heights   (or --estates pilot)
    hkestate listings [--sources centanet,hse28,spacious]      listing snapshots
    hkestate daily                                              scheduled: listings + prune
    hkestate update                                             scheduled weekly: new deals
    hkestate backfill-territory [--discover]   28Hse, whole territory, largest first
    hkestate ricacorp [--max-hours N]          Ricacorp previous-deal pairs (weekly)
    hkestate link-ids                          match 28Hse ids to master ids
    hkestate status

Every run is logged to data/logs/<command>_<time>.log and to `run_log` in the database.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from hkestate import db, locks
from hkestate.config import get_data_dir


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
                centanet_budget=args.centanet_budget, spacious_budget=args.spacious_budget)
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
    # 2 s between requests: hk.centanet.com is shared with the Centanet listing scraper
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
    with Fetcher(min_interval=ricacorp.MIN_INTERVAL_S) as f:
        ricacorp.run_ricacorp(f, db_path=args.db, log=log, limit=args.limit,
                              max_hours=args.max_hours)
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


def _budgets(parser) -> None:
    from hkestate import listing_run

    parser.add_argument("--centanet-budget", type=int,
                        default=listing_run.CENTANET_TERRITORY_BUDGET,
                        help="territory detail pages read per run (new listings first)")
    parser.add_argument("--spacious-budget", type=int,
                        default=listing_run.SPACIOUS_TERRITORY_BUDGET,
                        help="territory Spacious building pages read per run")


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

    pr = sub.add_parser("prune-cache", help="delete cached listing pages older than N days")
    pr.add_argument("--days", type=int, default=7)
    pr.set_defaults(func=cmd_prune)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
