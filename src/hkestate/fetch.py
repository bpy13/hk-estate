"""Polite HTTP fetcher: robots.txt, per-host rate limit, gzip page cache, back-off.

Every page the project reads goes through `Fetcher.get`. It:
- refuses any URL the site's robots.txt disallows for our user agent (`RobotsDisallowed`);
- waits at least 1 s (or the site's robots.txt Crawl-delay, if longer) plus 0-0.5 s random
  jitter between requests to the same host;
- follows redirects by hand (max 5), checking robots.txt and pacing for every hop;
- returns a cached copy if one is younger than the page type's lifetime (no request made);
- retries 500/502/504 up to 3 times with exponential back-off (2 s, 4 s, 8 s);
- adapts the pace per host when the site pushes back (see "Adaptive pacing" below);
- raises `SourceBlocked` on 403 or a CAPTCHA/challenge page, and refuses that host for the
  rest of the run, so we never try to get around a block.

Adaptive pacing (process-local, per host). The base gap is the configured `min_interval`
(never below 1 s or the robots.txt Crawl-delay). A throttle signal (HTTP 429 or 503, a 5xx
still failing after the retries, or a timeout) multiplies the host's gap by 1.5, up to 15 s.
A 429/503 also starts a cool-down for the host (15 min, then 30, 60, then 2 h each time;
Retry-After is honoured if longer): the next request to that host waits until it is over and
is a single probe. `throttle_probes` (default 1) is how many probes one call makes before it
raises FetchError; a caller that retries the item later still waits out the cool-down.
After 100 good answers in a row the gap steps 25% back towards the base, never below it.
Each change is logged once (logger `hkestate.fetch`), e.g. "hk.centanet.com: slowing to
3.0 s after HTTP 429".

Downloaded pages are untrusted data: they are only stored and parsed, never executed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from protego import Protego

from hkestate.config import USER_AGENT, get_data_dir

__all__ = [
    "USER_AGENT",
    "PAGE_LIFETIME_DAYS",
    "Fetcher",
    "FetchError",
    "RobotsDisallowed",
    "SourceBlocked",
]

# How long a cached page is reused before it is fetched again, by page type.
PAGE_LIFETIME_DAYS = {"listing": 1, "transaction": 7, "static": 30}

MIN_INTERVAL_S = 1.0
MAX_JITTER_S = 0.5
MAX_RETRIES = 3
ROBOTS_RETRIES = 2  # robots.txt: retries on 5xx or a network error before staying away
BACKOFF_BASE_S = 2.0
MAX_RETRY_AFTER_S = 120.0
RETRYABLE = {500, 502, 504}  # retried with back-off inside one call
THROTTLED = {429, 503}  # cool-down, then a single probe
REDIRECTS = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
# Adaptive pacing (module docstring). Centanet's Transaction/Search API kept answering 429
# 11 hours after the first ones (2026-10-20), so repeated cool-downs grow to 2 hours.
SLOWDOWN_FACTOR = 1.5
MAX_INTERVAL_S = 15.0
SPEEDUP_AFTER = 100  # good answers in a row before the gap steps back down
SPEEDUP_FACTOR = 0.75  # 15 s back to 2 s in ~700 good answers (0.9 per 500 took ~16 h)
COOLDOWN_RESET_AFTER = 100  # good answers in a row before cool-downs start again at the first
COOLDOWNS_S = (15 * 60, 30 * 60, 60 * 60, 120 * 60)  # the last one repeats
MAX_COOLDOWN_S = 120 * 60  # Retry-After is honoured up to this

log = logging.getLogger(__name__)

HK_TZ = ZoneInfo("Asia/Hong_Kong")

# Signs that a site served a bot challenge instead of the page. Kept specific on purpose:
# Cloudflare injects challenge scripts into normal pages too, so we look at the <title> and a
# few block-page-only markers rather than any mention of "captcha".
_CHALLENGE_TITLE = re.compile(
    r"<title[^>]*>[^<]*(just a moment|attention required|captcha|access denied|"
    r"are you a robot|security check|請稍候|驗證)[^<]*</title>",
    re.IGNORECASE,
)
_CHALLENGE_MARKERS = ("px-captcha", "_Incapsula_Resource", "cf-chl-widget", "g-recaptcha-response")


class FetchError(Exception):
    """A request failed in a way that is not a block (e.g. 404, retries used up)."""

    def __init__(self, url: str, status: int | None, message: str = ""):
        self.url = url
        self.status = status
        super().__init__(message or f"{status} for {url}")


class SourceBlocked(Exception):
    """The site answered 403 or a CAPTCHA/challenge. Stop using this source for the run."""


class RobotsDisallowed(Exception):
    """robots.txt does not allow our user agent to fetch this URL."""


def _hk_today() -> date:
    return datetime.now(HK_TZ).date()


def _retry_after_s(response: httpx.Response) -> float | None:
    """Retry-After in seconds (a number or an HTTP date), or None."""
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - datetime.now(when.tzinfo)).total_seconds())


def looks_like_challenge(text: str) -> bool:
    head = text[:20000]
    return bool(_CHALLENGE_TITLE.search(head)) or any(m in head for m in _CHALLENGE_MARKERS)


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        # Older HK sites still serve Big5; HKSCS is its superset.
        return content.decode("big5hkscs", errors="replace")


class Fetcher:
    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = _hk_today,
        jitter: Callable[[], float] | None = None,
        min_interval: float = MIN_INTERVAL_S,
        cooldowns_s: tuple[float, ...] = COOLDOWNS_S,
        throttle_probes: int = 1,
    ):
        # Long runs can ask for a wider gap than the 1 s minimum (some sites answer 429
        # after a long stretch at 1 request/s). robots.txt Crawl-delay still wins if longer.
        self.min_interval = max(min_interval, MIN_INTERVAL_S)
        self.cooldowns_s = tuple(cooldowns_s)
        self.throttle_probes = throttle_probes
        self._pace: dict[str, float] = {}  # per host: the adapted gap, when above the base
        self._ok_streak: dict[str, int] = {}
        self._cooldowns: dict[str, int] = {}  # per host: cool-downs since the last good run
        self._cooldown_until: dict[str, float] = {}
        self.cache_dir = Path(cache_dir) if cache_dir else get_data_dir() / "cache"
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,  # redirects are followed by hand in _request
        )
        self._sleep = sleep
        self._clock = clock
        self._today = today
        self._jitter = jitter or (lambda: random.uniform(0.0, MAX_JITTER_S))
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, Protego] = {}
        self._robots_text: dict[str, str] = {}
        self._robots_status: dict[str, int] = {}
        self._min_interval: dict[str, float] = {}  # per host, raised by robots Crawl-delay
        self._blocked: set[str] = set()
        self.requests_made = 0

    # ---- public -------------------------------------------------------------------------

    def get(self, url: str, page_type: str) -> str:
        """Return the page text, from cache when fresh enough, else from the site."""
        return _decode(self.get_bytes(url, page_type))

    def get_bytes(self, url: str, page_type: str) -> bytes:
        return self._fetch(url, page_type, None)

    def post_json(self, url: str, body: dict, page_type: str) -> str:
        """POST a JSON body and return the response text.

        Treated exactly like a GET: robots.txt is checked for the URL, the host's delay and
        retries apply, and the response is cached under a key made of the URL plus the body
        (so the same question is never asked twice).
        """
        return _decode(self.post_json_bytes(url, body, page_type))

    def post_json_bytes(self, url: str, body: dict, page_type: str) -> bytes:
        return self._fetch(url, page_type, body)

    @staticmethod
    def post_key(url: str, body: dict) -> str:
        """Cache key of a JSON POST: the URL plus the body in a fixed form."""
        return url + "\n" + json.dumps(body, sort_keys=True, separators=(",", ":"))

    def _fetch(self, url: str, page_type: str, body: dict | None) -> bytes:
        if page_type not in PAGE_LIFETIME_DAYS:
            raise ValueError(f"unknown page type {page_type!r}; use {sorted(PAGE_LIFETIME_DAYS)}")
        host = urlsplit(url).hostname or ""
        key = url if body is None else self.post_key(url, body)
        cached = self._read_cache(host, key, PAGE_LIFETIME_DAYS[page_type])
        if cached is not None:
            return cached
        if host in self._blocked:
            raise SourceBlocked(f"{host} blocked earlier in this run")
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        response = self._request(url, host, body)
        self._write_cache(host, key, response.content)
        return response.content

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(url, USER_AGENT)

    def robots_txt(self, url: str) -> str:
        """The raw robots.txt text for the URL's host ('' if the site has none)."""
        self._robots_for(url)
        return self._robots_text[self._origin(url)]

    def robots_status(self, url: str) -> int:
        """HTTP status the host's robots.txt returned."""
        self._robots_for(url)
        return self._robots_status[self._origin(url)]

    def forget(self, url: str) -> int:
        """Delete the cached copies of a URL (for pages too big or too personal to keep once
        parsed). Returns the number of files removed."""
        host = urlsplit(url).hostname or ""
        removed = 0
        today = self._today()
        for age in range(max(PAGE_LIFETIME_DAYS.values())):
            path = self._cache_path(host, url, today - timedelta(days=age))
            if path.exists():
                path.unlink()
                removed += 1
        return removed

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- robots.txt ---------------------------------------------------------------------

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def _robots_for(self, url: str) -> Protego:
        """Parsed robots.txt for the URL's host, fetched once per run.

        Uses protego, which understands `*` and `$` wildcards and longest-match precedence
        (Python's urllib.robotparser silently ignores wildcards). A missing robots.txt
        (404/410) allows everything. We are stricter than RFC 9309 on 401/403 (it would
        allow): a site that refuses us its robots.txt gets nothing fetched. A server error
        or network failure that persists after ROBOTS_RETRIES retries means disallow-all, as
        the RFC says.
        """
        origin = self._origin(url)
        if origin not in self._robots:
            host = urlsplit(url).hostname or ""
            status, content = self._fetch_robots(origin, host)
            text = ""
            if status in (401, 403) or status >= 500 or status == 0:
                parser = Protego.parse("User-agent: *\nDisallow: /\n")
            elif status >= 400:
                parser = Protego.parse("")
            else:
                text = _decode(content)
                parser = Protego.parse(text)
            delay = parser.crawl_delay(USER_AGENT)
            if delay and delay > MIN_INTERVAL_S:
                self._min_interval[host] = float(delay)
            self._robots[origin] = parser
            self._robots_text[origin] = text
            self._robots_status[origin] = status
        return self._robots[origin]

    def _fetch_robots(self, origin: str, host: str) -> tuple[int, bytes]:
        """GET robots.txt, retrying a server error or network error up to ROBOTS_RETRIES times.

        One transient failure would otherwise shut the host out for the whole run. Returns
        (status, body); status 0 means the network failed on every try. 401/403/404 are final
        answers and are not retried.
        """
        status, content = 0, b""
        for attempt in range(ROBOTS_RETRIES + 1):
            if attempt:
                self._sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
            self._wait_for_host(host)
            self.requests_made += 1
            try:
                response = self._client.get(origin + "/robots.txt", follow_redirects=True)
            except httpx.TransportError:
                status, content = 0, b""
                continue
            status, content = response.status_code, response.content
            if status < 500:
                break
        return status, content

    # ---- requests -----------------------------------------------------------------------

    def base_interval(self, host: str) -> float:
        """The configured gap for the host (robots.txt Crawl-delay if longer)."""
        return max(self._min_interval.get(host, 0.0), self.min_interval)

    def interval(self, host: str) -> float:
        """The gap in use now: the base, or more after the site pushed back."""
        return max(self.base_interval(host), self._pace.get(host, 0.0))

    def cooldown_remaining(self, url_or_host: str) -> float:
        """Seconds until the host's cool-down is over (0 if none)."""
        host = urlsplit(url_or_host).hostname or url_or_host
        return max(0.0, self._cooldown_until.get(host, 0.0) - self._clock())

    def _wait_for_host(self, host: str) -> None:
        left = self.cooldown_remaining(host)
        if left > 0:
            self._sleep(left)
            log.info("%s: cool-down over; probing with one request", host)
        last = self._last_request.get(host)
        if last is not None:
            wait = last + self.interval(host) + self._jitter() - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    # ---- adaptive pacing ----------------------------------------------------------------

    def _slow_down(self, host: str, reason: str) -> None:
        old = self.interval(host)
        new = min(max(old * SLOWDOWN_FACTOR, self.base_interval(host)), MAX_INTERVAL_S)
        self._ok_streak[host] = 0
        if new > old:
            self._pace[host] = new
            log.warning("%s: slowing to %.1f s after %s", host, new, reason)

    def _start_cooldown(self, host: str, status: int, response: httpx.Response) -> None:
        n = self._cooldowns.get(host, 0)
        self._cooldowns[host] = n + 1
        if not self.cooldowns_s:
            return
        cool = float(self.cooldowns_s[min(n, len(self.cooldowns_s) - 1)])
        retry_after = _retry_after_s(response)
        if retry_after is not None:
            cool = max(cool, min(retry_after, MAX_COOLDOWN_S))
        self._cooldown_until[host] = self._clock() + cool
        until = datetime.now() + timedelta(seconds=cool)
        log.warning("%s: HTTP %s; cooling down %.0f min (until %s), then one probe request",
                    host, status, cool / 60, f"{until:%H:%M}")

    def _answered(self, host: str) -> None:
        """A good answer: count towards stepping the gap back down."""
        streak = self._ok_streak.get(host, 0) + 1
        self._ok_streak[host] = streak
        if streak >= COOLDOWN_RESET_AFTER:
            self._cooldowns.pop(host, None)
        pace = self._pace.get(host)
        if pace is None or streak < SPEEDUP_AFTER:
            return
        self._ok_streak[host] = 0
        base = self.base_interval(host)
        new = max(base, pace * SPEEDUP_FACTOR)
        if new <= base:
            self._pace.pop(host, None)
        else:
            self._pace[host] = new
        log.info("%s: speeding up to %.1f s", host, new)

    def _request(self, url: str, host: str, body: dict | None = None) -> httpx.Response:
        """GET a URL (or POST a JSON body), following redirects by hand (at most MAX_REDIRECTS).

        Every hop gets the same treatment as a first request: the target's robots.txt is
        checked (a disallowed target is never requested: `RobotsDisallowed`) and its host's
        delay applies. The caller keeps caching under the original URL.
        """
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            current_host = urlsplit(current).hostname or ""
            if current_host in self._blocked:
                raise SourceBlocked(f"{current_host} blocked earlier in this run")
            response = self._request_once(current, current_host, body)
            location = response.headers.get("location")
            if response.status_code not in REDIRECTS or not location:
                return response
            if hop == MAX_REDIRECTS:
                break
            target = urljoin(current, location)
            if urlsplit(target).scheme not in ("http", "https"):
                raise FetchError(url, response.status_code, f"redirect to {target!r} from {url}")
            if not self.allowed(target):
                log.warning("redirect %s -> %s refused: robots.txt disallows it", current, target)
                raise RobotsDisallowed(target)
            current = target
        raise FetchError(url, None, f"more than {MAX_REDIRECTS} redirects from {url}")

    def _request_once(self, url: str, host: str, body: dict | None = None) -> httpx.Response:
        retries = probes = 0
        while True:
            self._wait_for_host(host)
            try:
                if body is None:
                    response = self._client.get(url)
                else:
                    response = self._client.post(url, json=body)
            except httpx.TimeoutException:
                self.requests_made += 1
                self._slow_down(host, "a timeout")
                raise
            self.requests_made += 1
            status = response.status_code
            if status == 403:
                self._blocked.add(host)
                raise SourceBlocked(f"403 from {url}")
            if status in THROTTLED:
                self._slow_down(host, f"HTTP {status}")
                self._start_cooldown(host, status, response)
                if probes >= self.throttle_probes:
                    raise FetchError(url, status, f"{status} (cooling down): {url}")
                probes += 1
                continue
            if status in RETRYABLE:
                if retries == MAX_RETRIES:
                    self._slow_down(host, f"HTTP {status} {MAX_RETRIES + 1} times")
                    raise FetchError(url, status, f"{status} after {MAX_RETRIES} retries: {url}")
                self._sleep(self._backoff(retries, response))
                retries += 1
                continue
            self._answered(host)  # the site answered normally (a 404 counts)
            if status >= 400:
                raise FetchError(url, status)
            if status not in REDIRECTS and looks_like_challenge(_decode(response.content)):
                self._blocked.add(host)
                raise SourceBlocked(f"challenge/CAPTCHA page from {url}")
            return response

    @staticmethod
    def _backoff(attempt: int, response: httpx.Response) -> float:
        delay = BACKOFF_BASE_S * (2**attempt)
        retry_after = _retry_after_s(response)
        if retry_after is not None:
            delay = max(delay, min(retry_after, MAX_RETRY_AFTER_S))
        return delay

    # ---- cache --------------------------------------------------------------------------

    def _cache_path(self, host: str, url: str, day: date) -> Path:
        sha = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / host / day.isoformat() / f"{sha}.html.gz"

    def _read_cache(self, host: str, url: str, lifetime_days: int) -> bytes | None:
        today = self._today()
        for age in range(lifetime_days):
            path = self._cache_path(host, url, today - timedelta(days=age))
            if path.exists():
                return gzip.decompress(path.read_bytes())
        return None

    def _write_cache(self, host: str, url: str, content: bytes) -> None:
        path = self._cache_path(host, url, self._today())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(content))
