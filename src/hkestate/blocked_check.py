"""Monthly check of the sites we don't use because their robots.txt refuses us.

Each blocked site's robots.txt is requested once with our normal User-Agent and the normal
polite fetcher: no evasion of any kind. If a site starts answering 200 the log says so, and
docs/sources.md should be reviewed (a site is only used once its robots.txt allows us).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hkestate import db
from hkestate.fetch import Fetcher


@dataclass(frozen=True)
class Site:
    name: str
    host: str


SITES = (
    Site("midland", "www.midland.com.hk"),
    Site("squarefoot", "www.squarefoot.com.hk"),
    Site("house730", "www.house730.com"),
    Site("hkp", "www.hkp.com.hk"),
)


@dataclass(frozen=True)
class Result:
    site: Site
    status: int  # HTTP status of robots.txt; 0 = network failure


def run_check(fetcher: Fetcher, db_path: str | Path | None, log: Callable[[str], None]
              ) -> list[Result]:
    """Request each site's robots.txt once; one `run_log` row (command 'check-blocked') per site."""
    results = []
    for site in SITES:
        with db.session(db_path) as con:
            db.init_db(con)
            run_id = db.start_run(con, "check-blocked", site.name)
        status = fetcher.robots_status(f"https://{site.host}/")
        results.append(Result(site, status))
        log(f"{site.name} ({site.host}): robots.txt answered {status}")
        if status == 200:
            log(f"{site.name}: now reachable, review docs/sources.md")
        message = json.dumps({"host": site.host, "robots_status": status})
        with db.session(db_path) as con:
            db.finish_run(con, run_id, "ok", 0, message)
    return results
