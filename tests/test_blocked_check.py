"""Monthly check of the sites we don't use because their robots.txt refuses us (mocked network)."""

import httpx

from hkestate import blocked_check, db
from hkestate.fetch import USER_AGENT, Fetcher


def _fetcher(tmp_path, answers):
    """`answers` maps host -> robots.txt status; every request is recorded."""
    requests = []

    def dispatch(request):
        requests.append(request)
        return httpx.Response(answers[request.url.host], text="User-agent: *\nDisallow:\n")

    f = Fetcher(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(dispatch),
                sleep=lambda s: None, jitter=lambda: 0.0)
    return f, requests


def _all(status, **over):
    answers = {s.host: status for s in blocked_check.SITES}
    answers.update(over)
    return answers


def test_sites_are_the_four_blocked_ones():
    assert {s.host for s in blocked_check.SITES} == {
        "www.midland.com.hk", "www.squarefoot.com.hk", "www.house730.com", "www.hkp.com.hk"}


def test_each_site_checked_once_and_logged_to_run_log(tmp_path):
    f, requests = _fetcher(tmp_path, _all(403))
    lines = []
    db_path = tmp_path / "t.duckdb"
    results = blocked_check.run_check(f, db_path, lines.append)
    assert [r.status for r in results] == [403] * 4
    assert sorted(r.url.host for r in requests) == sorted(s.host for s in blocked_check.SITES)
    assert all(r.url.path == "/robots.txt" for r in requests)
    assert all(r.headers["user-agent"] == USER_AGENT for r in requests)
    assert not any("now reachable" in line for line in lines)
    with db.session(db_path) as con:
        rows = con.execute(
            "SELECT args, status, message FROM run_log WHERE command = 'check-blocked' "
            "ORDER BY args").fetchall()
    assert len(rows) == 4
    assert all(r[1] == "ok" and "403" in r[2] for r in rows)


def test_a_site_answering_200_is_flagged(tmp_path):
    f, _ = _fetcher(tmp_path, _all(403, **{"www.house730.com": 200}))
    lines = []
    blocked_check.run_check(f, tmp_path / "t.duckdb", lines.append)
    flagged = [line for line in lines if "now reachable, review docs/sources.md" in line]
    assert len(flagged) == 1 and "house730" in flagged[0]



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
