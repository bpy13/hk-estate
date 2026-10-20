"""What a job does when a site pushes back: wait, then try the same item again.

The fetcher already slows down and, on HTTP 429/503, starts a cool-down for the host that
the next request waits out (fetch.py). `Patience` is the job's side of it:

- `throttle` (HTTP 429/503): the fetcher's cool-down is pending, so the same item is simply
  tried again (that request waits). If the fetcher has no cool-down pending (a test double,
  or a fetcher built without one) the job sleeps its own cool-down first.
- `server` (another 5xx after the fetcher's retries) or `network` (timeout, connection
  error): the same item is tried again; after `streak_limit` such errors in a row the job
  sleeps a cool-down (10, 20, then 30 min). One item that keeps failing this way is skipped
  after `item_tries` tries (it may be that page, not the site); the caller treats it as
  before (counted as an error, retried on the next run).
- Anything else (404, parse errors) is not a throttle signal: `kind()` is None and the caller
  keeps its own handling.

`max_cooldowns=None` waits for ever (the long resumable jobs); the daily listing run passes 3
and gives up on the source for the day after the third.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from hkestate.fetch import THROTTLED, FetchError

JOB_COOLDOWNS_S = (10 * 60, 20 * 60, 30 * 60)  # the last one repeats
STREAK_LIMIT = 5
ITEM_TRIES = 3

RETRY, SKIP, STOP = "retry", "skip", "stop"


def kind(e: BaseException) -> str | None:
    """'throttle', 'server', 'network', or None for an error that is not the site pushing
    back."""
    if isinstance(e, FetchError):
        if e.status in THROTTLED:
            return "throttle"
        if e.status is not None and e.status >= 500:
            return "server"
        return None
    if isinstance(e, httpx.TransportError):
        return "network"
    return None


def describe(e: BaseException) -> str:
    if isinstance(e, FetchError) and e.status is not None:
        return f"HTTP {e.status}"
    return type(e).__name__


class Patience:
    def __init__(self, log: Callable[[str], None], *, what: str, fetcher=None,
                 sleep: Callable[[float], None] = time.sleep,
                 cooldowns_s: tuple[float, ...] = JOB_COOLDOWNS_S,
                 max_cooldowns: int | None = None, streak_limit: int = STREAK_LIMIT,
                 item_tries: int = ITEM_TRIES):
        self.log = log
        self.what = what
        self.fetcher = fetcher
        self.sleep = sleep
        self.cooldowns_s = cooldowns_s
        self.max_cooldowns = max_cooldowns
        self.streak_limit = streak_limit
        self.item_tries = item_tries
        self.streak = 0  # throttle-type errors in a row (any item)
        self.tries = 0  # server/network errors on the current item
        self.cooldowns = 0  # cool-downs so far (the fetcher's and ours)
        self.note = ""  # why the job stopped, after STOP

    def ok(self) -> None:
        """The item worked (or failed for a reason that isn't throttling)."""
        self.streak = 0
        self.tries = 0

    def _cooldown(self, reason: str) -> bool:
        """Count one cool-down; False if that is one too many."""
        self.cooldowns += 1
        if self.max_cooldowns is not None and self.cooldowns > self.max_cooldowns:
            self.note = (f"gave up after {self.max_cooldowns} cool-downs "
                         f"(last: {reason})")
            self.log(f"{self.what}: {self.note}")
            return False
        return True

    def _own_cooldown(self, reason: str) -> None:
        n = min(self.cooldowns - 1, len(self.cooldowns_s) - 1)
        cool = self.cooldowns_s[max(n, 0)]
        self.log(f"{self.what}: {reason}; cooling down {cool / 60:.0f} min, then trying the "
                 "same item again")
        self.sleep(cool)

    def on_error(self, e: BaseException) -> str:
        """RETRY (same item again), SKIP (give up on this item) or STOP (give up on the
        source). Only call it for errors where `kind(e)` is not None."""
        k = kind(e)
        reason = describe(e)
        self.streak += 1
        if k == "throttle":
            self.tries = 0
            if not self._cooldown(reason):
                return STOP
            left = 0.0
            if self.fetcher is not None and hasattr(self.fetcher, "cooldown_remaining"):
                left = self.fetcher.cooldown_remaining(getattr(e, "url", "") or "")
            if left > 0:
                self.log(f"{self.what}: {reason}; waiting out the site's cool-down "
                         f"({left / 60:.0f} min), then trying the same item again")
            else:
                self._own_cooldown(reason)
            return RETRY
        self.tries += 1
        if self.streak >= self.streak_limit:
            if not self._cooldown(f"{self.streak} x {reason} in a row"):
                return STOP
            self._own_cooldown(f"{self.streak} errors in a row ({reason})")
            self.streak = 0
        if self.tries >= self.item_tries:
            self.tries = 0
            self.log(f"{self.what}: {reason} {self.item_tries} times on one item; skipping it "
                     "(retried on the next run)")
            return SKIP
        return RETRY
