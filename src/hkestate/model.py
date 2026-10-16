"""Fair-price model (Task 4.2): LightGBM on log price per sq ft (saleable basis).

Target. Trees can't extrapolate in time, so the target is log price per sq ft divided by the
RVD price index for the flat's size class (A-E), with the index taken relative to a
reference month. The model learns flat, building and location effects on a common market
level; the month index is still a feature for what the index misses (e.g. one district
moving differently).

Out-of-time test (plan): train on deals up to T-6 months, test on the last 6 months. The
index is *frozen* at the cut-off for test deals (the model never sees the market level of
the test period), which is what valuing a listing today looks like: RVD publishes about two
months late.

Three fits, all with the same settings:
1. fit on deals up to T-12, predict T-12..T-6: calibration residuals for the intervals
2. fit on deals up to T-6, predict the last 6 months: the reported out-of-time error, the
   interval coverage check, and a same-building comparables check alongside
3. fit on everything: the model `hkestate rank` uses, with 80% intervals from step 2's
   out-of-time residuals (split conformal, per group: pilot estates with full history vs the
   territory, which so far has only Centadata's latest deal per flat)

SHAP values come from LightGBM's TreeSHAP (`pred_contrib=True`); see decisions.md.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from hkestate import features
from hkestate.config import get_data_dir

NUM_FEATURES = ["area_sqft", "rooms", "floor_num", "rel_floor", "top_floor", "age_years",
                "mtr_m", "lat", "lon", "estate_units", "month_idx", "sea_view", "special_unit"]
CAT_FEATURES = ["facing", "district", "estate_key", "building_id"]
FEATURES = NUM_FEATURES + CAT_FEATURES

PARAMS = {
    "objective": "regression",
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 20,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    # buildings and estates are categories with thousands of values: smooth them so a
    # building with a handful of deals leans on its estate and district
    "max_cat_to_onehot": 4,
    "max_cat_threshold": 64,
    "cat_l2": 10.0,
    "cat_smooth": 20.0,
    "min_data_per_group": 20,
    "verbose": -1,
    "seed": 7,
    "deterministic": True,
    "num_threads": 4,  # other jobs share the laptop
}
ROUNDS = 800
TEST_MONTHS = 6
COVERAGE = 0.8  # 80% intervals
MIN_GROUP_N = 50  # fewer calibration residuals in a group: use all groups' residuals
COMPS_MONTHS = 24
COMPS_MIN = 3  # the comparables check needs this many same-building deals

LABELS = {
    "area_sqft": "size", "rooms": "rooms", "floor_num": "floor", "rel_floor": "floor position",
    "top_floor": "building height", "age_years": "building age", "mtr_m": "MTR distance",
    "lat": "location N-S", "lon": "location E-W", "estate_units": "estate size",
    "month_idx": "date", "sea_view": "sea view", "special_unit": "special unit",
    "facing": "facing", "district": "district", "estate_key": "estate", "building_id": "building",
}


def model_dir() -> Path:
    return get_data_dir() / "model"


def report_path() -> Path:
    return get_data_dir() / "reports" / "model_eval.md"


def group_of(df: pd.DataFrame) -> pd.Series:
    return pd.Series(np.where(df["is_pilot"].astype(bool), "pilot", "territory"), index=df.index)


def trainable(deals: pd.DataFrame) -> pd.DataFrame:
    return deals[deals["exclude_reason"].isna() & deals["log_psf"].notna()].copy()


# ---- time and index --------------------------------------------------------------------------


@dataclass
class Split:
    fit: pd.DataFrame
    cal: pd.DataFrame
    test: pd.DataFrame
    cal_start: pd.Timestamp
    test_start: pd.Timestamp
    end: pd.Timestamp


def time_split(df: pd.DataFrame, test_months: int = TEST_MONTHS) -> Split:
    """Test = the last `test_months` before the newest deal; calibration = the
    `test_months` before that; fit = everything earlier."""
    end = df["agreement_date"].max()
    test_start = end - pd.DateOffset(months=test_months)
    cal_start = test_start - pd.DateOffset(months=test_months)
    ad = df["agreement_date"]
    return Split(df[ad <= cal_start], df[(ad > cal_start) & (ad <= test_start)],
                 df[ad > test_start], cal_start, test_start, end)


_index_wide = features.index_wide
deflator = features.deflator


def _target(df: pd.DataFrame, index_df: pd.DataFrame, known_until) -> tuple[np.ndarray,
                                                                               np.ndarray]:
    """(index-adjusted log price per sq ft, log deflator)."""
    logd = np.log(deflator(index_df, df["rvd_class"], df["agreement_date"], known_until))
    return df["log_psf"].to_numpy(dtype=float) - logd, logd


# ---- fitting -----------------------------------------------------------------------------------


def matrix(df: pd.DataFrame, categories: dict[str, list[str]]) -> pd.DataFrame:
    X = pd.DataFrame(index=df.index)
    for c in NUM_FEATURES:
        X[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    for c in CAT_FEATURES:
        vals = df[c].astype(object).where(df[c].notna(), None)
        vals = vals.map(lambda v: None if v is None else str(v))
        # a value the model never saw (e.g. a new building) is missing to it
        vals = vals.where(vals.isin(categories[c]), None)
        X[c] = pd.Categorical(vals, categories=categories[c])
    return X


def fit(df: pd.DataFrame, y: np.ndarray, rounds: int = ROUNDS):
    cats = {c: sorted(df[c].dropna().astype(str).unique().tolist()) for c in CAT_FEATURES}
    ds = lgb.Dataset(matrix(df, cats), label=y, categorical_feature=CAT_FEATURES)
    return lgb.train(PARAMS, ds, num_boost_round=rounds), cats


def shap_frame(booster, X: pd.DataFrame) -> pd.DataFrame:
    contrib = booster.predict(X, pred_contrib=True)
    return pd.DataFrame(contrib, index=X.index,
                        columns=[f"shap_{f}" for f in FEATURES] + ["shap_base"])


# ---- errors, intervals, comparables --------------------------------------------------------


def abs_pct_error(actual_log, pred_log) -> np.ndarray:
    return np.abs(np.exp(np.asarray(pred_log) - np.asarray(actual_log)) - 1)


def conformal_q(residuals, groups, coverage: float = COVERAGE,
                min_n: int = MIN_GROUP_N) -> dict[str, float]:
    """Split-conformal half-width (in log price) per group and for all: the
    ceil((n+1)*coverage)-th smallest absolute residual. Groups with fewer than `min_n`
    residuals are left out (callers fall back to 'all')."""
    res = pd.Series(np.asarray(residuals, dtype=float))
    grp = pd.Series(np.asarray(groups))

    def q(r: pd.Series) -> float:
        r = np.sort(r.dropna().to_numpy())
        k = min(math.ceil((len(r) + 1) * coverage), len(r))
        return float(r[k - 1])

    out = {"all": q(res)} if res.notna().any() else {}
    for g in grp.unique():
        r = res[grp == g]
        if r.notna().sum() >= min_n:
            out[str(g)] = q(r)
    return out


def q_for(q: dict[str, float], group: str) -> float:
    return q.get(group, q["all"])


def comparables(hist: pd.DataFrame, y_hist: np.ndarray, target: pd.DataFrame,
                cutoff: pd.Timestamp, months: int = COMPS_MONTHS) -> pd.DataFrame:
    """Same-building comparables: deals in the `months` before `cutoff`. Returns, per target
    row, the count and (with at least COMPS_MIN) the median index-adjusted log price/sq ft."""
    h = hist.assign(_y=y_hist)
    h = h[(h["agreement_date"] > cutoff - pd.DateOffset(months=months))
          & (h["agreement_date"] <= cutoff)]
    g = h.groupby("building_id")["_y"].agg(["median", "size"])
    n = target["building_id"].map(g["size"]).fillna(0).astype(int)
    med = target["building_id"].map(g["median"]).where(n >= COMPS_MIN)
    return pd.DataFrame({"comps_n": n, "comps_adj": med}, index=target.index)


# ---- evaluation ----------------------------------------------------------------------------------


@dataclass
class EvalResult:
    summary: pd.DataFrame
    test_predictions: pd.DataFrame
    q_cal: dict
    split: dict
    importance: pd.Series
    counts: dict = field(default_factory=dict)


def _summary(tp: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, part in [("all", tp), ("pilot", tp[tp["group"] == "pilot"]),
                       ("territory", tp[tp["group"] == "territory"])]:
        if part.empty:
            continue
        has = part["comps_ape"].notna()
        rows.append({
            "group": name, "n_test": len(part),
            "mdape": part["ape"].median(),
            # median signed error (model / actual - 1): negative = the model runs low
            "bias": float(np.median(np.exp(part["pred_log_psf"] - part["log_psf"]) - 1)),
            "within_10": (part["ape"] <= 0.10).mean(),
            "within_20": (part["ape"] <= 0.20).mean(),
            "interval_coverage": part["inside"].mean(),
            "comps_covered": has.mean(),
            "comps_mdape": part.loc[has, "comps_ape"].median() if has.any() else np.nan,
            "model_mdape_on_comps": part.loc[has, "ape"].median() if has.any() else np.nan,
        })
    return pd.DataFrame(rows)


def evaluate(deals: pd.DataFrame, index_df: pd.DataFrame, rounds: int = ROUNDS) -> EvalResult:
    tr = trainable(deals)
    s = time_split(tr)

    # 1. calibration residuals (model trained up to T-12, frozen index at T-12)
    y_fit, _ = _target(s.fit, index_df, s.cal_start)
    b1, c1 = fit(s.fit, y_fit, rounds)
    _, logd_cal = _target(s.cal, index_df, s.cal_start)
    cal_pred = b1.predict(matrix(s.cal, c1)) + logd_cal
    q_cal = conformal_q(np.abs(s.cal["log_psf"].to_numpy() - cal_pred), group_of(s.cal))

    # 2. the out-of-time test (trained up to T-6, frozen index at T-6)
    hist = pd.concat([s.fit, s.cal])
    y_hist, _ = _target(hist, index_df, s.test_start)
    b2, c2 = fit(hist, y_hist, rounds)
    Xt = matrix(s.test, c2)
    shap = shap_frame(b2, Xt)
    pred_adj = shap.sum(axis=1).to_numpy()
    _, logd_test = _target(s.test, index_df, s.test_start)
    pred_log = pred_adj + logd_test
    comps = comparables(hist, y_hist, s.test, s.test_start)

    tp = s.test[["source", "source_id", "building_id", "unit_id", "agreement_date",
                 "price_hkd", "area_sqft", "log_psf", "is_pilot"]].copy()
    tp["group"] = group_of(s.test)
    tp["pred_adj"] = pred_adj
    tp["pred_log_psf"] = pred_log
    tp["ape"] = abs_pct_error(tp["log_psf"], pred_log)
    half = tp["group"].map(lambda g: q_for(q_cal, g))
    tp["inside"] = (tp["log_psf"] >= pred_log - half) & (tp["log_psf"] <= pred_log + half)
    tp["abs_log_res"] = np.abs(tp["log_psf"] - pred_log)
    tp["comps_n"] = comps["comps_n"]
    tp["comps_log_psf"] = comps["comps_adj"] + logd_test
    tp["comps_ape"] = pd.Series(abs_pct_error(tp["log_psf"], tp["comps_log_psf"]),
                                index=tp.index).where(tp["comps_log_psf"].notna())
    tp = pd.concat([tp, shap], axis=1)

    gain = pd.Series(b2.feature_importance("gain"), index=b2.feature_name())
    split = {k: str(getattr(s, k).date()) for k in ("cal_start", "test_start", "end")}
    counts = {"fit": len(s.fit), "cal": len(s.cal), "test": len(s.test)}
    return EvalResult(_summary(tp), tp.reset_index(drop=True), q_cal, split,
                      (gain / gain.sum()).sort_values(ascending=False), counts)


# ---- explanations ------------------------------------------------------------------------------


def _fmt(feature: str, v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA:
        return "unknown"
    if feature in ("sea_view", "special_unit"):
        return "yes" if bool(v) else "no"
    if feature in CAT_FEATURES:
        return str(v)
    v = float(v)
    return {
        "area_sqft": f"{v:.0f} sq ft", "floor_num": f"{v:.0f}/F", "rel_floor": f"{v:.0%} up",
        "top_floor": f"{v:.0f} floors", "age_years": f"{v:.0f} yrs", "mtr_m": f"{v:.0f} m",
        "estate_units": f"{v:.0f} flats", "rooms": f"{v:.0f}",
        "month_idx": f"{2000 + int(v) // 12}-{int(v) % 12 + 1:02d}",
    }.get(feature, f"{v:.3f}")


def reasons(shap_row, values, n: int = 3) -> list[str]:
    """The n biggest effects as text, e.g. 'floor 30/F: +5.1%' (the effect on price)."""
    items = [(k[len("shap_"):], float(v)) for k, v in dict(shap_row).items()
             if k.startswith("shap_") and k != "shap_base"]
    items.sort(key=lambda kv: -abs(kv[1]))
    return [f"{LABELS.get(f, f)} {_fmt(f, values.get(f))}: {math.exp(s) - 1:+.1%}"
            for f, s in items[:n]]


# ---- the trained model ---------------------------------------------------------------------------


@dataclass
class FairPrice:
    booster: lgb.Booster
    meta: dict

    @property
    def categories(self) -> dict:
        return self.meta["categories"]


def predict_adj(m: FairPrice, df: pd.DataFrame) -> np.ndarray:
    """Index-adjusted log price per sq ft (at the model's reference month)."""
    if df.empty:
        return np.array([], dtype=float)
    return m.booster.predict(matrix(df, m.categories))


def predict_shap(m: FairPrice, df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[f"shap_{f}" for f in FEATURES] + ["shap_base"])
    return shap_frame(m.booster, matrix(df, m.categories))


def load(path: Path | None = None) -> FairPrice:
    d = path or model_dir()
    meta = json.loads((d / "fair_price.json").read_text("utf-8"))
    return FairPrice(lgb.Booster(model_file=str(d / "fair_price.txt")), meta)


def coverage_table(train: pd.DataFrame, listings: pd.DataFrame) -> pd.DataFrame:
    """Share of rows with each feature known, in training and among valued listings."""
    rows = []
    for f in FEATURES:
        def known(df, f=f):
            if df.empty:
                return np.nan
            col = df[f]
            if f == "facing":
                return float((col != "unknown").mean())
            if f in ("sea_view", "special_unit"):
                return float(col.astype(bool).mean())  # share flagged, not "known"
            return float(col.notna().mean())
        rows.append({"feature": f, "label": LABELS[f], "train": known(train),
                     "listings": known(listings)})
    return pd.DataFrame(rows)


VALUATION_MIN_DEALS = 200


def valuation_month(tr: pd.DataFrame) -> int:
    """The month index listings are valued at: the newest month with at least 200 trainable
    deals (or half the recent monthly median, for small data). The current month has only a
    few deals so far, and a tree would let that handful set the date effect for every
    listing; the RVD index carries the market level forward anyway."""
    counts = tr.groupby("month_idx").size().sort_index()
    need = min(VALUATION_MIN_DEALS, 0.5 * counts.tail(12).median())
    ok = counts[counts >= need]
    return int(ok.index.max() if not ok.empty else counts.index.max())


def train(raw: dict, rounds: int = ROUNDS, today: date | None = None, log=print) -> dict:
    """Evaluate, fit the final model, save it with its intervals, write model_eval.md."""
    today = today or date.today()
    deals = features.deal_features(raw)
    log(f"deals: {len(deals)}, trainable {deals['exclude_reason'].isna().sum()}")
    ev = evaluate(deals, raw["index"], rounds)
    log(f"out-of-time test: {ev.summary.to_dict('records')}")

    tr = trainable(deals)
    y_all, _ = _target(tr, raw["index"], None)
    booster, cats = fit(tr, y_all, rounds)
    tp = ev.test_predictions
    q_final = conformal_q(tp["abs_log_res"], tp["group"])

    out_dir = model_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path, meta_path = out_dir / "fair_price.txt", out_dir / "fair_price.json"
    booster.save_model(str(model_path))
    index_last = str(_index_wide(raw["index"]).index.max().date())
    meta = {
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "features": FEATURES, "categories": cats, "rounds": rounds, "params": PARAMS,
        "q": q_final, "coverage": COVERAGE, "index_reference_month": index_last,
        "valuation_month_idx": valuation_month(tr),
        "n_train": len(tr), "n_train_pilot": int(tr["is_pilot"].sum()),
        "eval": ev.summary.to_dict("records"), "split": ev.split, "data": raw["meta"],
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), "utf-8")
    test_path = out_dir / "test_predictions.parquet"
    tp.to_parquet(test_path, index=False)
    # the dashboard's comparables and price histories read this (no database needed)
    keep = ["source", "source_id", "unit_id", "building_id", "building_name", "estate_name",
            "floor_label", "floor_num", "rel_floor", "flat", "agreement_date", "price_hkd",
            "area_sqft", "psf_hkd", "exclude_reason", "is_pilot", "district"]
    deals[keep].to_parquet(out_dir / "deals.parquet", index=False)

    listings = features.listing_features(raw, today)
    valued = listings[listings["value_note"].isna()]
    path = report_path()
    write_report(path, ev, deals, valued, meta)
    log(f"report: {path}")
    return {"model_path": model_path, "meta_path": meta_path, "report_path": path,
            "test_path": test_path, "eval": ev, "meta": meta}


# ---- report ---------------------------------------------------------------------------------


def _pct(x) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.1%}"


def write_report(path: Path, ev: EvalResult, deals: pd.DataFrame, listings: pd.DataFrame,
                 meta: dict) -> None:
    tr = trainable(deals)
    s = ev.summary.set_index("group")
    lines = [
        f"# Fair-price model evaluation ({meta['trained_at'][:10]})",
        "",
        "Written by `hkestate train`; rerun it as data grows. Generated, not edited by hand.",
        "",
        "## Headline",
        "",
    ]
    for g, text in [("all", "All test deals"), ("pilot", "Pilot estates (full history)"),
                    ("territory", "Territory (Centadata latest deal per flat only)")]:
        if g in s.index:
            lines.append(f"- **{text}:** median absolute % error **{_pct(s.loc[g, 'mdape'])}** "
                         f"on {int(s.loc[g, 'n_test']):,} deals; {_pct(s.loc[g, 'within_10'])} "
                         f"within 10%; 80% intervals held {_pct(s.loc[g, 'interval_coverage'])}.")
    lines += [
        "",
        "## Set-up",
        "",
        "- Target: log price per sq ft (saleable, or gross converted with the building's own "
        "ratio), divided by the RVD price index for the flat's size class.",
        f"- Out-of-time test: trained on deals up to {ev.split['test_start']}, tested on "
        f"{ev.split['test_start']} to {ev.split['end']} (the last 6 months), with the index "
        "frozen at the cut-off (the model never sees the test period's market level).",
        f"- Calibration for the 80% intervals: a model trained up to {ev.split['cal_start']} "
        f"predicting {ev.split['cal_start']} to {ev.split['test_start']}. The final model's "
        "intervals use the test residuals (per group).",
        f"- Rows: fit {ev.counts['fit']:,}, calibration {ev.counts['cal']:,}, test "
        f"{ev.counts['test']:,}. Final model trained on all {meta['n_train']:,} "
        f"({meta['n_train_pilot']:,} pilot).",
        f"- Dates: agreement dates; 28Hse registration dates moved back "
        f"{meta['data'].get('agreement_lag_days')} days (median Centadata gap).",
        "",
        "## Results by group",
        "",
        "Comparables check: the median index-adjusted price per sq ft of the same building's "
        f"deals in the {COMPS_MONTHS} months before the cut-off (needs {COMPS_MIN}+), "
        "compared on the test deals it covers.",
        "",
        "| Group | Test deals | Model median abs % error | Median signed error | Within 10% | "
        "Within 20% | 80% interval held | Comparables cover | Comparables error | "
        "Model error on same deals |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for g, row in s.iterrows():
        lines.append(
            f"| {g} | {int(row['n_test']):,} | {_pct(row['mdape'])} | {row['bias']:+.1%} | "
            f"{_pct(row['within_10'])} | {_pct(row['within_20'])} | "
            f"{_pct(row['interval_coverage'])} | {_pct(row['comps_covered'])} | "
            f"{_pct(row['comps_mdape'])} | {_pct(row['model_mdape_on_comps'])} |")
    q = meta["q"]
    lines += [
        "",
        "80% interval half-widths in the final model (in price): "
        + ", ".join(f"{k} ±{math.exp(v) - 1:.1%}" for k, v in q.items()) + ".",
        "",
        "Signed error: with the index frozen at the cut-off, a market that moves during the "
        "test period shows up as a bias (negative = the model valued below the actual deal). "
        "Listings are valued with the index carried forward from its last published month, "
        f"{meta['index_reference_month'][:7]}, so the same effect applies on a smaller scale.",
        "",
    ]
    if {"pilot", "territory"} <= set(s.index):
        better = "pilot" if s.loc["pilot", "mdape"] < s.loc["territory", "mdape"] else "territory"
        lines += [
            "Pilot vs territory: pilot estates have every 28Hse deal since 2018 plus Centadata's "
            "latest deal per flat; territory estates so far have only Centadata's latest deal "
            "per flat (plus the first 28Hse estates of the territory backfill), so their "
            "buildings' recent comparables are fewer and their test deals are only the flats "
            "whose latest deal fell in the test window. On this run the "
            f"**{better}** group has the lower error. The territory 28Hse backfill (Task 3.2, "
            "about 9 days) adds full histories; retrain then and compare.",
            "",
        ]
    lines += [
        "## Rows left out of training",
        "",
        "| Reason | Deals |",
        "|---|---:|",
    ]
    for reason, n in deals["exclude_reason"].fillna("(trained on)").value_counts().items():
        lines.append(f"| {reason} | {n:,} |")
    lines += [
        f"| quarantined LOHAS Park rows (not read at all) | "
        f"{meta['data'].get('quarantined_rows', 0):,} |",
        "",
        "## Feature coverage",
        "",
        "Share of rows with the feature known (for sea view and special unit: share flagged). "
        "Missing values stay missing; the model learns a direction for them.",
        "",
        "| Feature | Training deals | Valued listings |",
        "|---|---:|---:|",
    ]
    for _, r in coverage_table(tr, listings).iterrows():
        lines.append(f"| {r['label']} (`{r['feature']}`) | {_pct(r['train'])} | "
                     f"{_pct(r['listings'])} |")
    lines += [
        "",
        "MTR distance and coordinates come from the geocoding run (Task 3.3), which is still "
        "partial; facing and sea view only from listings that state them.",
        "",
        "## What drives the model (share of total gain, test-stage model)",
        "",
        "| Feature | Share |",
        "|---|---:|",
    ]
    for f, v in ev.importance.head(12).items():
        lines.append(f"| {LABELS.get(f, f)} | {v:.1%} |")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), "utf-8")
