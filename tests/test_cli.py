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
