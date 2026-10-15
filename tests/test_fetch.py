"""Polite fetcher behaviour, tested offline against httpx.MockTransport."""

import gzip
import hashlib
from datetime import date, timedelta

import httpx
import pytest

from hkestate.fetch import USER_AGENT, Fetcher, FetchError, RobotsDisallowed, SourceBlocked

ROBOTS = "User-agent: *\nDisallow: /private/\n"


class FakeClock:
    """Monotonic clock that only moves when the fetcher sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_fetcher(
    tmp_path,
    handler,
    *,
    today=date(2026, 10, 16),
    jitter=0.0,
    clock=None,
    robots=ROBOTS,
    robots_status=200,
):
    """Fetcher with a mock transport; `handler(request)` answers everything but robots.txt."""
    requests = []

    def dispatch(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots)
        return handler(request)

    clock = clock or FakeClock()
    fetcher = Fetcher(
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(dispatch),
        sleep=clock.sleep,
        clock=clock.time,
        today=lambda: today,
        jitter=lambda: jitter,
    )
    return fetcher, requests, clock


def page_requests(requests):
    return [r for r in requests if r.url.path != "/robots.txt"]


def ok(request):
    return httpx.Response(200, text="<html>ok</html>")


def test_robots_disallowed_path_is_refused_without_request(tmp_path):
    fetcher, requests, _ = make_fetcher(tmp_path, ok)
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://example.com/private/page", "static")
    assert page_requests(requests) == []


def test_allowed_path_is_fetched_with_our_user_agent(tmp_path):
    fetcher, requests, _ = make_fetcher(tmp_path, ok)
    assert fetcher.get("https://example.com/ok", "static") == "<html>ok</html>"
    assert all(r.headers["user-agent"] == USER_AGENT for r in requests)
    assert USER_AGENT == "hk-estate-research/0.1 (personal, non-commercial)"


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/centanetapi/api/x", False),
        ("/fooTest", False),  # Disallow: /*Test (wildcard)
        ("/list?a=1&search_words_thing=x", False),  # Disallow: /*search_words_thing=
        ("/for-rent/page/1", True),  # Allow: /for-rent/page/1$ (end anchor, longest match)
        ("/for-rent/page/10", False),
        ("/estate/123", True),
    ],
)
def test_robots_wildcards_and_end_anchors_are_honoured(tmp_path, path, allowed):
    robots = (
        "User-agent: *\n"
        "Disallow: /centanetapi/api/\n"
        "Disallow: /*Test\n"
        "Disallow: /*search_words_thing=\n"
        "Allow: /for-rent/page/1$\n"
        "Disallow: /for-rent/page/\n"
    )
    fetcher, _, _ = make_fetcher(tmp_path, ok, robots=robots)
    assert fetcher.allowed("https://example.com" + path) is allowed


def test_crawl_delay_longer_than_one_second_is_honoured(tmp_path):
    clock = FakeClock()
    times = []

    def handler(request):
        times.append(clock.now)
        return ok(request)

    robots = "User-agent: *\nDisallow:\nCrawl-delay: 30\n"
    fetcher, _, _ = make_fetcher(tmp_path, handler, clock=clock, robots=robots)
    fetcher.get("https://example.com/a", "static")
    fetcher.get("https://example.com/b", "static")
    assert times[1] - times[0] >= 30.0


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_robots_txt_forbidden_or_server_error_means_disallow_all(tmp_path, status):
    fetcher, requests, _ = make_fetcher(tmp_path, ok, robots="", robots_status=status)
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://example.com/anything", "static")
    assert page_requests(requests) == []
    assert fetcher.robots_status("https://example.com/") == status


def test_missing_robots_txt_allows_everything(tmp_path):
    def dispatch(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return ok(request)

    clock = FakeClock()
    fetcher = Fetcher(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(dispatch),
        sleep=clock.sleep,
        clock=clock.time,
        today=lambda: date(2026, 10, 16),
        jitter=lambda: 0.0,
    )
    assert fetcher.get("https://example.com/private/x", "static") == "<html>ok</html>"


@pytest.mark.parametrize("jitter", [0.0, 0.5])
def test_at_least_one_second_plus_jitter_between_requests_to_same_host(tmp_path, jitter):
    times = []
    clock = FakeClock()

    def handler(request):
        times.append(clock.now)
        return ok(request)

    fetcher, _, _ = make_fetcher(tmp_path, handler, jitter=jitter, clock=clock)
    fetcher.get("https://example.com/a", "listing")
    fetcher.get("https://example.com/b", "listing")
    fetcher.get("https://example.com/c", "listing")
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(g >= 1.0 + jitter - 1e-9 for g in gaps)


def test_default_jitter_is_between_zero_and_half_a_second(tmp_path):
    fetcher = Fetcher(cache_dir=tmp_path)
    samples = [fetcher._jitter() for _ in range(200)]
    assert all(0.0 <= s <= 0.5 for s in samples)


def test_different_hosts_do_not_wait_for_each_other(tmp_path):
    clock = FakeClock()
    times = {}

    def handler(request):
        times[request.url.host] = clock.now
        return ok(request)

    fetcher, _, _ = make_fetcher(tmp_path, handler, clock=clock)
    fetcher.allowed("https://b.example.com/x")  # read b's robots.txt up front
    fetcher.get("https://a.example.com/x", "static")
    fetcher.get("https://b.example.com/x", "static")
    # b's page request does not wait for a's: both happen 1 s after their own robots.txt.
    assert times["b.example.com"] == times["a.example.com"]


@pytest.mark.parametrize(
    ("page_type", "age_days", "expect_request"),
    [
        ("listing", 0, False),
        ("listing", 1, True),
        ("transaction", 6, False),
        ("transaction", 7, True),
        ("static", 29, False),
        ("static", 30, True),
    ],
)
def test_cache_lifetime_per_page_type(tmp_path, page_type, age_days, expect_request):
    url = "https://example.com/page"
    day0 = date(2026, 10, 1)
    first, _, _ = make_fetcher(tmp_path, ok, today=day0)
    first.get(url, page_type)

    later, requests, _ = make_fetcher(tmp_path, ok, today=day0 + timedelta(days=age_days))
    assert later.get(url, page_type) == "<html>ok</html>"
    assert bool(page_requests(requests)) is expect_request


def test_cache_file_is_gzip_under_host_date_sha1(tmp_path):
    url = "https://example.com/page?id=1"
    fetcher, _, _ = make_fetcher(tmp_path, ok)
    fetcher.get(url, "static")
    sha = hashlib.sha1(url.encode()).hexdigest()
    path = tmp_path / "cache" / "example.com" / "2026-10-16" / f"{sha}.html.gz"
    assert path.exists()
    assert gzip.decompress(path.read_bytes()) == b"<html>ok</html>"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_errors_back_off_exponentially_then_succeed(tmp_path, status):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) <= 2:
            return httpx.Response(status)
        return ok(request)

    fetcher, _, clock = make_fetcher(tmp_path, handler)
    assert fetcher.get("https://example.com/x", "static") == "<html>ok</html>"
    assert len(calls) == 3
    backoffs = [s for s in clock.sleeps if s >= 2.0]
    assert backoffs == [2.0, 4.0]


def test_gives_up_after_three_retries(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    fetcher, _, _ = make_fetcher(tmp_path, handler)
    with pytest.raises(FetchError) as excinfo:
        fetcher.get("https://example.com/x", "static")
    assert excinfo.value.status == 503
    assert len(calls) == 4  # first try + 3 retries


def test_403_raises_source_blocked_and_host_stays_blocked(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403)

    fetcher, _, _ = make_fetcher(tmp_path, handler)
    with pytest.raises(SourceBlocked):
        fetcher.get("https://example.com/x", "static")
    with pytest.raises(SourceBlocked):
        fetcher.get("https://example.com/y", "static")
    assert len(calls) == 1  # the second call never reached the site


@pytest.mark.parametrize(
    "body",
    [
        "<html><head><title>Just a moment...</title></head></html>",
        "<html><head><title>Attention Required! | Cloudflare</title></head></html>",
        "<html><body><div id='px-captcha'></div></body></html>",
        "<html><head><title>CAPTCHA</title></head></html>",
    ],
)
def test_captcha_page_raises_source_blocked(tmp_path, body):
    fetcher, _, _ = make_fetcher(tmp_path, lambda r: httpx.Response(200, text=body))
    with pytest.raises(SourceBlocked):
        fetcher.get("https://example.com/x", "static")
    # A challenge page is never cached.
    assert not list((tmp_path / "cache").rglob("*.html.gz"))


def test_404_raises_fetch_error_without_retry(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    fetcher, _, _ = make_fetcher(tmp_path, handler)
    with pytest.raises(FetchError):
        fetcher.get("https://example.com/missing", "static")
    assert len(calls) == 1


def test_unknown_page_type_is_rejected(tmp_path):
    fetcher, _, _ = make_fetcher(tmp_path, ok)
    with pytest.raises(ValueError):
        fetcher.get("https://example.com/x", "weekly")
