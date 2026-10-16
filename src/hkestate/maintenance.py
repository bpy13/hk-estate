"""Housekeeping run by the daily job.

Cached listing pages show agents' names, licence numbers and phones (the parsers skip them
and the database never stores them). To keep personal data on disk short-lived, the daily
job deletes cache folders 7 days old or more for the listing sites. The cache is laid out as
`cache/<host>/<YYYY-MM-DD>/<sha1>.html.gz`, so whole day folders go. Transaction pages on
those hosts live only 7 days in the cache anyway, so nothing still in use is lost.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

# Hosts whose cached pages can contain agent details.
LISTING_HOSTS = ("hk.centanet.com", "www.28hse.com", "www.spacious.hk")
KEEP_DAYS = 7


def prune_listing_cache(
    cache_dir: Path, *, today: date, keep_days: int = KEEP_DAYS, hosts=LISTING_HOSTS
) -> int:
    """Delete day folders `keep_days` or more days old. Returns the number of files removed."""
    removed = 0
    for host in hosts:
        root = Path(cache_dir) / host
        if not root.is_dir():
            continue
        for folder in root.iterdir():
            try:
                day = date.fromisoformat(folder.name)
            except ValueError:
                continue
            if folder.is_dir() and (today - day).days >= keep_days:
                removed += sum(1 for p in folder.rglob("*") if p.is_file())
                shutil.rmtree(folder)
    return removed
