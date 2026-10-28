"""Ranked listings (Task 4.4): every active sale listing valued by the fair-price model and
sorted by its discount to fair price. No cut-off (decisions.md: "all is relative").

discount = 1 - expected deal price / fair price, where the expected deal price is the asking
price less the district's usual asking-vs-deal gap (gap.py; 5% `assumed` until a district
has 30 listing-deal pairs). Positive = the listing should sell below the model's value.

Shown alongside, never changing the order: the 80% interval, confidence (`low` with fewer
than 10 comparable deals in the same building in the last 24 months), keyword flags, and
the top 3 SHAP reasons. Listings on several sites (same `dup_group`), and the same flat listed
twice (same building, area and price within 2%), are ranked once, at the lowest asking price,
with the others in `also_listed`.

Listing checks (`listing_checks`, docs/decisions.md "Listing checks"): a row with a data
problem or doubtful value gets an `exclude_reason` (subsidised, check_area, check_price, ...)
and goes after all other rows; `below_all_recent` is information, not an exclusion.

Outputs: `data/reports/ranked_listings_<date>.csv` (UTF-8 with BOM so Excel shows Chinese)
and `data/model/ranked_latest.parquet` (with SHAP values, for the dashboard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from hkestate import features, gap, model
from hkestate.config import get_data_dir

LOW_CONFIDENCE_COMPS = 10
COMPS_MONTHS = 24


def discount(asking, fair, gap_pct):
    """1 - expected deal price / fair price (positive = below fair price)."""
    return 1 - np.asarray(asking, dtype=float) * (1 - np.asarray(gap_pct, dtype=float)) / (
        np.asarray(fair, dtype=float))


@dataclass
class Ranked:
    table: pd.DataFrame  # one row per valued listing, best discount first
    gaps: pd.DataFrame  # per district
    counts: dict


def _comps(deals: pd.DataFrame, today: date) -> pd.DataFrame:
    since = pd.Timestamp(today) - pd.DateOffset(months=COMPS_MONTHS)
    c = deals[deals["exclude_reason"].isna() & (deals["agreement_date"] > since)]
    return c.groupby("building_id").agg(comps_24m=("source_id", "size"),
                                        comps_median_psf=("psf_hkd", "median"))


# ---- listing checks (plan 2026-10-23 Tasks 2-3; thresholds: docs/decisions.md "Listing checks")
# A check never changes a listing's value; it names a reason to look at the listing before
# believing its discount. Rows with a reason go to the end of the ranking, never deleted.
SUBSIDISED_PATH = Path(__file__).with_name("subsidised_estates.csv")
AREA_MIN_SQFT, AREA_MAX_SQFT = 150, 5000
AREA_EXACT_TOL = 0.03          # within 3% of a recorded unit area = the same saleable area
GROSS_RATIO = (1.12, 1.45)     # listed / recorded area in this band looks like a gross area
NOT_ONE_FLAT_RATIO = 1.5       # at or above: a typo or more than one flat
MIN_RECORDED_AREAS = 3         # recorded areas a building needs before we compare without a flat
PRICE_RATIO = (0.6, 2.5)       # asking vs the building's index-adjusted median: typo guard
MODEL_MIN_COMPS, MODEL_RATIO = 5, 1.25
SUBSIDISED_RATIO, SUBSIDISED_MIN_DEALS = 0.7, 3
BELOW_ALL_MIN_COMPS = 3        # below_all_recent needs this many deals in the window
PEER_ASK_RATIO = 0.8           # check_peer_ask: asking psf under this x the peers' median
PEER_ASK_MIN = 3               # ... needs this many other active sale asks in the building
PEER_AREA_TOL = 0.03           # ... whose area is within 3%
DUP_TOLERANCE = 0.02           # same-flat duplicates: area and price within 2%
BLANK_FLAT_TOLERANCE = 0.01    # flat missing on a side: area and price must agree to 1%
MAX_BLANK_FLAT_MERGES = 3      # and one kept row absorbs at most this many such rows
_NOT_ONE_FLAT_WORDS = re.compile(r"複式|天台")
# No `check_match`: match_building returns no building for a doubtful match, so such a
# listing never gets here (it is counted as "no building match"). Giving it a building id
# would make gap.py, the facing/age tables and the dashboard treat it as a confirmed match.
CHECK_ORDER = ["subsidised", "check_area", "check_combined", "check_area_basis",
               "check_price", "check_model", "check_subsidised", "check_peer_ask"]


_NAME_VARIANTS = str.maketrans({"臺": "台", "叠": "疊", "悅": "悦"})


def subsidised_name(name) -> str:
    """Estate name in the form the subsidised list is compared in: no spaces, and one form
    of the characters the official lists and the listing sites spell two ways (臺/台 ...)."""
    if not isinstance(name, str):
        return ""
    return "".join(name.split()).translate(_NAME_VARIANTS)


def subsidised_estates(path: Path | None = None) -> frozenset[str]:
    """Housing Authority (HOS, PSPS, GSH, TPS) and Housing Society (Flat-for-Sale, Sandwich
    Class, Subsidised Sale Flats) estates: resale rules distort their prices and the project
    covers private flats only. Keys are 'name|district' (18-district name, e.g. '離島區'), or
    just 'name' when the list gives no district. The district matters: private estates share
    names with subsidised ones elsewhere (富景花園 is a PSPS court in 東區 and a private estate
    in 中西區)."""
    df = pd.read_csv(path or SUBSIDISED_PATH, encoding="utf-8-sig", dtype=str)
    if "district" not in df:
        df["district"] = ""
    keys = set()
    for name, district in zip(df["estate_name"], df["district"].fillna(""), strict=True):
        name = subsidised_name(name)
        if name:
            keys.add(f"{name}|{district.strip()}" if district.strip() else name)
    return frozenset(keys)


def _is_subsidised(li: pd.DataFrame, subsidised) -> pd.Series:
    """A listing's estate is on the list: same name and district, or same name when either
    side has no district."""
    name = li["estate_name"].map(subsidised_name)
    district = (li["district"] if "district" in li else pd.Series("", index=li.index)).fillna("")
    named = {k.split("|")[0] for k in subsidised}
    return (name.isin(subsidised) | (name + "|" + district).isin(subsidised)
            | (district.eq("") & name.isin(named))) & name.ne("")


def _text(*values) -> str:
    return " ".join(v for v in values if isinstance(v, str))


def _nearest(areas: np.ndarray, x: float) -> float:
    return float(areas[np.abs(areas - x).argmin()])


def _area_reasons(li: pd.DataFrame, deals: pd.DataFrame) -> pd.Series:
    """check_area / check_combined / check_area_basis, against the unit areas the building's
    deals record (saleable basis). Flat letters match their recorded area to 0.998-1.000."""
    rec = deals[(deals["area_basis"] == "saleable") & (deals["area_sqft"] > 0)
                & deals["building_id"].notna()][["building_id", "flat", "area_sqft"]].copy()
    rec["area_sqft"] = rec["area_sqft"].round(1)
    rec = rec.drop_duplicates()
    by_building = {b: np.sort(g["area_sqft"].unique()) for b, g in rec.groupby("building_id")}
    by_flat = {k: np.sort(g["area_sqft"].unique())
               for k, g in rec.dropna(subset=["flat"]).groupby(["building_id", "flat"])}
    out = []
    for b, flat, area, feat, ftext in zip(li["building_id"], li["flat"], li["area_sqft"],
                                          li["features"], li["floor_text"], strict=True):
        text = _text(feat, ftext)
        combined = "相鄰" in text
        special = bool(_NOT_ONE_FLAT_WORDS.search(text))
        if not area > 0:
            out.append(None)
        elif area < AREA_MIN_SQFT or area > AREA_MAX_SQFT:
            out.append("check_area")
        else:
            everything = by_building.get(b)
            enough = everything is not None and len(everything) >= MIN_RECORDED_AREAS
            ref = by_flat.get((b, flat)) if isinstance(flat, str) and flat else None
            if ref is None and enough:
                ref = everything
            ratio = area / _nearest(ref, area) if ref is not None else None
            if ratio is not None and ratio >= NOT_ONE_FLAT_RATIO and enough \
                    and not (special or combined):
                out.append("check_area")
            elif combined:
                out.append("check_combined")
            elif ratio is not None and not special and GROSS_RATIO[0] <= ratio <= GROSS_RATIO[1]:
                out.append("check_area_basis")
            else:
                out.append(None)
    return pd.Series(out, index=li.index, dtype=object)


def _peer_ask_low(li: pd.DataFrame) -> pd.Series:
    """True where a listing asks under PEER_ASK_RATIO x the median asking psf of at least
    PEER_ASK_MIN *other* listings in `li` (active sale listings) of its building whose area
    is within PEER_AREA_TOL. Catches bait asks the deal history cannot (a building with few
    or old deals). Cross-site copies of one flat count as peers; they only pull the median
    towards the listing's own ask, so the check errs towards not flagging."""
    out = pd.Series(False, index=li.index)
    for rows in li.groupby("building_id").indices.values():
        if len(rows) <= PEER_ASK_MIN:
            continue
        area = li["area_sqft"].to_numpy(dtype=float)[rows]
        ask = li["asking_psf"].to_numpy(dtype=float)[rows]
        near = np.abs(area[:, None] - area[None, :]) <= PEER_AREA_TOL * area[:, None]
        np.fill_diagonal(near, False)  # a listing is not its own peer
        near &= ~np.isnan(ask)[None, :]
        for n, i in enumerate(rows):
            peers = ask[near[n]]
            if len(peers) >= PEER_ASK_MIN and ask[n] < PEER_ASK_RATIO * np.median(peers):
                out.iloc[i] = True
    return out


def listing_checks(li: pd.DataFrame, deals: pd.DataFrame, index: pd.DataFrame, today: date,
                   subsidised=None) -> pd.DataFrame:
    """`exclude_reason` (first of CHECK_ORDER that applies, else None) and the information
    flag `below_all_recent` (asking per sq ft under the building's lowest clean deal of the
    last 24 months) for each listing in `li` (needs the valuation columns of `rank`).
    `deals` is `features.deal_features`; `index` the RVD price index."""
    subsidised = subsidised_estates() if subsidised is None else subsidised
    clean = deals[deals["exclude_reason"].isna() & deals["building_id"].notna()
                  & (deals["psf_hkd"] > 0)]
    since = pd.Timestamp(today) - pd.DateOffset(months=COMPS_MONTHS)
    recent = clean[clean["agreement_date"] > since]

    # asking vs the building's index-adjusted median (all years of clean deals)
    psf = clean["psf_hkd"].astype(float)
    ask = li["asking_psf"].astype(float)
    if not index.empty:
        psf = psf / pd.Series(features.deflator(index, clean["rvd_class"],
                                                clean["agreement_date"]), index=clean.index)
        ask = ask / features.deflator(index, li["rvd_class"],
                                      pd.Series([pd.Timestamp(today)] * len(li)))
    median = psf.groupby(clean["building_id"]).median()
    price_ratio = ask / li["building_id"].map(median)

    # stop-gap for subsidised courts missing from the names file: a 苑/邨 whose 24-month
    # median is under 0.7x its district's
    est = recent.groupby("estate_key")["psf_hkd"].agg(["median", "size"])
    dist = recent.groupby("district")["psf_hkd"].median()
    est_district = recent.groupby("estate_key")["district"].first().reindex(est.index)
    est_ratio = est["median"] / est_district.map(dist)
    cheap_estate = est_ratio[(est["size"] >= SUBSIDISED_MIN_DEALS)
                             & (est_ratio < SUBSIDISED_RATIO)].index
    name = li["estate_name"].fillna("")

    conditions = {
        "subsidised": _is_subsidised(li, subsidised),
        "check_price": (price_ratio < PRICE_RATIO[0]) | (price_ratio > PRICE_RATIO[1]),
        "check_model": (li["comps_24m"] >= MODEL_MIN_COMPS)
                       & (li["fair_psf"] > MODEL_RATIO * li["comps_median_psf"]),
        "check_subsidised": name.str.endswith(("苑", "邨")) & li["estate_key"].isin(cheap_estate),
        "check_peer_ask": _peer_ask_low(li),
    }
    area = _area_reasons(li, deals)
    for k in ("check_area", "check_combined", "check_area_basis"):
        conditions[k] = area.eq(k)
    reason = pd.Series([None] * len(li), index=li.index, dtype=object)
    for k in reversed(CHECK_ORDER):
        reason = reason.mask(conditions[k].fillna(False).astype(bool), k)

    lowest = recent.groupby("building_id")["psf_hkd"].agg(["min", "size"])
    lowest = lowest["min"].where(lowest["size"] >= BELOW_ALL_MIN_COMPS)
    below = li["asking_psf"] < li["building_id"].map(lowest)
    return pd.DataFrame({"exclude_reason": reason, "below_all_recent": below.fillna(False)})


def _blank(s: pd.Series) -> np.ndarray:
    return (s.isna() | s.isin(["", "unknown"])).to_numpy()


def _collapse_same_flat(li: pd.DataFrame) -> pd.DataFrame:
    """Same building, area and price within 2%, floor band and flat equal or blank on either
    side (one site may omit them): one flat listed twice, on one site or several. Keeps the
    first row in sort order (clean rows first, then lowest asking price); the others go to
    `also_listed`. Greedy against the kept row, so a chain of near-equal prices can't drift."""
    li = li.sort_values(["_excluded", "asking_hkd", "key"])
    drop = np.zeros(len(li), dtype=bool)
    extra: dict[int, list[str]] = {}
    area = li["area_sqft"].to_numpy(dtype=float)
    price = li["asking_hkd"].to_numpy(dtype=float)
    keys = li["key"].to_numpy()
    flat, flat_blank = li["flat"].to_numpy(dtype=object), _blank(li["flat"])
    band, band_blank = li["floor_band"].to_numpy(dtype=object), _blank(li["floor_band"])
    also = li["also_listed"].to_numpy(dtype=object)
    for rows in li.groupby("building_id", sort=False).indices.values():
        for n, i in enumerate(rows):
            if drop[i]:
                continue
            later = rows[n + 1:]
            later = later[~drop[later]]
            blank_flat = flat_blank[later] | flat_blank[i]
            near = (np.abs(area[later] - area[i]) <= DUP_TOLERANCE * area[i]) \
                & (np.abs(price[later] - price[i]) <= DUP_TOLERANCE * price[i])
            # a flat missing on either side: nothing says the two are the same flat, so the
            # floor band must be given and equal, and area and price within 1% (J3)
            close = (np.abs(area[later] - area[i]) <= BLANK_FLAT_TOLERANCE * area[i]) \
                & (np.abs(price[later] - price[i]) <= BLANK_FLAT_TOLERANCE * price[i]) \
                & ~band_blank[later] & ~band_blank[i] & (band[later] == band[i])
            hit = near & np.where(blank_flat, close, flat[later] == flat[i]) \
                & (band_blank[later] | band_blank[i] | (band[later] == band[i]))
            blank_taken = 0
            for j, was_blank in zip(later[hit], blank_flat[hit], strict=True):
                if was_blank:
                    if blank_taken >= MAX_BLANK_FLAT_MERGES:
                        continue
                    blank_taken += 1
                drop[j] = True
                extra.setdefault(i, []).append(keys[j])
                # the folded row's own cross-site keys move to the kept row
                if isinstance(also[j], str):
                    extra[i] += [k for k in also[j].split(", ") if k]
    kept = li[~drop].copy()
    for i, more in extra.items():
        row = li.index[i]
        old = kept.at[row, "also_listed"]
        mine = [k for k in ([old] if isinstance(old, str) else []) + more
                for k in (k.split(", ") if ", " in k else [k])]
        kept.at[row, "also_listed"] = ", ".join(
            k for k in dict.fromkeys(mine) if k != keys[i])
    return kept


def _display_values(row) -> dict:
    vals = {f: row.get(f) for f in model.FEATURES}
    vals["building_id"] = row.get("building_name") or row.get("building_id")
    vals["estate_key"] = row.get("estate_name") or row.get("estate_key")
    return vals


def _floor_text(row) -> str:
    if row["floor_basis"] == "exact":
        return f"{row['floor_num']:.0f}/F"
    if row["floor_basis"] == "band":
        return f"{row['floor_band']} band" if row["floor_band"] != "unknown" else "not given"
    return "not given"


def rank(raw: dict, m: model.FairPrice, today: date | None = None) -> Ranked:
    today = today or date.today()
    deals = features.deal_features(raw)
    listings = features.listing_features(raw, today)
    not_valued = listings["value_note"].value_counts().to_dict()
    li = listings[listings["value_note"].isna()].copy()
    # value at the last well-populated month (model.valuation_month), not the current one
    li["month_idx"] = m.meta.get("valuation_month_idx", li["month_idx"])

    # asking-vs-deal gap per district, from every sale listing seen (active or delisted)
    b = features.building_table(raw)
    sale = raw["listings"][raw["listings"]["kind"] == "sale"].join(b[["district"]],
                                                                   on="building_id")
    gaps = gap.district_gaps(gap.pairs(sale, deals))

    # value: index-adjusted prediction, moved by any index change since the model's
    # reference month (zero unless RVD has published a newer month since training)
    adj = model.predict_adj(m, li)
    ref = pd.Timestamp(m.meta["index_reference_month"])
    when = pd.Series([pd.Timestamp(today)] * len(li))
    logd = (np.log(model.deflator(raw["index"], li["rvd_class"], when))
            - np.log(model.deflator(raw["index"], li["rvd_class"], pd.Series([ref] * len(li)))))
    li["fair_psf"] = np.exp(adj + logd)
    li["fair_price_hkd"] = li["fair_psf"] * li["area_sqft"]
    li["group"] = model.group_of(li)
    half = li["group"].map(lambda g: model.q_for(m.meta["q"], g))
    li["fair_low_hkd"] = li["fair_price_hkd"] * np.exp(-half)
    li["fair_high_hkd"] = li["fair_price_hkd"] * np.exp(half)

    gb = [gap.gap_for(gaps, d) for d in li["district"]]
    li["gap_pct"] = [g for g, _ in gb]
    li["gap_basis"] = [basis for _, basis in gb]
    li["asking_hkd"] = li["price_hkd"]
    li["asking_psf"] = li["price_hkd"] / li["area_sqft"]
    li["expected_deal_hkd"] = li["asking_hkd"] * (1 - li["gap_pct"])
    li["discount_pct"] = discount(li["asking_hkd"], li["fair_price_hkd"], li["gap_pct"])

    comps = _comps(deals, today)
    li["comps_24m"] = li["building_id"].map(comps["comps_24m"]).fillna(0).astype(int)
    li["comps_median_psf"] = li["building_id"].map(comps["comps_median_psf"])
    li["confidence"] = np.where(li["comps_24m"] < LOW_CONFIDENCE_COMPS, "low", "normal")

    shap = model.predict_shap(m, li)
    shap.index = li.index
    li = pd.concat([li, shap], axis=1)
    reasons = [model.reasons(li.loc[i, shap.columns], _display_values(li.loc[i]))
               for i in li.index]
    for k in range(3):
        li[f"reason_{k + 1}"] = [r[k] if len(r) > k else None for r in reasons]
    li["floor"] = li.apply(_floor_text, axis=1)

    checks = listing_checks(li, deals, raw["index"], today)
    li["exclude_reason"] = checks["exclude_reason"]
    li["below_all_recent"] = checks["below_all_recent"]
    li["_excluded"] = li["exclude_reason"].notna()

    # one row per cross-site duplicate group (same dup_group), at the lowest asking price,
    # clean rows first
    li["key"] = li["source"] + ":" + li["listing_id"]
    li["dup_group"] = li["dup_group"].fillna(li["key"])
    li = li.sort_values(["dup_group", "_excluded", "asking_hkd", "key"])
    others = li.groupby("dup_group")["key"].agg(list)
    li = li.drop_duplicates("dup_group", keep="first").copy()
    li["also_listed"] = [", ".join(k for k in others[g] if k != key) or None
                         for g, key in zip(li["dup_group"], li["key"], strict=True)]
    # then the same flat listed twice (any site)
    li = _collapse_same_flat(li)

    # rows with a check reason go after all the others, each part best discount first
    li = li.sort_values(["_excluded", "discount_pct"], ascending=[True, False]).drop(
        columns="_excluded").reset_index(drop=True)
    li.insert(0, "rank", np.arange(1, len(li) + 1))
    counts = {"active_sale_listings": len(listings), "ranked": len(li),
              "duplicates_merged": int(listings["value_note"].isna().sum()) - len(li),
              "excluded": {str(k): int(v) for k, v in
                           li["exclude_reason"].value_counts().items()},
              "not_valued": {str(k): int(v) for k, v in not_valued.items()},
              "gap_pairs": int(gaps["n_pairs"].sum()) if not gaps.empty else 0}
    return Ranked(li, gaps, counts)


CSV_COLUMNS = [
    "rank", "discount_pct", "source", "listing_id", "url", "estate_name", "building_name",
    "district", "floor", "flat", "area_sqft", "rooms", "facing", "age_years", "asking_hkd",
    "asking_psf", "gap_pct", "gap_basis", "expected_deal_hkd", "fair_price_hkd", "fair_low_hkd",
    "fair_high_hkd", "fair_psf", "confidence", "comps_24m", "comps_median_psf", "flags",
    "reason_1", "reason_2", "reason_3", "group", "floor_basis", "first_seen", "also_listed",
    "exclude_reason", "below_all_recent",
]


SHORTLIST_SIZE = 50


def shortlist(table: pd.DataFrame, n: int = SHORTLIST_SIZE) -> tuple[pd.DataFrame, pd.Series]:
    """The top `n` rows with no exclusion reason and normal confidence (table order = best
    discount first), plus how many rows each filter left out (by exclude_reason, and
    'low confidence' for clean rows with few comparable deals)."""
    clean = table["exclude_reason"].isna()
    normal = table["confidence"] == "normal"
    left_out = table.loc[~clean, "exclude_reason"].value_counts()
    left_out.loc["low confidence"] = int((clean & ~normal).sum())
    return table[clean & normal].head(n), left_out[left_out > 0]


def _hk(x) -> str:
    return f"{x / 1e6:,.2f}m"


def shortlist_markdown(table: pd.DataFrame, today: date, n: int = SHORTLIST_SIZE) -> str:
    top, left_out = shortlist(table, n)
    lines = [
        f"# Shortlist {today:%Y-%m-%d}", "",
        f"The top {n} listings with the biggest discount to the model's fair price, after "
        "leaving out rows with a data check reason (such as a doubtful area or price, or a "
        "subsidised flat) and rows with low confidence (fewer than "
        f"{LOW_CONFIDENCE_COMPS} comparable deals in the building in 24 months).", "",
        "- **Discount**: how far the expected deal price (asking less the usual "
        "asking-vs-deal gap) sits below fair price.",
        "- **Fair range**: the model's 80% range for the fair price (HK$).",
        "- **Reasons**: the three features that moved the fair price most.",
        "- **Below every recent sale** (a flag): asking per sq ft is under every recent clean "
        "deal in the building (information only).",
        "- **Also listed**: the same flat on other sites or listings.", "",
    ]
    if len(left_out):
        why = ", ".join(f"{k}: {v}" for k, v in left_out.items())
        lines += [f"Left out: {int(left_out.sum())} rows ({why}).", ""]
    for i, row in enumerate(top.itertuples(index=False), 1):
        flat = " ".join(str(x) for x in (row.floor, row.flat) if isinstance(x, str) and x
                        and x != "not given") or "floor and flat not given"
        reasons = "; ".join(x for x in (row.reason_1, row.reason_2, row.reason_3)
                            if isinstance(x, str))
        flag_list = [row.flags] if isinstance(row.flags, str) and row.flags else []
        if row.below_all_recent:
            flag_list.append("below every recent sale")
        flags = "; ".join(flag_list) or "none"
        also = row.also_listed if isinstance(row.also_listed, str) and row.also_listed else "no"
        lines += [
            f"## {i}. {row.estate_name or '(no estate)'} - {row.building_name or ''}".rstrip(" -"),
            f"- Flat: {flat}, {row.area_sqft:,.0f} sq ft",
            f"- Asking HK${_hk(row.asking_hkd)}; fair HK${_hk(row.fair_low_hkd)} to "
            f"{_hk(row.fair_high_hkd)} (mid {_hk(row.fair_price_hkd)})",
            f"- Discount: {row.discount_pct * 100:.1f}%",
            f"- Reasons: {reasons or 'none'}",
            f"- Flags: {flags}; also listed: {also}",
            f"- {row.url}", "",
        ]
    if top.empty:
        lines += ["No listing passes the filters.", ""]
    return "\n".join(lines)


def write(r: Ranked, today: date | None = None) -> dict[str, Path]:
    today = today or date.today()
    reports = get_data_dir() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    csv_path = reports / f"ranked_listings_{today:%Y%m%d}.csv"
    out = r.table[CSV_COLUMNS].copy()
    for c in ("asking_hkd", "expected_deal_hkd", "fair_price_hkd", "fair_low_hkd",
              "fair_high_hkd"):
        out[c] = out[c].round(-3)
    for c in ("asking_psf", "fair_psf", "comps_median_psf"):
        out[c] = out[c].round(0)
    for c in ("discount_pct", "gap_pct"):
        out[c] = out[c].round(4)
    out.to_csv(csv_path, index=False, encoding="utf-8-sig")
    gap_path = reports / f"asking_gap_{today:%Y%m%d}.csv"
    r.gaps.to_csv(gap_path, index=False, encoding="utf-8-sig")
    mdir = model.model_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    parquet_path = mdir / "ranked_latest.parquet"
    r.table.assign(ranked_on=str(today)).to_parquet(parquet_path, index=False)
    short_path = reports / f"shortlist_{today:%Y%m%d}.md"
    short_path.write_text(shortlist_markdown(r.table, today), encoding="utf-8")
    return {"csv": csv_path, "gaps": gap_path, "parquet": parquet_path,
            "shortlist": short_path}
