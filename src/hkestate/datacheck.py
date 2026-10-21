"""`hkestate datacheck`: is the full transaction data fit to analyse? (plan 2026-10-21, Task 4)

Reads the snapshot (`features.load_snapshot`, never the live database by default) and writes
`data/reports/datacheck_<date>.md`; `run` also returns every number as a dict. Four checks,
each with a plain verdict ("ok" or "look at this"):

1. Sales per year 1995-today against an independent count: the Land Registry's monthly
   number of residential sale and purchase agreements (data.gov.hk open data, yearly JSON
   files from 2005, fetched through the polite fetcher and cached 30 days). Years before
   2005 have no free independent count and are shown without a verdict. A year is flagged
   when our clean resales are under 50% or over 110% of the registry's count.
2. Duplicate sales still present after `excl_duplicate`: two stored rows on the same unit
   with prices within the duplicate tolerance and dates within the duplicate window, by
   year and source pair. Centadata history rows and latest-deal rows share one source
   label in the snapshot, so a centadata-centadata pair is one of those two kinds.
3. Odd values: deal dates in the future or before 1990, prices under HK$500k or over
   HK$500m, saleable area under 100 or over 5,000 sq ft, price per sq ft outside
   1,000-100,000, and units with two sales on the same day.
4. Coverage by decade: the share of sales with a building, a saleable area and a floor.

Nothing is changed or deleted: odd rows go to the report for the owner to decide.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from hkestate import db
from hkestate.config import get_data_dir
from hkestate.normalise import parse_floor

YEAR_FROM = 1995
RATIO_LOW, RATIO_HIGH = 0.5, 1.1  # clean resales / registry count outside this = flag

# Land Registry: "Number of Agreements for Sale and Purchase of residential building units
# received for registration (by range of consideration)", one JSON file per year from 2005
# (data.gov.hk dataset hk-lr-data5-landreg; docs/sources.md "Land Registry statistics").
LR_URL = "https://www.landreg.gov.hk/datagovhk/consideration_{year}.json"
LR_FIRST_YEAR = 2005
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Odd-value limits (plan Task 4, check 3); txn_all flags dates before MIN_DEAL_YEAR (Task 4b)
MIN_DEAL_YEAR = db.MIN_DEAL_DATE.year
PRICE_MIN, PRICE_MAX = db.MIN_PRICE_HKD, 500_000_000
AREA_MIN, AREA_MAX = 100.0, 5_000.0
PSF_MIN, PSF_MAX = 1_000.0, 100_000.0

# Verdict thresholds for checks 2 and 4 (Claude's call; the numbers are always printed)
DUP_SHARE_MAX = 0.005  # remaining duplicate pairs as a share of stored sales
COVERAGE_MIN = {"building": 0.99, "area": 0.80, "floor": 0.90}
COVERAGE_MIN_DEALS = 1_000  # a decade with fewer sales is shown but not judged

EXCL_FLAGS = ["excl_duplicate", "excl_low_price", "excl_first_hand", "excl_non_market",
              "excl_bulk", "excl_outlier", "excl_bad_date"]
EXAMPLES = 5


def reports_dir() -> Path:
    return get_data_dir() / "reports"


# ---- the independent count ------------------------------------------------------------------


def parse_consideration_json(text: str) -> dict[int, int]:
    """Month (1-12) -> number of residential agreements, from the file's "Total" row.
    Months without a number (not yet published) are left out."""
    rows = json.loads(text.lstrip("﻿"))
    total = next((r for r in rows if str(r.get(next(iter(r)), "")).strip().lower() == "total"),
                 None)
    if total is None:
        raise ValueError("no Total row in the Land Registry file")
    out = {}
    for i, name in enumerate(_MONTHS, start=1):
        value = re.sub(r"[,\s]", "", str(total.get(name, "")))
        if value.isdigit():
            out[i] = int(value)
    return out


def land_registry_counts(fetcher, years, log: Callable[[str], None] = print) -> dict:
    """{year: {month: count}} for the years the registry publishes (2005 on); a year whose
    file can't be read is logged and left out. One request per year, cached 30 days."""
    counts: dict[int, dict[int, int]] = {}
    for year in years:
        if year < LR_FIRST_YEAR:
            continue
        url = LR_URL.format(year=year)
        try:
            counts[year] = parse_consideration_json(fetcher.get(url, "static"))
        except Exception as e:  # noqa: BLE001 - one missing year must not stop the check
            log(f"Land Registry {year}: {type(e).__name__}: {e}")
    return counts


# ---- the frame ---------------------------------------------------------------------------------


def prepare(deals: pd.DataFrame) -> pd.DataFrame:
    """The snapshot's deals with the few derived columns the checks share."""
    d = deals.copy()
    for col in ("deal_date", "instrument_date", "registration_date"):
        d[col] = pd.to_datetime(d[col], errors="coerce")
    d["price_hkd"] = pd.to_numeric(d["price_hkd"], errors="coerce")
    d["area_sqft"] = pd.to_numeric(d["area_sqft"], errors="coerce")
    d["psf_hkd"] = pd.to_numeric(d["psf_hkd"], errors="coerce")
    for col in EXCL_FLAGS + ["parking"]:
        d[col] = d[col].fillna(False).astype(bool) if col in d else False
    # Ricacorp's derived previous deals are not recorded sales
    d["derived"] = d["source_id"].astype(str).str.endswith(":prev")
    d["stored"] = ~d["excl_duplicate"] & ~d["derived"]  # one copy of every sale
    d["clean"] = d["stored"] & ~d[EXCL_FLAGS].any(axis=1) & ~d["parking"]
    # the registry counts by the month it received the agreement: registration date first
    d["count_date"] = d["registration_date"].where(d["registration_date"].notna(), d["deal_date"])
    d["year"] = d["count_date"].dt.year
    d["has_area"] = d["area_basis"].isin(["saleable", "converted"]) & (d["area_sqft"] > 0)
    labels = d["floor_label"].dropna().unique()
    parsed = {lab: parse_floor(lab).num for lab in labels if isinstance(lab, str)}
    d["has_floor"] = (pd.to_numeric(d["floor_num"], errors="coerce").notna()
                      | d["floor_label"].map(parsed).notna())
    return d


# ---- check 1: sales per year ----------------------------------------------------------------


def yearly_check(d: pd.DataFrame, registry: dict, today: date) -> pd.DataFrame:
    """One row per year: stored sales, clean resales, the registry count, ratio, verdict.
    The current year is compared year-to-date (the months the registry has published)."""
    rows = []
    for year in range(YEAR_FROM, today.year + 1):
        in_year = d["year"] == year
        months = registry.get(year) or {}
        if months and year == today.year:
            last = max(months)
            in_year = in_year & (d["count_date"].dt.month <= last)
            note = f"to {date(year, last, 1):%b}"
        else:
            note = ""
        stored = int((in_year & d["stored"]).sum())
        clean = int((in_year & d["clean"]).sum())
        lr = sum(months.values()) if months else None
        ratio = clean / lr if lr else np.nan
        if lr is None:
            verdict = "no independent count" if year < LR_FIRST_YEAR else "registry file missing"
        elif ratio < RATIO_LOW or ratio > RATIO_HIGH:
            verdict = "look at this"
        else:
            verdict = "ok"
        rows.append({"year": year, "stored": stored, "clean": clean, "registry": lr,
                     "ratio": ratio, "note": note, "verdict": verdict})
    return pd.DataFrame(rows)


# ---- check 2: duplicates left ----------------------------------------------------------------


def duplicates_check(d: pd.DataFrame) -> dict:
    """Pairs of stored rows on the same unit, price within DUP_PRICE_TOL and dates within
    DUP_DAYS, that `excl_duplicate` did not flag. Returns counts by year and source pair,
    the total, and a few examples."""
    cols = ["source", "source_id", "unit_id", "deal_date", "price_hkd", "building_id"]
    base = d.loc[d["stored"] & d["unit_id"].notna() & d["price_hkd"].notna(), cols]
    con = duckdb.connect()
    con.register("base", base)
    pairs = con.execute(f"""
        SELECT a.source AS source_a, a.source_id AS id_a, b.source AS source_b,
            b.source_id AS id_b, a.unit_id, a.building_id, a.deal_date AS date_a,
            b.deal_date AS date_b, a.price_hkd AS price_a, b.price_hkd AS price_b,
            year(a.deal_date) AS year,
            least(a.source, b.source) || '-' || greatest(a.source, b.source) AS pair
        FROM base a JOIN base b ON a.unit_id = b.unit_id
        WHERE (a.source || '|' || a.source_id) < (b.source || '|' || b.source_id)
          AND abs(a.price_hkd - b.price_hkd)
              <= {db.DUP_PRICE_TOL} * greatest(a.price_hkd, b.price_hkd)
          AND abs(date_diff('day', a.deal_date, b.deal_date)) <= {db.DUP_DAYS}
    """).df()
    con.close()
    by_year = (pairs.groupby(["year", "pair"]).size().unstack(fill_value=0).sort_index()
               if not pairs.empty else pd.DataFrame())
    by_pair = pairs["pair"].value_counts().to_dict() if not pairs.empty else {}
    stored = int(d["stored"].sum())
    share = len(pairs) / stored if stored else 0.0
    return {
        "pairs": int(len(pairs)), "stored": stored, "share": share,
        "by_pair": by_pair, "by_year": by_year,
        "examples": pairs.sort_values(["year", "unit_id"]).head(EXAMPLES),
        "verdict": "ok" if share <= DUP_SHARE_MAX else "look at this",
    }


# ---- check 3: odd values ---------------------------------------------------------------------


def odd_values_check(d: pd.DataFrame, today: date) -> dict:
    """Counts per rule and source, plus examples. Judged on stored rows (one copy of each
    sale), so a duplicate copy of an odd deal is not counted twice."""
    s = d[d["stored"]]
    today_ts = pd.Timestamp(today)
    first_ok = pd.Timestamp(year=MIN_DEAL_YEAR, month=1, day=1)
    rules = {
        "date in the future": s["deal_date"] > today_ts,
        f"date before {MIN_DEAL_YEAR}": s["deal_date"] < first_ok,
        f"price under HK${PRICE_MIN / 1e3:,.0f}k": s["price_hkd"] < PRICE_MIN,
        f"price over HK${PRICE_MAX / 1e6:,.0f}m": s["price_hkd"] > PRICE_MAX,
        f"area under {AREA_MIN:,.0f} sq ft": s["has_area"] & (s["area_sqft"] < AREA_MIN),
        f"area over {AREA_MAX:,.0f} sq ft": s["has_area"] & (s["area_sqft"] > AREA_MAX),
        f"price per sq ft under {PSF_MIN:,.0f}": s["psf_hkd"] < PSF_MIN,
        f"price per sq ft over {PSF_MAX:,.0f}": s["psf_hkd"] > PSF_MAX,
    }
    same_day = s.loc[s["unit_id"].notna()].duplicated(["unit_id", "deal_date"], keep=False)
    rules["two sales of one unit on one day"] = same_day.reindex(s.index, fill_value=False)
    out = {}
    for name, mask in rules.items():
        mask = mask.fillna(False).astype(bool)
        hit = s[mask]
        out[name] = {
            "rows": int(mask.sum()),
            "clean": int((mask & s["clean"]).sum()),  # still in the analysis data
            "by_source": hit["source"].value_counts().to_dict(),
            "examples": hit.sort_values("deal_date")[
                ["source", "source_id", "building_id", "floor_label", "flat", "deal_date",
                 "price_hkd", "area_sqft", "psf_hkd"]].head(EXAMPLES),
        }
    out["two sales of one unit on one day"]["units"] = int(
        s.loc[same_day.reindex(s.index, fill_value=False), "unit_id"].nunique())
    total = sum(v["rows"] for v in out.values())
    # rows under HK$500k are excluded by txn_all already; the verdict is about odd rows that
    # the existing rules let through into the clean data
    clean = sum(v["clean"] for v in out.values())
    return {"rules": out, "rows": int(total), "clean": int(clean),
            "verdict": "ok" if clean == 0 else "look at this"}


# ---- check 4: coverage ------------------------------------------------------------------------


def coverage_check(d: pd.DataFrame) -> pd.DataFrame:
    """Per decade of stored sales: count and the share with a building, an area, a floor."""
    s = d[d["stored"] & d["year"].notna()]
    decade = (s["year"] // 10 * 10).astype(int)
    g = pd.DataFrame({
        "sales": s.groupby(decade).size(),
        "building": s["building_id"].notna().groupby(decade).mean(),
        "area": s["has_area"].groupby(decade).mean(),
        "floor": s["has_floor"].groupby(decade).mean(),
    })
    g.index.name = "decade"
    judged = g["sales"] >= COVERAGE_MIN_DEALS
    low = pd.Series(False, index=g.index)
    for col, floor in COVERAGE_MIN.items():
        low |= judged & (g[col] < floor)
    g["verdict"] = np.where(~judged, "too few to judge", np.where(low, "look at this", "ok"))
    return g.reset_index()


def coverage_by_source(d: pd.DataFrame) -> pd.DataFrame:
    """The same shares per source (all years), to show where a gap comes from."""
    s = d[d["stored"]]
    g = pd.DataFrame({
        "sales": s.groupby("source").size(),
        "building": s["building_id"].notna().groupby(s["source"]).mean(),
        "area": s["has_area"].groupby(s["source"]).mean(),
        "floor": s["has_floor"].groupby(s["source"]).mean(),
    })
    g.index.name = "source"
    return g.reset_index()


def unparsed_floor_labels(d: pd.DataFrame, n: int = 10) -> pd.Series:
    """The most common floor labels of stored sales that gave no floor number (bands such
    as 高層 are expected; anything else is a parsing gap)."""
    s = d[d["stored"] & ~d["has_floor"]]
    return s["floor_label"].fillna("(none)").value_counts().head(n)


# ---- the run and the report ------------------------------------------------------------------


def run(raw: dict, registry: dict | None = None, today: date | None = None,
        log: Callable[[str], None] = print) -> dict:
    """Every check on the snapshot `raw`; `registry` is the Land Registry count
    (`land_registry_counts`), or None when it was not fetched."""
    today = today or date.today()
    d = prepare(raw["deals"])
    log(f"{len(d):,} deals in the snapshot, {int(d['stored'].sum()):,} stored sales, "
        f"{int(d['clean'].sum()):,} clean resales")
    yearly = yearly_check(d, registry or {}, today)
    dups = duplicates_check(d)
    odd = odd_values_check(d, today)
    cov = coverage_check(d)
    flagged_years = yearly.loc[yearly["verdict"] == "look at this", "year"].tolist()
    judged = yearly[yearly["registry"].notna()]
    verdicts = {
        "sales per year": ("look at this" if flagged_years else
                           "ok" if not judged.empty else "no independent count"),
        "duplicates left": dups["verdict"],
        "odd values": odd["verdict"],
        "coverage": "look at this" if (cov["verdict"] == "look at this").any() else "ok",
    }
    return {
        "today": today, "meta": raw.get("meta", {}), "deals": int(len(d)),
        "stored": int(d["stored"].sum()), "clean": int(d["clean"].sum()),
        "registry_years": sorted(registry) if registry else [],
        "yearly": yearly, "flagged_years": flagged_years, "duplicates": dups,
        "odd_values": odd, "coverage": cov, "coverage_by_source": coverage_by_source(d),
        "floor_labels": unparsed_floor_labels(d), "verdicts": verdicts,
    }


def _n(x) -> str:
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{int(x):,}"


def _examples(df: pd.DataFrame, names: pd.DataFrame | None) -> list[str]:
    lines = []
    for _, r in df.iterrows():
        where = r.get("building_id")
        where = "(no building)" if where is None or pd.isna(where) else where
        if names is not None and where in names.index:
            b = names.loc[where]
            where = f"{b['estate_name'] or ''} {b['building_name'] or ''}".strip() or where
        when = r["deal_date"].date() if pd.notna(r["deal_date"]) else "-"
        area = f", {r['area_sqft']:,.0f} sq ft" if pd.notna(r.get("area_sqft")) else ""
        lines.append(f"  - {r['source']} `{r['source_id']}`: {where} {r.get('floor_label') or ''}"
                     f"/{r.get('flat') or ''}, {when}, HK${r['price_hkd']:,.0f}{area}")
    return lines


def write_report(res: dict, raw: dict, path: Path | None = None) -> Path:
    today = res["today"]
    path = path or reports_dir() / f"datacheck_{today:%Y%m%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    names = None
    if "buildings" in raw and not raw["buildings"].empty:
        names = raw["buildings"].drop_duplicates("building_id").set_index("building_id")[
            ["estate_name", "building_name"]]
    meta = res["meta"]
    L = [f"# Data check {today:%Y-%m-%d}", "",
         f"Snapshot taken {meta.get('extracted_at', '?')}: {res['deals']:,} deal rows, "
         f"{res['stored']:,} stored sales (one copy each, Ricacorp derived rows left out), "
         f"{res['clean']:,} clean resales (after the txn_all flags: duplicate, under HK$500k, "
         f"first-hand, non-market, bulk, outlier, dated before {MIN_DEAL_YEAR}, parking). "
         "Nothing was changed or deleted.",
         "", "## Verdicts", "", "| Check | Verdict |", "|---|---|"]
    for k, v in res["verdicts"].items():
        L.append(f"| {k} | **{v}** |")

    # 1. sales per year
    y = res["yearly"]
    L += ["", "## 1. Sales per year against the Land Registry", "",
          "Registry = residential sale and purchase agreements received for registration "
          "(Land Registry open data, from 2005; by registration month, so our count uses the "
          "registration date where known). Ratio = clean resales / registry; flagged under "
          f"{RATIO_LOW:.0%} or over {RATIO_HIGH:.0%}. The registry counts first-hand sales "
          "and every residential unit, so a ratio well under 100% is expected.", ""]
    if not res["registry_years"]:
        L.append("The registry files were not read in this run (offline or `--no-fetch`): "
                 "years are shown without a verdict.")
        L.append("")
    L += ["| Year | Stored | Clean resales | Registry | Ratio | Verdict |",
          "|---|---|---|---|---|---|"]
    for _, r in y.iterrows():
        ratio = "-" if pd.isna(r["ratio"]) else f"{r['ratio']:.0%}"
        note = f" ({r['note']})" if r["note"] else ""
        L.append(f"| {r['year']}{note} | {_n(r['stored'])} | {_n(r['clean'])} | "
                 f"{_n(r['registry'])} | {ratio} | {r['verdict']} |")

    # 2. duplicates
    dd = res["duplicates"]
    L += ["", "## 2. Duplicate sales left after `excl_duplicate`", "",
          f"Pairs of stored rows on the same unit, price within {db.DUP_PRICE_TOL:.0%} and "
          f"dates within {db.DUP_DAYS} days: **{dd['pairs']:,}** pairs, {dd['share']:.2%} of "
          f"stored sales (flagged over {DUP_SHARE_MAX:.1%}). Verdict: **{dd['verdict']}**.",
          "", "By source pair: " + (", ".join(f"{k} {v:,}" for k, v in dd["by_pair"].items())
                                   or "none") + ".",
          "A centadata-centadata pair is a Centadata history row and a latest-deal row (or two "
          "history rows) for one sale under two ids; the snapshot cannot tell the two kinds "
          "apart."]
    by = dd["by_year"]
    if not by.empty:
        L += ["", "| Year | " + " | ".join(by.columns) + " |", "|---|" + "---|" * len(by.columns)]
        for year, r in by.iterrows():
            L.append(f"| {int(year)} | " + " | ".join(f"{int(v):,}" for v in r) + " |")
    if not dd["examples"].empty:
        L += ["", "Examples:"]
        for _, r in dd["examples"].iterrows():
            L.append(f"  - {r['unit_id']}: {r['source_a']} `{r['id_a']}` {r['date_a'].date()} "
                     f"HK${r['price_a']:,.0f} vs {r['source_b']} `{r['id_b']}` "
                     f"{r['date_b'].date()} HK${r['price_b']:,.0f}")

    # 3. odd values
    od = res["odd_values"]
    L += ["", "## 3. Odd values", "",
          f"{od['rows']:,} stored rows hit a rule (a row can hit several); {od['clean']:,} of "
          "them are still in the clean data (\"Clean\" column), which is what the verdict "
          f"judges. Verdict: **{od['verdict']}**. Rows are reported, not deleted.", "",
          "| Rule | Rows | Clean | By source |", "|---|---|---|---|"]
    for name, r in od["rules"].items():
        extra = f" ({r['units']:,} units)" if "units" in r else ""
        src = ", ".join(f"{k} {v:,}" for k, v in r["by_source"].items()) or "-"
        L.append(f"| {name} | {r['rows']:,}{extra} | {r['clean']:,} | {src} |")
    for name, r in od["rules"].items():
        if r["rows"]:
            L += ["", f"{name}, examples:"] + _examples(r["examples"], names)

    # 4. coverage
    cv = res["coverage"]
    L += ["", "## 4. Coverage by decade", "",
          "Share of stored sales with a building, a saleable area (own or converted from "
          "gross) and a floor. Flagged when a decade with "
          f"{COVERAGE_MIN_DEALS:,}+ sales has under {COVERAGE_MIN['building']:.0%} building, "
          f"{COVERAGE_MIN['area']:.0%} area or {COVERAGE_MIN['floor']:.0%} floor.", "",
          "| Decade | Sales | Building | Area | Floor | Verdict |", "|---|---|---|---|---|---|"]
    for _, r in cv.iterrows():
        L.append(f"| {int(r['decade'])}s | {int(r['sales']):,} | {r['building']:.1%} | "
                 f"{r['area']:.1%} | {r['floor']:.1%} | {r['verdict']} |")
    L += ["", "By source, all years:", "", "| Source | Sales | Building | Area | Floor |",
          "|---|---|---|---|---|"]
    for _, r in res["coverage_by_source"].iterrows():
        L.append(f"| {r['source']} | {int(r['sales']):,} | {r['building']:.1%} | "
                 f"{r['area']:.1%} | {r['floor']:.1%} |")
    L += ["", "Most common floor labels that gave no floor number (bands such as 高層 are "
          "expected; anything else is a parsing gap): "
          + (", ".join(f"{k} ({v:,})" for k, v in res["floor_labels"].items()) or "none")
          + ".", "", f"Quarantined rows in the snapshot (db.QUARANTINE_SQL): "
          f"{meta.get('quarantined_rows', '?')}.", ""]
    path.write_text("\n".join(L), encoding="utf-8")
    return path
