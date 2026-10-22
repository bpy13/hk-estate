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

from hkestate import db, features, market, model
from hkestate.config import get_data_dir
from hkestate.report import plain_words  # the study's labels and notes still say "pairs"
from hkestate.study import district_label  # noqa: F401  (one label, shared with the report)

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
                  need_flags=None, hide_flags=None, confidence=None,
                  show_excluded=False) -> pd.DataFrame:
    """The ranked list narrowed by the dashboard's filters (None = no filter). Rows with an
    `exclude_reason` (a data check failed) are hidden unless `show_excluded`."""
    out = df
    if not show_excluded and "exclude_reason" in out:
        out = out[out["exclude_reason"].isna()]
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
# District names are shown as the report shows them: `study.district_label`, 'Eastern (東區)'.

PAIRS_TYPES = {"recorded": "Flats sold twice (two recorded sales)",
               "estimated": "Model-valued flats (cross-check)"}
VERDICT_SHORT = {"robust": "robust", "significant, not robust": "significant only",
                 "not significant": "no effect found"}


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
            name=n, label=plain_words(i.get("label")) or n,
            enough=bool(i.get("enough", not effects.empty)), note=plain_words(i.get("note")),
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
    percentage points of annual growth. A setting with no verdicts shows 'too few flats sold
    twice' (or
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
        fill = "too few flats sold twice" if not s.has_verdicts else "–"
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


# ---- Market today (plan Task 22; figures from market.summary on the snapshot) ----------------

SIZE_LABELS = {  # the government's size classes, as flat sizes anyone can picture
    "ALL": "All sizes", "A": "Under 430 sq ft", "B": "430-750 sq ft", "C": "750-1,075 sq ft",
    "D": "1,075-1,720 sq ft", "E": "Over 1,720 sq ft"}
ALL_HK = "All of Hong Kong"


@dataclass
class MarketData:
    summary: pd.DataFrame
    trends: dict  # district name (market.TERRITORY for all) -> market.trend frame
    data_to: pd.Timestamp
    three_month_ends: pd.Timestamp


def build_market(raw: dict) -> MarketData:
    """The summary and a 36-month trend per district, from a loaded snapshot. The deal table
    is built once and shared, so this is a few seconds, not one pass per district."""
    deals_ft = features.deal_features(raw)
    s = market.summary(raw, deals=deals_ft)
    trends = {market.TERRITORY: market.trend(raw, None, deals=deals_ft)}
    for dist in sorted(set(s["district"]) - {market.TERRITORY}):
        trends[dist] = market.trend(raw, dist, deals=deals_ft)
    data_to, ends = s.attrs["data_to"], s.attrs["three_month_change_ends"]
    s.attrs = {}  # Timestamps in attrs trip Streamlit's table serialiser
    return MarketData(s, trends, data_to, ends)


def market_data() -> MarketData | None:
    """build_market on data/model/snapshot, or None when there is no snapshot yet."""
    folder = model.model_dir() / "snapshot"
    if not (folder / "meta.json").exists():
        return None
    return build_market(features.load_snapshot(folder))


def snapshot_stamp() -> float:
    """The snapshot's meta.json modified time: the cache key, so the summary is rebuilt only
    when a new snapshot is written."""
    path = model.model_dir() / "snapshot" / "meta.json"
    return path.stat().st_mtime if path.exists() else 0.0


def market_district_label(name: str) -> str:
    return ALL_HK if name == market.TERRITORY else district_label(name)


def pct_text(x, digits: int = 1) -> str:
    """'+3.2%' for a fraction, '-' when unknown."""
    return "–" if x is None or pd.isna(x) else f"{x * 100:+.{digits}f}%"


def market_cell(summary: pd.DataFrame, district: str, size: str) -> pd.Series | None:
    r = summary[(summary["district"] == district) & (summary["size_class"] == size)]
    return r.iloc[0] if len(r) else None


def market_table(summary: pd.DataFrame, size: str) -> pd.DataFrame:
    """The district table for one size: plain column names, numbers kept numeric so it sorts.
    `Note` says 'few sales' when the change from our sales rests on fewer than
    market.THIN_DEALS sales in either window."""
    t = summary[summary["size_class"] == size].copy()
    t["District"] = t["district"].map(market_district_label)
    thin = (t["n_own_12m"] < market.THIN_DEALS) | (t["n_own_3m"] < market.THIN_DEALS)
    t["Note"] = thin.map({True: "few sales", False: ""})
    t = t.rename(columns={
        "median_psf_12m": "Price per sq ft (HK$)", "n_sales_12m": "Sales in 12 months",
        "own_chg_12m": "12 months (our sales)", "own_chg_3m": "3 months (our sales)",
        "idx_chg_12m": "12 months (gov. index)", "idx_chg_3m": "3 months (gov. index)",
        "ask_premium": "Asking vs deals"})
    for c in ("12 months (our sales)", "3 months (our sales)", "12 months (gov. index)",
              "3 months (gov. index)", "Asking vs deals"):
        t[c] = t[c] * 100  # shown with a % sign by the page
    t["_terr"] = t["district"] != market.TERRITORY  # all of Hong Kong first
    t = t.sort_values(["_terr", "District"])
    return t[["District", "Price per sq ft (HK$)", "Sales in 12 months", "12 months (our sales)",
              "3 months (our sales)", "12 months (gov. index)", "3 months (gov. index)",
              "Asking vs deals", "Note"]].reset_index(drop=True)


def market_header(data_to, three_month_ends) -> str:
    d = pd.Timestamp(data_to)
    weeks = max(1, round((d - pd.Timestamp(three_month_ends)).days / 7))
    return (f"Sales up to {d.day} {d.strftime('%b %Y')}; the last {weeks} weeks are "
            "incomplete (deals reach the register late).")


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
