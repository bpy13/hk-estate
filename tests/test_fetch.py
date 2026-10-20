"""Polite fetcher behaviour, tested offline against httpx.MockTransport."""

import gzip
import hashlib
import json
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


def _robots_flaky_fetcher(tmp_path, robots_answers):
    """Fetcher whose robots.txt answers come from `robots_answers` in order (a status or an
    exception instance); every later robots.txt request gets the last answer."""
    calls = []

    def dispatch(request):
        if request.url.path == "/robots.txt":
            answer = robots_answers[min(len(calls), len(robots_answers) - 1)]
            calls.append(answer)
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(answer, text=ROBOTS if answer == 200 else "")
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
    return fetcher, calls, clock


@pytest.mark.parametrize(
    "first_failure", [503, 500, httpx.ConnectError("boom"), httpx.ReadTimeout("slow")]
)
def test_robots_txt_is_retried_after_server_or_network_error(tmp_path, first_failure):
    fetcher, calls, _ = _robots_flaky_fetcher(tmp_path, [first_failure, 200])
    assert fetcher.get("https://example.com/ok", "static") == "<html>ok</html>"
    assert len(calls) == 2
    assert fetcher.robots_status("https://example.com/") == 200


def test_robots_txt_gives_up_after_two_retries_and_disallows(tmp_path):
    fetcher, calls, clock = _robots_flaky_fetcher(tmp_path, [503, 503, 503, 200])
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://example.com/ok", "static")
    assert len(calls) == 3  # first try + 2 retries
    assert any(s >= 2.0 for s in clock.sleeps)  # backed off between tries


def test_robots_txt_network_error_after_retries_disallows(tmp_path):
    fetcher, calls, _ = _robots_flaky_fetcher(tmp_path, [httpx.ConnectError("down")])
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://example.com/ok", "static")
    assert len(calls) == 3


def test_robots_txt_403_is_not_retried(tmp_path):
    fetcher, calls, _ = _robots_flaky_fetcher(tmp_path, [403, 200])
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://example.com/ok", "static")
    assert len(calls) == 1


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


def test_min_interval_can_be_raised_for_long_runs(tmp_path):
    clock = FakeClock()
    times = []

    def dispatch(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        times.append(clock.now)
        return ok(request)

    fetcher = Fetcher(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(dispatch),
        sleep=clock.sleep,
        clock=clock.time,
        today=lambda: date(2026, 10, 16),
        jitter=lambda: 0.0,
        min_interval=2.5,
    )
    fetcher.get("https://example.com/a", "static")
    fetcher.get("https://example.com/b", "static")
    assert times[1] - times[0] >= 2.5


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


@pytest.mark.parametrize("status", [500, 502, 504])
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
        return httpx.Response(500)

    fetcher, _, _ = make_fetcher(tmp_path, handler)
    with pytest.raises(FetchError) as excinfo:
        fetcher.get("https://example.com/x", "static")
    assert excinfo.value.status == 500
    assert len(calls) == 4  # first try + 3 retries


# ---- adaptive pacing ---------------------------------------------------------------------


def _gaps(times):
    return [round(b - a, 6) for a, b in zip(times, times[1:], strict=False)]


def paced_fetcher(tmp_path, answers, *, min_interval=2.0, **kw):
    """Fetcher whose page requests get the statuses in `answers` (then 200); returns
    (fetcher, request times, clock)."""
    clock = FakeClock()
    times = []
    answers = list(answers)

    def dispatch(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        times.append(clock.now)
        if answers:
            a = answers.pop(0)
            return a if isinstance(a, httpx.Response) else httpx.Response(a)
        return httpx.Response(200, text="<html>ok</html>")

    f = Fetcher(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(dispatch),
                sleep=clock.sleep, clock=clock.time, today=lambda: date(2026, 10, 16),
                jitter=lambda: 0.0, min_interval=min_interval, **kw)
    return f, times, clock


def test_429_slows_the_host_down_cools_down_and_probes_once(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="hkestate.fetch")
    f, times, clock = paced_fetcher(tmp_path, [429])
    assert f.get("https://example.com/a", "listing") == "<html>ok</html>"
    assert f.interval("example.com") == pytest.approx(3.0)  # 2 s x 1.5
    assert _gaps(times) == [15 * 60]  # the first cool-down, then one probe that worked
    msgs = [r.getMessage() for r in caplog.records]
    assert "example.com: slowing to 3.0 s after HTTP 429" in msgs
    assert any("cooling down 15 min" in m for m in msgs)
    f.get("https://example.com/b", "listing")
    assert _gaps(times)[-1] == pytest.approx(3.0)  # the next request uses the new gap


def test_retry_after_longer_than_the_cool_down_is_honoured(tmp_path):
    f, times, _ = paced_fetcher(
        tmp_path, [httpx.Response(429, headers={"retry-after": "3600"})])
    f.get("https://example.com/a", "listing")
    assert _gaps(times) == [3600]


def test_repeated_429s_grow_the_cool_down_to_two_hours_and_cap_the_gap(tmp_path):
    f, times, clock = paced_fetcher(tmp_path, [429] * 6)
    with pytest.raises(FetchError) as e1:
        f.get("https://example.com/a", "listing")  # 429, cool-down 15 min, probe 429
    assert e1.value.status == 429
    assert f.cooldown_remaining("https://example.com/a") == 30 * 60  # next one pending
    with pytest.raises(FetchError):
        f.get("https://example.com/a", "listing")
    with pytest.raises(FetchError):
        f.get("https://example.com/a", "listing")
    assert _gaps(times)[:5] == [15 * 60, 30 * 60, 60 * 60, 120 * 60, 120 * 60]
    assert f.interval("example.com") == 15.0  # 2 x 1.5^6 > 15: capped
    assert f.get("https://example.com/a", "listing") == "<html>ok</html>"  # after 2 h


def test_no_probe_of_its_own_with_throttle_probes_0(tmp_path):
    f, times, _ = paced_fetcher(tmp_path, [429], throttle_probes=0, cooldowns_s=(600,))
    with pytest.raises(FetchError):
        f.get("https://example.com/a", "listing")
    assert len(times) == 1 and f.cooldown_remaining("example.com") == 600
    f.get("https://example.com/a", "listing")  # the caller's retry waits it out
    assert _gaps(times) == [600]


def test_5xx_after_retries_slows_down_without_a_cool_down(tmp_path):
    f, times, _ = paced_fetcher(tmp_path, [500] * 4)
    with pytest.raises(FetchError):
        f.get("https://example.com/a", "listing")
    assert f.interval("example.com") == pytest.approx(3.0)
    assert f.cooldown_remaining("example.com") == 0


def test_three_timeouts_in_a_row_slow_the_host_down_one_does_not(tmp_path):
    clock = FakeClock()

    def dispatch(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/ok":
            return httpx.Response(200, text="fine")
        raise httpx.ReadTimeout("slow")

    f = Fetcher(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(dispatch),
                sleep=clock.sleep, clock=clock.time, jitter=lambda: 0.0, min_interval=2.0)
    for i in range(2):
        with pytest.raises(httpx.ReadTimeout):
            f.get(f"https://example.com/t{i}", "listing")
    assert f.interval("example.com") == pytest.approx(2.0)  # 2 in a row: still the base
    f.get("https://example.com/ok", "listing")  # an answer resets the run
    for i in range(2, 4):
        with pytest.raises(httpx.ReadTimeout):
            f.get(f"https://example.com/t{i}", "listing")
    assert f.interval("example.com") == pytest.approx(2.0)
    with pytest.raises(httpx.ReadTimeout):
        f.get("https://example.com/t4", "listing")  # third in a row
    assert f.interval("example.com") == pytest.approx(3.0)


def test_speeds_back_up_after_a_success_streak_but_never_below_the_base(
        tmp_path, monkeypatch, caplog):
    import logging

    from hkestate import fetch
    caplog.set_level(logging.INFO, logger="hkestate.fetch")
    monkeypatch.setattr(fetch, "SPEEDUP_AFTER", 3)
    f, times, _ = paced_fetcher(tmp_path, [429])
    f.get("https://example.com/p0", "listing")  # 429 then ok: 3.0 s (streak 1)
    assert f.interval("example.com") == pytest.approx(3.0)
    for i in range(1, 3):
        f.get(f"https://example.com/p{i}", "listing")
    assert f.interval("example.com") == pytest.approx(2.25)  # 3 good answers: -25%
    assert any(r.getMessage() == "example.com: speeding up to 2.2 s" for r in caplog.records)
    for i in range(3, 40):
        f.get(f"https://example.com/p{i}", "listing")
    assert f.interval("example.com") == 2.0  # back at the base, not below
    assert min(_gaps(times)[1:]) >= 2.0 - 1e-9


def test_pacing_is_per_host(tmp_path):
    f, times, _ = paced_fetcher(tmp_path, [429])
    f.get("https://example.com/a", "listing")
    assert f.interval("example.com") == pytest.approx(3.0)
    assert f.interval("other.example") == 2.0
    assert f.cooldown_remaining("other.example") == 0


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


def test_forget_deletes_the_cached_copy(tmp_path):
    fetcher, requests, _ = make_fetcher(tmp_path, ok)
    fetcher.get("https://example.com/big", "transaction")
    assert list((tmp_path / "cache").rglob("*.html.gz"))
    assert fetcher.forget("https://example.com/big") == 1
    assert not list((tmp_path / "cache").rglob("*.html.gz"))
    assert fetcher.forget("https://example.com/big") == 0
    fetcher.get("https://example.com/big", "transaction")  # nothing cached: fetched again
    assert len(page_requests(requests)) == 2


# ---- redirects: followed by hand, each hop checked against robots.txt and paced -------------


def redirect(location, status=302):
    return httpx.Response(status, headers={"location": location})


def test_same_host_redirect_is_followed_and_cached_under_original_url(tmp_path):
    def handler(request):
        if request.url.path == "/old":
            return redirect("/new")
        return ok(request)

    fetcher, requests, clock = make_fetcher(tmp_path, handler)
    assert fetcher.get("https://example.com/old", "static") == "<html>ok</html>"
    assert [r.url.path for r in page_requests(requests)] == ["/old", "/new"]
    # the second hop waited for the host's 1 s delay
    assert clock.sleeps and clock.sleeps[-1] >= 1.0
    # cached under the original URL: no new request
    n = len(requests)
    fetcher.get("https://example.com/old", "static")
    assert len(requests) == n


def test_redirect_to_disallowed_path_is_not_requested(tmp_path):
    fetcher, requests, _ = make_fetcher(tmp_path, lambda r: redirect("/private/x"))
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://example.com/old", "static")
    assert [r.url.path for r in page_requests(requests)] == ["/old"]


def test_cross_host_redirect_checks_other_hosts_robots_txt(tmp_path):
    requests = []

    def dispatch(request):
        requests.append(request)
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            body = "User-agent: *\nDisallow: /secret/\n" if host == "other.com" else ROBOTS
            return httpx.Response(200, text=body)
        if host == "example.com":
            return redirect("https://other.com/secret/a" if path == "/a" else "https://other.com/b")
        return httpx.Response(200, text="<html>other</html>")

    clock = FakeClock()
    fetcher = Fetcher(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(dispatch),
                      sleep=clock.sleep, clock=clock.time, today=lambda: date(2026, 10, 16),
                      jitter=lambda: 0.0)
    assert fetcher.get("https://example.com/b", "static") == "<html>other</html>"
    assert any(r.url.host == "other.com" and r.url.path == "/robots.txt" for r in requests)
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://example.com/a", "static")
    assert not any(r.url.path == "/secret/a" for r in requests)


def test_redirect_loop_gives_up_after_five_hops(tmp_path):
    fetcher, requests, _ = make_fetcher(tmp_path, lambda r: redirect("/loop"))
    with pytest.raises(FetchError, match="redirect"):
        fetcher.get("https://example.com/loop", "static")
    assert len(page_requests(requests)) == 6  # the original plus 5 hops


def test_five_hops_is_allowed(tmp_path):
    def handler(request):
        n = int(request.url.path.strip("/") or 0)
        return redirect(f"/{n + 1}") if n < 5 else ok(request)

    fetcher, _, _ = make_fetcher(tmp_path, handler)
    assert fetcher.get("https://example.com/0", "static") == "<html>ok</html>"


# ---- JSON POST -----------------------------------------------------------------------------


def test_post_json_checks_robots_and_sends_user_agent_and_body(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text='{"ok": 1}')

    fetcher, requests, _ = make_fetcher(tmp_path, handler)
    url = "https://example.com/api/s"
    assert fetcher.post_json(url, {"codes": ["a"]}, "transaction") == '{"ok": 1}'
    assert seen[0].method == "POST"
    assert seen[0].headers["user-agent"] == USER_AGENT
    assert seen[0].headers["content-type"] == "application/json"
    assert json.loads(seen[0].content) == {"codes": ["a"]}
    with pytest.raises(RobotsDisallowed):
        fetcher.post_json("https://example.com/private/s", {"codes": ["a"]}, "transaction")
    assert len(page_requests(requests)) == 1


def test_post_json_is_paced_like_a_get(tmp_path):
    times = []
    clock = FakeClock()

    def handler(request):
        times.append(clock.now)
        return ok(request)

    fetcher, _, _ = make_fetcher(tmp_path, handler, clock=clock)
    fetcher.get("https://example.com/a", "listing")
    fetcher.post_json("https://example.com/b", {"n": 1}, "listing")
    fetcher.post_json("https://example.com/b", {"n": 2}, "listing")
    assert all(b - a >= 1.0 - 1e-9 for a, b in zip(times, times[1:], strict=False))


@pytest.mark.parametrize("status", [500, 502])
def test_post_json_retries_and_resends_the_body(tmp_path, status):
    bodies = []

    def handler(request):
        bodies.append(request.content)
        if len(bodies) < 3:
            return httpx.Response(status, text="slow down")
        return httpx.Response(200, text="done")

    fetcher, _, clock = make_fetcher(tmp_path, handler)
    assert fetcher.post_json("https://example.com/s", {"n": 1}, "listing") == "done"
    assert len(bodies) == 3 and len(set(bodies)) == 1
    assert [s for s in clock.sleeps if s >= 2.0][:2] == [2.0, 4.0]


@pytest.mark.parametrize("status", [429, 503])
def test_post_json_probe_after_a_cool_down_resends_the_body(tmp_path, status):
    bodies = []

    def handler(request):
        bodies.append(request.content)
        if len(bodies) < 2:
            return httpx.Response(status, text="slow down")
        return httpx.Response(200, text="done")

    fetcher, _, clock = make_fetcher(tmp_path, handler)
    assert fetcher.post_json("https://example.com/s", {"n": 1}, "listing") == "done"
    assert len(bodies) == 2 and len(set(bodies)) == 1
    assert 15 * 60 in [round(s) for s in clock.sleeps]


def test_post_json_cache_key_is_url_plus_body(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=f"answer {len(calls)}")

    fetcher, _, _ = make_fetcher(tmp_path, handler)
    url = "https://example.com/s"
    assert fetcher.post_json(url, {"a": 1, "b": 2}, "transaction") == "answer 1"
    # same body, keys in another order: cached
    assert fetcher.post_json(url, {"b": 2, "a": 1}, "transaction") == "answer 1"
    # different body: a new request
    assert fetcher.post_json(url, {"a": 1, "b": 3}, "transaction") == "answer 2"
    # a GET of the same URL is a different key
    assert fetcher.get(url, "transaction") == "answer 3"
    sha = hashlib.sha1(Fetcher.post_key(url, {"a": 1, "b": 2}).encode()).hexdigest()
    assert (tmp_path / "cache" / "example.com" / "2026-10-16" / f"{sha}.html.gz").exists()


def test_post_json_follows_redirect_keeping_body_and_checking_robots(tmp_path):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content))
        if request.url.path == "/old":
            return httpx.Response(307, headers={"location": "/new"})
        return httpx.Response(200, text="moved")

    fetcher, _, _ = make_fetcher(tmp_path, handler)
    assert fetcher.post_json("https://example.com/old", {"n": 1}, "listing") == "moved"
    assert seen[1][:2] == ("POST", "/new") and seen[1][2] == seen[0][2]
    seen.clear()

    def to_private(request):
        return httpx.Response(307, headers={"location": "/private/x"})

    fetcher2, requests2, _ = make_fetcher(tmp_path / "b", to_private)
    with pytest.raises(RobotsDisallowed):
        fetcher2.post_json("https://example.com/old", {"n": 1}, "listing")
    assert len(page_requests(requests2)) == 1
