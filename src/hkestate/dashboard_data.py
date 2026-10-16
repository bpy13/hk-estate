"""Data for the Streamlit dashboard (app/dashboard.py), kept out of the app so it can be tested.

The dashboard reads the files `hkestate train` and `hkestate rank` write to `data/model/`,
so it never holds the database while jobs write to it. Only the run-status banner looks at
the database, read-only and briefly; if the file is busy it falls back to the run log saved
in the last snapshot.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import duckdb
import pandas as pd

from hkestate import db, model

FLAG_LABELS = {
    "haunted": "haunted", "tenanted": "tenanted", "sea_view": "sea view",
    "renovated": "renovated", "illegal_structure": "illegal structure", "studio": "studio",
    "with_parking": "includes parking",
}


def ranked() -> pd.DataFrame:
    path = model.model_dir() / "ranked_latest.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["key"] = df["source"] + ":" + df["listing_id"]
    df["flag_list"] = df["flags"].fillna("").map(lambda s: [f for f in s.split("|") if f])
    return df


def deals() -> pd.DataFrame:
    path = model.model_dir() / "deals.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def meta() -> dict:
    path = model.model_dir() / "fair_price.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def eval_report() -> str | None:
    path = model.report_path()
    return path.read_text("utf-8") if path.exists() else None


def filter_ranked(df: pd.DataFrame, *, districts=None, price_m=None, size=None,
                  need_flags=None, hide_flags=None, confidence=None) -> pd.DataFrame:
    """The ranked list narrowed by the dashboard's filters (None = no filter)."""
    out = df
    if districts:
        out = out[out["district"].isin(districts)]
    if price_m:
        out = out[out["asking_hkd"].between(price_m[0] * 1e6, price_m[1] * 1e6)]
    if size:
        out = out[out["area_sqft"].between(size[0], size[1])]
    if need_flags:
        out = out[out["flag_list"].map(lambda fl: any(f in fl for f in need_flags))]
    if hide_flags:
        out = out[out["flag_list"].map(lambda fl: not any(f in fl for f in hide_flags))]
    if confidence:
        out = out[out["confidence"].isin(confidence)]
    return out


def comparables(all_deals: pd.DataFrame, building_id: str, ranked_on, months: int = 24):
    """Clean deals in the same building in the `months` before the ranking date."""
    if all_deals.empty:
        return all_deals
    since = pd.Timestamp(ranked_on) - pd.DateOffset(months=months)
    d = all_deals[(all_deals["building_id"] == building_id)
                  & all_deals["exclude_reason"].isna()
                  & (all_deals["agreement_date"] > since)]
    return d.sort_values("agreement_date", ascending=False)


def unit_history(all_deals: pd.DataFrame, row) -> tuple[pd.DataFrame, str]:
    """Recorded deals on the listing's flat: the exact unit when the floor is known, else
    the same flat letter in the same floor band, else every floor of that flat letter.
    Returns (deals, what was matched)."""
    if all_deals.empty or not row.get("flat"):
        return all_deals.iloc[0:0], "no flat letter on the listing"
    d = all_deals[(all_deals["building_id"] == row["building_id"])
                  & (all_deals["flat"] == row["flat"])]
    if row.get("floor_basis") == "exact" and pd.notna(row.get("floor_num")):
        d = d[d["floor_num"] == row["floor_num"]]
        what = "this flat"
    elif row.get("floor_basis") == "band" and pd.notna(row.get("rel_floor")):
        lo = {1 / 6: 0, 0.5: 1 / 3, 5 / 6: 2 / 3}.get(row["rel_floor"], None)
        if lo is not None:
            d = d[(d["rel_floor"] >= lo) & (d["rel_floor"] < lo + 1 / 3 + 1e-9)]
        what = f"flat {row['flat']} in the same floor band"
    else:
        what = f"flat {row['flat']} on any floor"
    return d.sort_values("agreement_date"), what


@dataclass
class RunStatus:
    failed_last: dict | None  # the newest run_log entry, if it failed
    failing: pd.DataFrame  # commands whose newest run failed
    source: str  # "database" or "snapshot (database busy)"


def _status_from(log: pd.DataFrame, source: str) -> RunStatus:
    if log.empty:
        return RunStatus(None, log, source)
    log = log.sort_values("run_id", ascending=False)
    last = log.iloc[0]
    newest = log.drop_duplicates("command")
    failing = newest[newest["status"] == "failed"]
    return RunStatus(last.to_dict() if last["status"] == "failed" else None, failing, source)


def run_status(db_path=None, tries: int = 3) -> RunStatus:
    path = db_path or db.default_db_path()
    for _ in range(tries):
        try:
            con = duckdb.connect(str(path), read_only=True)
        except duckdb.Error:
            time.sleep(0.3)
            continue
        try:
            log = con.execute(
                "SELECT run_id, command, args, started_at, finished_at, status, message "
                "FROM run_log ORDER BY run_id DESC LIMIT 200").df()
        finally:
            con.close()
        return _status_from(log, "database")
    snap = model.model_dir() / "snapshot" / "run_log.parquet"
    log = pd.read_parquet(snap) if snap.exists() else pd.DataFrame()
    return _status_from(log, "snapshot (database busy)")
