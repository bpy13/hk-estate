"""The `hkestate` command line on a small database."""

from datetime import date, datetime
from types import SimpleNamespace

from hkestate import cli, db


def test_status_runs_on_a_small_db(tmp_path, capsys):
    path = tmp_path / "t.duckdb"
    with db.session(path) as con:
        db.init_db(con)
        # estate has a `source` column too: the status query must not be ambiguous
        con.execute(
            "INSERT INTO estate (estate_id, name_zh, group_name, source, source_key) "
            "VALUES ('h28-1', '測試苑', 'test-group', 'hse28', '1')"
        )
        db.insert_txn_raw(
            con,
            [
                {
                    "source": "hse28",
                    "source_id": "u1:2024-01-01",
                    "fetched_at": datetime(2024, 1, 2),
                    "estate_id": "h28-1",
                    "building_id": "h28-1-b1",
                    "floor_label": "10",
                    "flat": "A",
                    "deal_date": date(2024, 1, 1),
                    "deal_type": "registry",
                    "price_hkd": 6_000_000,
                    "saleable_sqft": 500.0,
                }
            ],
        )
    assert cli.cmd_status(SimpleNamespace(db=str(path))) == 0
    out = capsys.readouterr().out
    assert "test-group 1 1" in out


def test_link_ids_runs_on_an_empty_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    path = tmp_path / "t.duckdb"
    assert cli.cmd_link_ids(SimpleNamespace(db=str(path))) == 0
    assert "linked:" in capsys.readouterr().out


def test_backfill_territory_without_any_estate_asks_for_a_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    args = SimpleNamespace(db=str(tmp_path / "t.duckdb"), discover=False, order="flats",
                           mapped_only=False, skip_flats=False, max_estates=None,
                           max_hours=None)
    assert cli.cmd_backfill_territory(args) == 1
    assert "no 28Hse estates known" in capsys.readouterr().out


def test_cd_history_is_a_command_and_refuses_a_second_copy(tmp_path, monkeypatch, capsys):
    import os

    from hkestate import locks

    monkeypatch.setenv("HKESTATE_DATA_DIR", str(tmp_path))
    path = locks.lock_path("cd-history")
    path.parent.mkdir(parents=True)
    # this test process is "the other job", seen from a child's point of view
    monkeypatch.setattr(locks, "held_by", lambda name: os.getpid())
    assert cli.main(["--db", str(tmp_path / "x.duckdb"), "cd-history", "--limit", "1"]) == 1
    assert "not started" in capsys.readouterr().out
