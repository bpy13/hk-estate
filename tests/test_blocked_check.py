"""Monthly check of the sites we don't use because their robots.txt refuses us (mocked network)."""

import httpx

from hkestate import blocked_check, db
from hkestate.fetch import USER_AGENT, Fetcher

CHALLENGE = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"


def _fetcher(tmp_path, answers, bodies=None):
    """`answers` maps host -> robots.txt status; `bodies` host -> body text; requests recorded."""
    requests = []
    bodies = bodies or {}

    def dispatch(request):
        requests.append(request)
        return httpx.Response(answers[request.url.host],
                              text=bodies.get(request.url.host, "User-agent: *\nDisallow:\n"))

    f = Fetcher(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(dispatch),
                sleep=lambda s: None, jitter=lambda: 0.0)
    return f, requests


def _all(status, **over):
    answers = {s.host: status for s in blocked_check.SITES}
    answers.update(over)
    return answers


def test_sites_are_the_five_blocked_ones():
    assert {s.host for s in blocked_check.SITES} == {
        "www.midland.com.hk", "www.squarefoot.com.hk", "www.house730.com", "www.hkp.com.hk",
        "www.spacious.hk"}


def test_each_site_checked_once_and_logged_to_run_log(tmp_path):
    f, requests = _fetcher(tmp_path, _all(403))
    lines = []
    db_path = tmp_path / "t.duckdb"
    results = blocked_check.run_check(f, db_path, lines.append)
    assert [r.status for r in results] == [403] * 5
    assert sorted(r.url.host for r in requests) == sorted(s.host for s in blocked_check.SITES)
    assert all(r.url.path == "/robots.txt" for r in requests)
    assert all(r.headers["user-agent"] == USER_AGENT for r in requests)
    assert not any("now reachable" in line for line in lines)
    with db.session(db_path) as con:
        rows = con.execute(
            "SELECT args, status, message FROM run_log WHERE command = 'check-blocked' "
            "ORDER BY args").fetchall()
    assert len(rows) == 5
    assert all(r[1] == "ok" and "403" in r[2] for r in rows)


def test_a_site_answering_200_is_flagged(tmp_path):
    f, _ = _fetcher(tmp_path, _all(403, **{"www.house730.com": 200}))
    lines = []
    blocked_check.run_check(f, tmp_path / "t.duckdb", lines.append)
    flagged = [line for line in lines if "now reachable, review docs/sources.md" in line]
    assert len(flagged) == 1 and "house730" in flagged[0]


def test_spacious_cloudflare_challenge_stays_blocked_whatever_the_status(tmp_path):
    """A challenge may answer 403, 503 or 200 with HTML: only a real robots file counts as open."""
    host = "www.spacious.hk"
    for status in (403, 503, 200):
        f, _ = _fetcher(tmp_path / str(status), _all(403, **{host: status}),
                        bodies={host: CHALLENGE})
        lines = []
        blocked_check.run_check(f, tmp_path / f"{status}.duckdb", lines.append)
        assert not any("now reachable" in line for line in lines), status
        assert any(line.startswith("spacious") and "still blocked" in line for line in lines)


def test_spacious_is_open_only_when_robots_txt_is_a_real_robots_file(tmp_path):
    f, _ = _fetcher(tmp_path, _all(403, **{"www.spacious.hk": 200}))  # default body is robots
    lines = []
    blocked_check.run_check(f, tmp_path / "t.duckdb", lines.append)
    flagged = [line for line in lines if "now reachable, review docs/sources.md" in line]
    assert len(flagged) == 1 and "spacious" in flagged[0]



def test_cli_command_is_registered_and_uses_the_fetcher(tmp_path, monkeypatch):
    from hkestate import cli

    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    seen = []

    def fake_run_check(fetcher, db_path, log):
        seen.append(db_path)
        return []

    monkeypatch.setattr(blocked_check, "run_check", fake_run_check)
    assert cli.main(["--db", str(tmp_path / "h.duckdb"), "check-blocked"]) == 0
    assert seen == [str(tmp_path / "h.duckdb")]
