"""Polite HTTP fetcher: robots.txt, per-host rate limit, gzip page cache, back-off.

Every page the project reads goes through `Fetcher.get`. It:
- refuses any URL the site's robots.txt disallows for our user agent (`RobotsDisallowed`);
- waits at least 1 s (or the site's robots.txt Crawl-delay, if longer) plus 0-0.5 s random
  jitter between requests to the same host;
- returns a cached copy if one is younger than the page type's lifetime (no request made);
- retries 429 and 5xx up to 3 times with exponential back-off (2 s, 4 s, 8 s);
- raises `SourceBlocked` on 403 or a CAPTCHA/challenge page, and refuses that host for the
  rest of the run, so we never try to get around a block.

Downloaded pages are untrusted data: they are only stored and parsed, never executed.
"""

from __future__ import annotations

import gzip
import hashlib
import random
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
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
BACKOFF_BASE_S = 2.0
MAX_RETRY_AFTER_S = 120.0
RETRYABLE = {429, 500, 502, 503, 504}

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
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else get_data_dir() / "cache"
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
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
        if page_type not in PAGE_LIFETIME_DAYS:
            raise ValueError(f"unknown page type {page_type!r}; use {sorted(PAGE_LIFETIME_DAYS)}")
        host = urlsplit(url).hostname or ""
        cached = self._read_cache(host, url, PAGE_LIFETIME_DAYS[page_type])
        if cached is not None:
            return cached
        if host in self._blocked:
            raise SourceBlocked(f"{host} blocked earlier in this run")
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        response = self._request(url, host)
        self._write_cache(host, url, response.content)
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
        means disallow-all, as the RFC says.
        """
        origin = self._origin(url)
        if origin not in self._robots:
            host = urlsplit(url).hostname or ""
            self._wait_for_host(host)
            response = self._client.get(origin + "/robots.txt")
            self.requests_made += 1
            status = response.status_code
            text = ""
            if status in (401, 403) or status >= 500:
                parser = Protego.parse("User-agent: *\nDisallow: /\n")
            elif status >= 400:
                parser = Protego.parse("")
            else:
                text = _decode(response.content)
                parser = Protego.parse(text)
            delay = parser.crawl_delay(USER_AGENT)
            if delay and delay > MIN_INTERVAL_S:
                self._min_interval[host] = float(delay)
            self._robots[origin] = parser
            self._robots_text[origin] = text
            self._robots_status[origin] = status
        return self._robots[origin]

    # ---- requests -----------------------------------------------------------------------

    def _wait_for_host(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            interval = self._min_interval.get(host, MIN_INTERVAL_S)
            wait = last + interval + self._jitter() - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _request(self, url: str, host: str) -> httpx.Response:
        for attempt in range(MAX_RETRIES + 1):
            self._wait_for_host(host)
            response = self._client.get(url)
            self.requests_made += 1
            status = response.status_code
            if status == 403:
                self._blocked.add(host)
                raise SourceBlocked(f"403 from {url}")
            if status in RETRYABLE:
                if attempt == MAX_RETRIES:
                    raise FetchError(url, status, f"{status} after {MAX_RETRIES} retries: {url}")
                self._sleep(self._backoff(attempt, response))
                continue
            if status >= 400:
                raise FetchError(url, status)
            if looks_like_challenge(_decode(response.content)):
                self._blocked.add(host)
                raise SourceBlocked(f"challenge/CAPTCHA page from {url}")
            return response
        raise AssertionError("unreachable")

    @staticmethod
    def _backoff(attempt: int, response: httpx.Response) -> float:
        delay = BACKOFF_BASE_S * (2**attempt)
        retry_after = response.headers.get("retry-after", "")
        if retry_after.isdigit():
            delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_S))
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
