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
from pathlib import Path

import duckdb
import pandas as pd

from hkestate import db, model
from hkestate.config import get_data_dir

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


# ---- growth study (data/reports/growth/<date>/, written by `hkestate report`) ----------------

DISTRICT_EN = {
    "中西區": "Central and Western", "灣仔區": "Wan Chai", "東區": "Eastern",
    "南區": "Southern", "油尖旺區": "Yau Tsim Mong", "深水埗區": "Sham Shui Po",
    "九龍城區": "Kowloon City", "黃大仙區": "Wong Tai Sin", "觀塘區": "Kwun Tong",
    "荃灣區": "Tsuen Wan", "屯門區": "Tuen Mun", "元朗區": "Yuen Long", "北區": "North",
    "大埔區": "Tai Po", "沙田區": "Sha Tin", "西貢區": "Sai Kung", "葵青區": "Kwai Tsing",
    "離島區": "Islands", "unknown": "Unknown",
}
PAIRS_TYPES = {"recorded": "Recorded sales (real pairs)",
               "estimated": "Estimated (model-priced) pairs"}
VERDICT_SHORT = {"robust": "robust", "significant, not robust": "significant only",
                 "not significant": "no effect found"}


def district_label(name: str) -> str:
    """'東區' -> 'Eastern 東區' (English first, Chinese kept alongside)."""
    en = DISTRICT_EN.get(name)
    if en is None:
        return name
    return en if name == "unknown" else f"{en} {name}"


@dataclass
class GrowthSetting:
    name: str
    label: str
    enough: bool  # a regression could be run on recorded pairs
    note: str | None  # why not, when not enough
    sensitivity: bool  # a labelled side-check (e.g. 1-year minimum hold), not a main setting
    min_hold_years: float | None
    start: str | None
    end: str | None
    pairs: dict
    effects: pd.DataFrame
    districts: pd.DataFrame
    index: pd.DataFrame
    returns: pd.DataFrame

    @property
    def has_verdicts(self) -> bool:
        return self.enough and not self.effects.empty


@dataclass
class GrowthStudy:
    folder: Path
    date: str
    settings: list[GrowthSetting]

    @property
    def main(self) -> list[GrowthSetting]:
        return [s for s in self.settings if not s.sensitivity]

    @property
    def sensitivities(self) -> list[GrowthSetting]:
        return [s for s in self.settings if s.sensitivity]

    def get(self, name: str) -> GrowthSetting | None:
        return next((s for s in self.settings if s.name == name), None)


def latest_growth_dir(root: Path | None = None) -> Path | None:
    """Newest dated folder (YYYYMMDD) under data/reports/growth, or None."""
    root = root or get_data_dir() / "reports" / "growth"
    if not root.is_dir():
        return None
    dated = sorted(p for p in root.iterdir() if p.is_dir() and p.name.isdigit())
    return dated[-1] if dated else None


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, encoding="utf-8") if path.exists() else pd.DataFrame()
    except (pd.errors.EmptyDataError, OSError, UnicodeDecodeError):
        return pd.DataFrame()


def load_growth(folder: Path | None = None) -> GrowthStudy | None:
    """Everything the growth page shows, tolerant of a changing folder: any number of
    settings, missing CSVs or settings.json, settings with too few pairs (empty effects),
    and sensitivity settings (`"sensitivity": true`, `"min_hold_years"`)."""
    folder = folder or latest_growth_dir()
    if folder is None or not folder.is_dir():
        return None
    meta_path = folder / "settings.json"
    try:
        meta = json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else {}
    except (ValueError, OSError):
        meta = {}
    info = meta.get("settings") or {}
    on_disk = sorted(p.name for p in folder.iterdir() if p.is_dir())
    names = [n for n in meta.get("order", list(info)) if n in info or n in on_disk]
    names += [n for n in list(info) + on_disk if n not in names]
    names = list(dict.fromkeys(names))
    settings = []
    for n in names:
        i, d = info.get(n) or {}, folder / n
        effects = _read_csv(d / "effects.csv")
        settings.append(GrowthSetting(
            name=n, label=i.get("label") or n,
            enough=bool(i.get("enough", not effects.empty)), note=i.get("note"),
            sensitivity=bool(i.get("sensitivity", False)),
            min_hold_years=i.get("min_hold_years"), start=i.get("start"), end=i.get("end"),
            pairs=i.get("pairs") or {}, effects=effects,
            districts=_read_csv(d / "districts.csv"), index=_read_csv(d / "index.csv"),
            returns=_read_csv(d / "returns.csv")))
    return GrowthStudy(folder, str(meta.get("date") or folder.name), settings)


def _pairs_rows(df: pd.DataFrame, pairs_type: str) -> pd.DataFrame:
    if df.empty or "pairs_type" not in df:
        return df
    return df[df["pairs_type"] == pairs_type]


def verdict_cell(row: pd.Series | None) -> str:
    if row is None:
        return "–"
    return f"{VERDICT_SHORT.get(row['verdict'], row['verdict'])} ({row['coef'] * 100:+.2f})"


def verdict_table(settings: list[GrowthSetting], pairs_type: str = "recorded") -> pd.DataFrame:
    """Attribute × setting, one text cell each: 'robust (+2.20)' = verdict and the effect in
    percentage points of annual growth. A setting with no verdicts shows 'too few pairs' (or
    '–' if the file is missing). Pass the main settings only; sensitivities go in their own
    call so they never mix into this table."""
    labels: dict[str, str] = {}
    cols: dict[str, dict[str, str]] = {}
    for s in settings:
        e = _pairs_rows(s.effects, pairs_type)
        cell: dict[str, str] = {}
        for _, r in e.iterrows():
            labels.setdefault(r["attribute"], r["label"])
            cell[r["attribute"]] = verdict_cell(r)
        cols[s.name] = cell
    t = pd.DataFrame(index=list(labels))
    for s in settings:
        fill = "too few pairs" if not s.has_verdicts else "–"
        t[s.name] = [cols[s.name].get(a, fill) for a in t.index]
    t.insert(0, "Attribute", [labels[a] for a in t.index])
    return t


def effect_rows(settings: list[GrowthSetting], pairs_type: str = "recorded") -> pd.DataFrame:
    """Long table for the effect chart: attribute, setting, effect (pp a year), 95% interval."""
    parts = []
    for s in settings:
        e = _pairs_rows(s.effects, pairs_type)
        if e.empty:
            continue
        parts.append(pd.DataFrame({
            "attribute": e["attribute"], "label": e["label"], "setting": s.name,
            "effect": e["coef"] * 100, "low": (e["coef"] - 1.96 * e["se"]) * 100,
            "high": (e["coef"] + 1.96 * e["se"]) * 100, "verdict": e["verdict"],
            "n": e["n"]}))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=["attribute", "label", "setting", "effect", "low", "high", "verdict", "n"])


def attribute_sentence(attribute: str, settings: list[GrowthSetting],
                       pairs_type: str = "recorded") -> str:
    """One plain-language line on whether an attribute holds across the main settings."""
    robust, signif, none, signs = [], [], [], set()
    for s in settings:
        e = _pairs_rows(s.effects, pairs_type)
        r = e[e["attribute"] == attribute] if not e.empty else e
        if r.empty:
            continue
        r = r.iloc[0]
        {"robust": robust, "significant, not robust": signif}.get(r["verdict"], none).append(
            s.name)
        if r["verdict"] != "not significant":
            signs.add(r["coef"] > 0)
    tested = len(robust) + len(signif) + len(none)
    if tested == 0:
        return "No result for this attribute."
    if not robust and not signif:
        return f"No clear effect in any of the {tested} settings."
    direction = ("raises" if signs == {True} else "lowers" if signs == {False}
                 else "pushes growth both ways, depending on the period")
    verb = f"{direction} growth" if signs in ({True}, {False}) else direction
    if len(robust) == tested:
        return f"Holds in every setting ({tested}): {verb}."
    if robust:
        return (f"Robust in {len(robust)} of {tested} settings ({', '.join(robust)}); "
                f"it {verb}.")
    return (f"Shows up in {len(signif)} of {tested} settings but never robustly: treat "
            "it as a hint, not a finding.")


def district_table(s: GrowthSetting, pairs_type: str = "recorded") -> pd.DataFrame:
    """District rows with English + Chinese names and percentages as numbers."""
    d = _pairs_rows(s.districts, pairs_type)
    if d.empty:
        return d
    d = d.copy()
    d.insert(0, "District", d["district"].map(district_label))
    return d.sort_values("total_return", ascending=False).reset_index(drop=True)


def district_index(s: GrowthSetting, pairs_type: str = "recorded") -> pd.DataFrame:
    """Monthly price index per district (first month = 100), long form with 'District'."""
    i = _pairs_rows(s.index, pairs_type)
    if i.empty:
        return i
    i = i[i["group"].str.startswith("district:")].copy()
    i["District"] = i["group"].str[len("district:"):].map(district_label)
    i["month"] = pd.to_datetime(i["month"])
    return i


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
