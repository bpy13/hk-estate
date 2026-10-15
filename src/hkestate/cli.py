"""`hkestate` command line.

    hkestate backfill --estates taikoo-shing,goldwin-heights   (or --estates pilot)
    hkestate status

Every run is logged to data/logs/<command>_<time>.log and to `run_log` in the database.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from hkestate import db
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
    added = backfill_estates(
        {n: PILOT_ESTATES[n] for n in names},
        db_path=args.db,
        log=log,
        refresh=args.refresh,
        skip_flats=args.skip_flats,
    )
    log(f"done: {added} rows added to txn_raw")
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
            LEFT JOIN txn c USING (source, source_id)
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall():
            print("  ", *row)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hkestate")
    p.add_argument("--db", default=None, help="database file (default data/hkestate.duckdb)")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backfill", help="load transaction history from 28Hse")
    b.add_argument("--estates", required=True, help="comma-separated names, or 'pilot'")
    b.add_argument("--refresh", action="store_true", help="redo finished steps")
    b.add_argument("--skip-flats", action="store_true", help="skip the per-flat pages")
    b.set_defaults(func=cmd_backfill)

    s = sub.add_parser("status", help="recent runs and row counts")
    s.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
