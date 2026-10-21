"""Attribute growth study (Task 5.3): regression, out-of-sample checks, repeat-sales indices
and drawdown.

- `attribute_frame` turns pair rows into the attributes tested and the controls.
- `fit` regresses annualised log growth on the attributes with district, building age,
  start-period (start year; end year too where it varies) and start-price (log price per
  sq ft) controls. Standard errors are clustered by building (flats in one building move
  together, and for estimated pairs share the model's building-level error).
- `verdicts` runs the two out-of-sample checks (a time split and a district split) and calls
  an attribute "robust" only if p < 0.05 on all pairs and the estimate has the same sign in
  both halves of both splits (decisions.md "Growth study").
- `rs_index` builds a monthly repeat-sales index (log price, first month = 0) and `drawdown`
  its worst peak-to-trough fall.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import scipy.sparse as sp
from scipy.sparse.linalg import lsqr

P_ROBUST = 0.05
MIN_LEVEL_N = 30  # an attribute needs this many pairs with it (and without it)
SQFT_A, SQFT_B, SQFT_C = 430.0, 753.0, 1076.0  # RVD classes A|B|C|D+ in sq ft (40/70/100 m2)
NEAR_MTR, FAR_MTR = 500.0, 1000.0
BIG_ESTATE = 2000
SOUTH = {"S", "SE", "SW"}

# attribute column -> plain-English label (the reference level is in brackets)
ATTRIBUTES = {
    "size_small": "Small flat, under 430 sq ft (vs 430-752)",
    "size_large": "Large flat, 753-1,075 sq ft (vs 430-752)",
    "size_xl": "Very large flat, 1,076 sq ft or more (vs 430-752)",
    "floor_low": "Low floor, bottom third of the building (vs middle third)",
    "floor_high": "High floor, top third of the building (vs middle third)",
    "mtr_near": "Under 500 m from an MTR station (vs 500-1,000 m)",
    "mtr_far": "Over 1,000 m from an MTR station (vs 500-1,000 m)",
    "big_estate": "Large estate, 2,000+ flats (vs smaller)",
    "special_unit": "Special unit: roof, duplex or top floor",
    "facing_south": "Faces south, south-east or south-west (vs other known facings)",
    "sea_view": "Sea view stated in a listing",
    "first_hand_start": "Bought from the developer (first-hand start)",
}
# missing-value indicators: controls, never judged
INDICATORS = ["floor_unknown", "mtr_unknown", "facing_known"]


def attribute_frame(p: pd.DataFrame) -> pd.DataFrame:
    """Pairs plus 0/1 attribute columns, missing-value indicators and controls."""
    d = p.copy()
    area = pd.to_numeric(d["area_sqft"], errors="coerce")
    d["size_small"] = (area < SQFT_A).astype(int)
    d["size_large"] = ((area >= SQFT_B) & (area < SQFT_C)).astype(int)
    d["size_xl"] = (area >= SQFT_C).astype(int)
    rel = pd.to_numeric(d.get("rel_floor"), errors="coerce")
    d["floor_low"] = (rel < 1 / 3).astype(int)
    d["floor_high"] = (rel > 2 / 3).astype(int)
    d["floor_unknown"] = rel.isna().astype(int)
    mtr = pd.to_numeric(d.get("mtr_m"), errors="coerce")
    d["mtr_near"] = (mtr < NEAR_MTR).astype(int)
    d["mtr_far"] = (mtr > FAR_MTR).astype(int)
    d["mtr_unknown"] = mtr.isna().astype(int)
    # estate_units can be a nullable integer: an unknown size compares as NA, count it as "not big"
    d["big_estate"] = (pd.to_numeric(d.get("estate_units"), errors="coerce") >= BIG_ESTATE
                       ).fillna(False).astype(int)
    d["special_unit"] = d.get("special_unit", False).fillna(False).astype(bool).astype(int)
    facing = d.get("facing", pd.Series("unknown", index=d.index)).fillna("unknown")
    d["facing_known"] = (facing != "unknown").astype(int)
    d["facing_south"] = facing.isin(SOUTH).astype(int)
    d["sea_view"] = d.get("sea_view", False).fillna(False).astype(bool).astype(int)
    d["first_hand_start"] = d["first_hand_start"].fillna(False).astype(bool).astype(int)
    built = pd.to_numeric(d.get("built_year"), errors="coerce")
    age = (d["start_date"].dt.year - built).clip(lower=0)
    d["age_start"] = age.fillna(age.median() if age.notna().any() else 0)
    d["age_unknown"] = age.isna().astype(int)
    d["log_start_psf"] = np.log(pd.to_numeric(d["start_psf"], errors="coerce"))
    d["start_year"] = d["start_date"].dt.year.astype("Int64").astype(str)
    d["end_year"] = d["end_date"].dt.year.astype("Int64").astype(str)
    d["district"] = d["district"].fillna("unknown")
    d["building_id"] = d["building_id"].fillna(d["unit_id"])
    d["y"] = d["ann_log_growth"]
    return d[d["y"].notna() & d["log_start_psf"].notna()].reset_index(drop=True)


def testable(d: pd.DataFrame, min_n: int = MIN_LEVEL_N) -> list[str]:
    """Attributes with at least `min_n` pairs on each side, counted only in districts where
    the attribute varies: district is a control, so an attribute that is all-or-nothing
    within each district (e.g. "large estate" when every pair in a district is in one big
    estate) cannot be told apart from the district."""
    out = []
    for a in ATTRIBUTES:
        if a not in d.columns:
            continue
        varies = d.groupby("district")[a].transform("nunique") > 1
        n1 = int(d.loc[varies, a].sum())
        if n1 >= min_n and int(varies.sum()) - n1 >= min_n:
            out.append(a)
    return out


def _design(d: pd.DataFrame, attrs: list[str]) -> pd.DataFrame:
    """Attribute and indicator columns, age (and age squared), log start price, and
    district / start-year / end-year dummies (each dropping its first level)."""
    cols = list(attrs)
    if "facing_south" in attrs:
        cols.append("facing_known")
    cols += [c for c in ("floor_unknown", "mtr_unknown", "age_unknown")
             if c in d.columns and d[c].nunique() > 1]
    X = d[cols].astype(float).copy()
    X["age_start"] = d["age_start"].astype(float)
    X["age_start_sq"] = (d["age_start"].astype(float) ** 2) / 100.0
    X["log_start_psf"] = d["log_start_psf"].astype(float)
    for fe in ("district", "start_year", "end_year"):
        if d[fe].nunique() > 1:
            dummies = pd.get_dummies(d[fe], prefix=fe, drop_first=True, dtype=float)
            X = pd.concat([X, dummies], axis=1)
    X.insert(0, "const", 1.0)
    # drop columns that are constant (e.g. an attribute absent from a subsample)
    keep = [c for c in X.columns if c == "const" or X[c].std() > 0]
    return X[keep]


def _ols_cluster(y: np.ndarray, X: pd.DataFrame, groups) -> pd.DataFrame:
    """OLS with cluster-robust (by group) standard errors, small-sample corrected.
    Returns coef, se, t, p per column (normal p-values; clusters are numerous)."""
    Xv = X.to_numpy(dtype=float)
    beta, *_ = np.linalg.lstsq(Xv, y, rcond=None)
    resid = y - Xv @ beta
    xtx_inv = np.linalg.pinv(Xv.T @ Xv)
    g = pd.factorize(pd.Series(groups).astype(str))[0]
    n_g = g.max() + 1
    n, k = Xv.shape
    # per-cluster sums of X * residual, as a sparse indicator product (np.add.at took
    # minutes on half a million pairs)
    member = sp.csr_matrix((np.ones(n), (g, np.arange(n))), shape=(n_g, n))
    scores = np.asarray(member @ (Xv * resid[:, None]))
    meat = scores.T @ scores
    adj = (n_g / max(n_g - 1, 1)) * ((n - 1) / max(n - k, 1))
    cov = adj * xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = beta / se
    p = np.array([math.erfc(abs(v) / math.sqrt(2)) if np.isfinite(v) else np.nan for v in t])
    return pd.DataFrame({"coef": beta, "se": se, "t": t, "p": p}, index=X.columns)


def fit(d: pd.DataFrame, attrs: list[str] | None = None) -> pd.DataFrame:
    """Coefficients (in annual log growth; x100 = percentage points a year) for the
    attributes, with n and the number of building clusters."""
    attrs = testable(d) if attrs is None else attrs
    X = _design(d, attrs)
    res = _ols_cluster(d["y"].to_numpy(dtype=float), X, d["building_id"])
    out = res.reindex(attrs)
    out["n"] = len(d)
    out["clusters"] = d["building_id"].nunique()
    return out


def time_split(d: pd.DataFrame, end_cut: int = 2018, min_n: int = 200) -> tuple[str, pd.Series]:
    """The plan's split is pairs ending by 2018 vs later. Where either side has fewer than
    `min_n` pairs (real pairs mostly end after 2020; estimated pairs all end now), split by
    start year at the median instead. Returns (description, mask of the first half)."""
    end_year = d["end_date"].dt.year
    early = end_year <= end_cut
    if early.sum() >= min_n and (~early).sum() >= min_n:
        return f"pairs ending by {end_cut} vs later", early
    start = d["start_date"].dt.year
    cut = int(start.median())
    first = start <= cut
    if first.all() or not first.any():
        cut -= 1
        first = start <= cut
    return f"pairs starting by {cut} vs later", first


def district_split(d: pd.DataFrame) -> tuple[str, pd.Series]:
    """Districts dealt into two halves of similar size (largest first, each to the smaller
    half). Returns (description, mask of the first half)."""
    counts = d["district"].value_counts()
    a, b, na, nb = [], [], 0, 0
    for dist, n in counts.items():
        if na <= nb:
            a.append(dist)
            na += n
        else:
            b.append(dist)
            nb += n
    return f"districts {', '.join(map(str, a))} vs the rest", d["district"].isin(a)


def verdicts(d: pd.DataFrame, attrs: list[str] | None = None) -> pd.DataFrame:
    """Full-sample estimate plus both out-of-sample checks per attribute, and the verdict."""
    attrs = testable(d) if attrs is None else attrs
    if not attrs:
        return pd.DataFrame()
    full = fit(d, attrs)
    rows = {a: {"coef": full.loc[a, "coef"], "se": full.loc[a, "se"], "p": full.loc[a, "p"],
                "n_with": int(d[a].sum()), "n": len(d)} for a in attrs}
    checks = {}
    for name, (desc, mask) in {"time": time_split(d), "district": district_split(d)}.items():
        checks[name] = desc
        for half, m in (("a", mask), ("b", ~mask)):
            sub = d[m]
            ok = [a for a in attrs if sub[a].sum() >= 5 and len(sub) - sub[a].sum() >= 5]
            est = fit(sub, ok) if len(sub) > 50 and ok else pd.DataFrame(columns=["coef", "p"])
            for a in attrs:
                rows[a][f"{name}_{half}"] = est["coef"].get(a, np.nan) if a in est.index \
                    else np.nan
                rows[a][f"{name}_{half}_p"] = est["p"].get(a, np.nan) if a in est.index \
                    else np.nan
    out = pd.DataFrame.from_dict(rows, orient="index")
    for name in ("time", "district"):
        same = [np.sign(out[f"{name}_{h}"]) == np.sign(out["coef"]) for h in ("a", "b")]
        out[f"{name}_holds"] = same[0] & same[1]
    out["significant"] = out["p"] < P_ROBUST
    out["robust"] = out["significant"] & out["time_holds"] & out["district_holds"]
    out["label"] = [ATTRIBUTES[a] for a in out.index]
    out.attrs["checks"] = checks
    return out


# ---- summaries ---------------------------------------------------------------------------------


def group_summary(d: pd.DataFrame, attr: str, value_cols=("ann_growth", "net_ann_growth",
                                                           "rental_yield", "total_return")
                  ) -> pd.DataFrame:
    """Medians by attribute level (0/1), with counts."""
    agg = {c: "median" for c in value_cols if c in d.columns}
    g = d.groupby(attr).agg(n=("y", "size"), **{c: (c, f) for c, f in agg.items()})
    return g


# ---- repeat-sales index and drawdown ----------------------------------------------------------

MIN_INDEX_PAIRS = 100
SMOOTH = 4.0  # weight of the month-to-month smoothness penalty (a random-walk prior)


def rs_index(p: pd.DataFrame, smooth: float = SMOOTH) -> pd.Series:
    """Monthly repeat-sales index (Bailey-Muth-Nourse): log(end/start price) = index(end
    month) - index(start month), least squares, first month fixed at 0, with a small penalty
    on month-to-month changes so months with few sales borrow from their neighbours.
    Returns the index level (first month = 100)."""
    s, e = pd.to_datetime(p["start_date"]), pd.to_datetime(p["end_date"])
    sm = (s.dt.year * 12 + s.dt.month).to_numpy()  # months counted from year 0
    em = (e.dt.year * 12 + e.dt.month).to_numpy()
    base, top = int(min(sm.min(), em.min())), int(max(sm.max(), em.max()))
    months = pd.date_range(pd.Timestamp(year=(base - 1) // 12, month=(base - 1) % 12 + 1,
                                        day=1), periods=top - base + 1, freq="MS")
    n, k = len(p), top - base  # column j = month j+1 (month 0 is the base)
    if k == 0:
        return pd.Series([100.0], index=months, name="index")
    ia, ib = sm - base, em - base
    r = np.arange(n)
    rows = np.concatenate([r[ib > 0], r[ia > 0]])
    cols = np.concatenate([ib[ib > 0] - 1, ia[ia > 0] - 1])
    vals = np.concatenate([np.ones((ib > 0).sum()), -np.ones((ia > 0).sum())])
    A = sp.csr_matrix((vals, (rows, cols)), shape=(n, k))
    # smoothness rows: sqrt(smooth) * (x_j - x_{j-1}), with x_{-1} = 0 (base month)
    D = sp.diags([np.ones(k), -np.ones(k - 1)], [0, -1], shape=(k, k)) * math.sqrt(smooth)
    M = sp.vstack([A, D]).tocsr()
    y = np.concatenate([np.log(p["end_price"].to_numpy(float) / p["start_price"].to_numpy(float)),
                        np.zeros(k)])
    x = lsqr(M, y, atol=1e-10, btol=1e-10)[0]
    level = 100 * np.exp(np.concatenate([[0.0], x]))
    return pd.Series(level, index=months, name="index")


def drawdown(index: pd.Series, start=None, end=None) -> dict:
    """Worst peak-to-trough fall of an index (optionally within [start, end])."""
    s = index
    if start is not None:
        s = s[s.index >= pd.Timestamp(start)]
    if end is not None:
        s = s[s.index <= pd.Timestamp(end)]
    if s.empty:
        return {"drawdown": np.nan, "peak": None, "trough": None}
    peak = s.cummax()
    dd = 1 - s / peak
    trough = dd.idxmax()
    peak_date = s[:trough].idxmax()
    return {"drawdown": float(dd.max()), "peak": peak_date, "trough": trough}


def annualised(index: pd.Series) -> float:
    if len(index) < 2:
        return np.nan
    years = (index.index[-1] - index.index[0]).days / 365.25
    return float((index.iloc[-1] / index.iloc[0]) ** (1 / years) - 1) if years > 0 else np.nan


def rvd_long_run(rvd: pd.DataFrame) -> pd.DataFrame:
    """Long-run context from RVD: price index by size class (1993 on) and the popular
    developments index (1992 on): annualised growth, worst drawdown, peak and trough."""
    rows = []
    for series, label in (("rvd_price_index", "Price index"),
                          ("rvd_popular_dev_index", "Popular developments")):
        s = rvd[(rvd["series"] == series) & (rvd["freq"] == "month")]
        for (cls, region), g in s.groupby(["class", "region"]):
            if series == "rvd_price_index" and region != "ALL":
                continue
            ser = g.assign(period=pd.to_datetime(g["period"])).set_index("period")["value"]
            ser = ser.sort_index().dropna()
            if len(ser) < 24:
                continue
            dd = drawdown(ser)
            rows.append({"series": label, "class": cls, "region": region,
                         "from": ser.index[0], "to": ser.index[-1],
                         "ann_growth": annualised(ser), "drawdown": dd["drawdown"],
                         "peak": dd["peak"], "trough": dd["trough"]})
    return pd.DataFrame(rows)
