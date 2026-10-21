"""HK Estate dashboard: ranked listings, filters, a listing detail page, the model report.

    conda run -n hk-estate streamlit run app/dashboard.py

Reads what `hkestate train` and `hkestate rank` write to data/model/ (no long database
access; the run-status banner reads `run_log` read-only). Pages can be opened directly:
?page=ranked, ?page=detail&listing=<source>:<id>, ?page=model, ?page=growth&setting=<name>
(the growth page reads the CSVs under data/reports/growth/, nothing from the database).
"""

from __future__ import annotations

import math

import altair as alt
import pandas as pd
import streamlit as st

from hkestate import dashboard_data as dd
from hkestate import model

st.set_page_config(page_title="HK Estate", layout="wide")

# Reference palette (dataviz skill): blue raises the value, red lowers it; text labels carry
# the sign too, so colour is never the only cue.
RAISES, LOWERS, SERIES = "#2a78d6", "#e34948", "#2a78d6"
PAGES = {"ranked": "Ranked listings", "detail": "Listing detail", "model": "Model",
         "growth": "Growth study"}


@st.cache_data(ttl=300)
def load():
    return dd.ranked(), dd.deals(), dd.meta()


def hkd(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    return f"HK${x / 1e6:,.2f}m" if x >= 1e6 else f"HK${x:,.0f}"


def banner() -> None:
    s = dd.run_status()
    if s.failed_last:
        msg = str(s.failed_last.get("message") or "")[:300]
        st.error(f"**The last run failed:** `{s.failed_last['command']}` started "
                 f"{s.failed_last['started_at']}. {msg}  \nSee data/logs/ for the log.")
    if not s.failing.empty:
        cmds = ", ".join(f"`{c}`" for c in s.failing["command"])
        st.warning(f"Newest run failed for: {cmds} (run `hkestate status` for details).")
    if s.source != "database":
        st.caption(f"Run status from the {s.source}.")


def nav() -> str:
    keys = list(PAGES)
    current = st.query_params.get("page", "ranked")
    current = current if current in PAGES else "ranked"
    choice = st.sidebar.radio("Page", keys, index=keys.index(current),
                              format_func=PAGES.get)
    if choice != current:
        st.query_params["page"] = choice
    return choice


def flag_text(flags) -> str:
    return ", ".join(dd.FLAG_LABELS.get(f, f) for f in flags) or "–"


# ---- pages -----------------------------------------------------------------------------------


def page_ranked(df: pd.DataFrame, meta: dict) -> None:
    st.title("Listings ranked by discount to fair price")
    if df.empty:
        st.info("No ranking yet: run `hkestate train`, then `hkestate rank`.")
        return
    sb = st.sidebar
    sb.header("Filters")
    districts = sb.multiselect("District", sorted(df["district"].dropna().unique()))
    pmax = float(math.ceil(df["asking_hkd"].max() / 1e6))
    price = sb.slider("Asking price (HK$m)", 0.0, pmax, (0.0, pmax), step=0.5)
    smax = float(math.ceil(df["area_sqft"].max() / 50) * 50)
    size = sb.slider("Saleable area (sq ft)", 0.0, smax, (0.0, smax), step=50.0)
    all_flags = sorted({f for fl in df["flag_list"] for f in fl})
    need = sb.multiselect("Has any of these flags", all_flags, format_func=dd.FLAG_LABELS.get)
    hide = sb.multiselect("Hide listings flagged", all_flags, format_func=dd.FLAG_LABELS.get)
    conf = sb.multiselect("Confidence", ["normal", "low"])
    out = dd.filter_ranked(df, districts=districts, price_m=price, size=size,
                           need_flags=need, hide_flags=hide, confidence=conf)

    assumed = (df["gap_basis"] == "assumed").all()
    st.caption(
        f"Ranked on {df['ranked_on'].iloc[0]}; model trained {meta.get('trained_at', '?')[:10]}. "
        f"Showing {len(out):,} of {len(df):,}. Discount = how far the expected deal price "
        "(asking less the usual asking-vs-deal gap"
        + (", **5% assumed** everywhere until enough listing-deal pairs exist" if assumed else "")
        + ") sits below the model's fair price. Confidence and flags don't change the order.")
    table = out[["rank", "discount_pct", "estate_name", "building_name", "district", "floor",
                 "flat", "area_sqft", "asking_hkd", "fair_price_hkd", "fair_low_hkd",
                 "fair_high_hkd", "confidence", "comps_24m", "flags", "reason_1", "source",
                 "url", "key"]].copy()
    table["discount_pct"] = table["discount_pct"] * 100
    table["flags"] = out["flag_list"].map(flag_text)
    event = st.dataframe(
        table.drop(columns=["key"]), hide_index=True, use_container_width=True, height=560,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "rank": st.column_config.NumberColumn("#", width="small"),
            "discount_pct": st.column_config.NumberColumn("Discount", format="%.1f%%"),
            "estate_name": "Estate", "building_name": "Building", "district": "District",
            "floor": "Floor", "flat": "Flat",
            "area_sqft": st.column_config.NumberColumn("Sq ft", format="%d"),
            "asking_hkd": st.column_config.NumberColumn("Asking HK$", format="%,.0f"),
            "fair_price_hkd": st.column_config.NumberColumn("Fair HK$", format="%,.0f"),
            "fair_low_hkd": st.column_config.NumberColumn("80% low", format="%,.0f"),
            "fair_high_hkd": st.column_config.NumberColumn("80% high", format="%,.0f"),
            "confidence": "Confidence",
            "comps_24m": st.column_config.NumberColumn("Comps 24m", format="%d"),
            "flags": "Flags", "reason_1": "Main reason", "source": "Site",
            "url": st.column_config.LinkColumn("Listing", display_text="open"),
        })
    rows = event.selection.rows if event and event.selection else []
    if rows:
        key = table.iloc[rows[0]]["key"]
        if st.button(f"Open detail for #{int(table.iloc[rows[0]]['rank'])}", type="primary"):
            st.query_params["page"] = "detail"
            st.query_params["listing"] = key
            st.rerun()
    else:
        st.caption("Select a row to open its detail page.")
    st.download_button("Download this list (CSV)", out.drop(columns=["flag_list"]).to_csv(
        index=False).encode("utf-8-sig"), "ranked_listings_filtered.csv", "text/csv")


def shap_chart(row: pd.Series) -> alt.Chart:
    rows = []
    for f in model.FEATURES:
        v = float(row.get(f"shap_{f}", 0) or 0)
        rows.append({"feature": model.LABELS.get(f, f), "effect": math.exp(v) - 1})
    d = pd.DataFrame(rows)
    d = d.reindex(d["effect"].abs().sort_values(ascending=False).index).head(10)
    d["direction"] = ["raises value" if e >= 0 else "lowers value" for e in d["effect"]]
    d["label"] = [f"{e:+.1%}" for e in d["effect"]]
    base = alt.Chart(d).encode(y=alt.Y("feature:N", sort=None, title=None))
    # room for the value labels beyond the longest bar on each side
    lo, hi = min(d["effect"].min() * 1.6, -0.05), max(d["effect"].max() * 1.25, 0.05)
    bars = base.mark_bar(cornerRadiusEnd=4, height=14).encode(
        x=alt.X("effect:Q", axis=alt.Axis(format="+%", title="Effect on fair price"),
                scale=alt.Scale(domain=[lo, hi])),
        color=alt.Color("direction:N", title=None,
                        scale=alt.Scale(domain=["raises value", "lowers value"],
                                        range=[RAISES, LOWERS]),
                        legend=alt.Legend(orient="top")),
        tooltip=[alt.Tooltip("feature:N", title="Input"), alt.Tooltip("label:N", title="Effect")])
    # value labels at the bar ends, outside the bar, in text ink (not the series colour)
    pos = base.transform_filter("datum.effect >= 0").mark_text(
        align="left", dx=4, color="#52514e").encode(x="effect:Q", text="label:N")
    neg = base.transform_filter("datum.effect < 0").mark_text(
        align="right", dx=-4, color="#52514e").encode(x="effect:Q", text="label:N")
    return (bars + pos + neg).properties(height=34 * len(d))


def history_chart(hist: pd.DataFrame, asking: float, one_unit: bool) -> alt.Chart:
    """Deals over time; joined by a line only when they are the same flat (different
    floors joined up would suggest a trend that isn't there)."""
    h = hist.assign(price_m=hist["price_hkd"] / 1e6,
                    when=pd.to_datetime(hist["agreement_date"]))
    chart = alt.Chart(h)
    mark = (chart.mark_line(point=alt.OverlayMarkDef(size=64, filled=True), strokeWidth=2,
                            color=SERIES) if one_unit
            else chart.mark_circle(size=64, color=SERIES, opacity=0.85))
    pts = mark.encode(
        x=alt.X("when:T", title="Agreement date"),
        y=alt.Y("price_m:Q", title="Price (HK$m)"),
        tooltip=[alt.Tooltip("when:T", title="Date"), alt.Tooltip("floor_label:N", title="Floor"),
                 alt.Tooltip("price_m:Q", title="HK$m", format=",.2f"),
                 alt.Tooltip("area_sqft:Q", title="Sq ft", format=",.0f")])
    ask = alt.Chart(pd.DataFrame({"y": [asking / 1e6], "t": ["asking now"]})).mark_rule(
        strokeDash=[4, 4], color="#52514e").encode(y="y:Q", tooltip=[alt.Tooltip("t:N")])
    return (pts + ask).properties(height=280)


def page_detail(df: pd.DataFrame, deals: pd.DataFrame) -> None:
    st.title("Listing detail")
    if df.empty:
        st.info("No ranking yet: run `hkestate train`, then `hkestate rank`.")
        return
    keys = df["key"].tolist()
    labels = {k: f"#{r} {e} {b} {fl} – {hkd(a)}" for k, r, e, b, fl, a in
              zip(df["key"], df["rank"], df["estate_name"], df["building_name"], df["floor"],
                  df["asking_hkd"], strict=True)}
    wanted = st.query_params.get("listing")
    key = st.selectbox("Listing", keys, index=keys.index(wanted) if wanted in keys else 0,
                       format_func=labels.get)
    if key != wanted:
        st.query_params["listing"] = key
    row = df[df["key"] == key].iloc[0]

    st.subheader(f"{row['estate_name']} · {row['building_name']} · {row['floor']}"
                 + (f" · flat {row['flat']}" if row.get("flat") else ""))
    c = st.columns(5)
    c[0].metric("Asking", hkd(row["asking_hkd"]))
    c[1].metric("Fair price", hkd(row["fair_price_hkd"]))
    c[2].metric("80% range (HK$m)",
                f"{row['fair_low_hkd'] / 1e6:.2f}–{row['fair_high_hkd'] / 1e6:.2f}")
    c[3].metric("Discount", f"{row['discount_pct']:+.1%}",
                help="1 - expected deal price / fair price; positive = below fair price")
    c[4].metric("Confidence", row["confidence"],
                help=f"{row['comps_24m']} same-building deals in the last 24 months "
                     "(low if under 10)")
    # "HK\$": a bare $ starts a formula in Streamlit markdown
    st.markdown(
        f"{row['area_sqft']:.0f} sq ft saleable · {row['district'] or 'district unknown'} · "
        f"asking HK\\${row['asking_psf']:,.0f}/sq ft vs fair HK\\${row['fair_psf']:,.0f}/sq ft · "
        f"usual gap {row['gap_pct']:.0%} ({row['gap_basis']}) · flags: "
        f"{flag_text(row['flag_list'])}")
    if isinstance(row.get("url"), str) and row["url"]:
        st.link_button(f"Open the listing on {row['source']}", row["url"])
    if isinstance(row.get("also_listed"), str) and row["also_listed"]:
        st.caption(f"Also listed as: {row['also_listed']}")

    left, right = st.columns(2)
    with left:
        st.markdown("**Why this fair price** (SHAP: each input's effect vs the average flat)")
        st.altair_chart(shap_chart(row), use_container_width=True)
    with right:
        hist, what = dd.unit_history(deals, row)
        st.markdown(f"**Price history:** recorded deals on {what}")
        if hist.empty:
            st.caption("No recorded deals on this flat.")
        else:
            st.altair_chart(history_chart(hist, row["asking_hkd"], what == "this flat"),
                            use_container_width=True)
            st.caption("Dashed line: today's asking price.")

    comps = dd.comparables(deals, row["building_id"], row["ranked_on"])
    st.markdown(f"**Comparables:** {len(comps)} deals in {row['building_name']} in the 24 months "
                "before the ranking")
    if not comps.empty:
        show = comps[["agreement_date", "floor_label", "flat", "area_sqft", "price_hkd",
                      "psf_hkd", "source"]].copy()
        show["agreement_date"] = pd.to_datetime(show["agreement_date"]).dt.date
        st.dataframe(show, hide_index=True, use_container_width=True, column_config={
            "agreement_date": "Date", "floor_label": "Floor", "flat": "Flat",
            "area_sqft": st.column_config.NumberColumn("Sq ft", format="%d"),
            "price_hkd": st.column_config.NumberColumn("Price HK$", format="%,.0f"),
            "psf_hkd": st.column_config.NumberColumn("HK$/sq ft", format="%,.0f"),
            "source": "Source"})


def page_model() -> None:
    text = dd.eval_report()
    if text is None:
        st.info("No model report yet: run `hkestate train`.")
    else:
        st.markdown(text)


@st.cache_data(ttl=300)
def load_growth(folder_mtime: float):
    """Cached on the folder's modified time so a rerun of `hkestate report` shows up."""
    return dd.load_growth()


def _setting_name(s: dd.GrowthSetting) -> str:
    tag = f"Sensitivity ({s.min_hold_years:g}-year minimum hold): " if s.sensitivity else ""
    return f"{tag}{s.label}"


def effect_chart(rows: pd.DataFrame, order: list[str]) -> alt.Chart:
    """One attribute across settings: the effect with a 95% interval; the zero line marks
    'no effect'. Blue = raises growth, red = lowers; the sign is also in the label."""
    d = rows.assign(direction=["raises growth" if e >= 0 else "lowers growth"
                               for e in rows["effect"]],
                    text=[f"{e:+.2f}" for e in rows["effect"]])
    y = alt.Y("setting:N", sort=order, title=None)
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color="#52514e").encode(x="x:Q")
    bars = alt.Chart(d).mark_rule(strokeWidth=2).encode(
        y=y, x=alt.X("low:Q", title="Effect on annual growth (percentage points a year)"),
        x2="high:Q", color=alt.Color(
            "direction:N", title=None, scale=alt.Scale(
                domain=["raises growth", "lowers growth"], range=[RAISES, LOWERS]),
            legend=alt.Legend(orient="top")))
    dots = alt.Chart(d).mark_point(filled=True, size=90).encode(
        y=y, x="effect:Q", color=alt.Color("direction:N", legend=None, scale=alt.Scale(
            domain=["raises growth", "lowers growth"], range=[RAISES, LOWERS])),
        tooltip=[alt.Tooltip("setting:N"), alt.Tooltip("text:N", title="Effect (pp/yr)"),
                 alt.Tooltip("verdict:N", title="Verdict"),
                 alt.Tooltip("n:Q", title="Pairs", format=",")])
    labels = alt.Chart(d).mark_text(dy=-12, color="#52514e").encode(
        y=y, x="effect:Q", text="text:N")
    return (zero + bars + dots + labels).properties(height=max(60, 34 * len(d)))


def index_chart(idx: pd.DataFrame, shown: list[str]) -> alt.Chart:
    d = idx[idx["District"].isin(shown)]
    return alt.Chart(d).mark_line(strokeWidth=1.8).encode(
        x=alt.X("month:T", title=None),
        y=alt.Y("level:Q", title="Price index (first month = 100)",
                scale=alt.Scale(type="log")),
        color=alt.Color("District:N", title=None),
        tooltip=[alt.Tooltip("District:N"), alt.Tooltip("month:T", format="%b %Y"),
                 alt.Tooltip("level:Q", format=",.0f")]).properties(height=340)


def page_growth() -> None:
    st.title("Which flat features go with better price growth?")
    folder = dd.latest_growth_dir()
    study = load_growth(folder.stat().st_mtime if folder else 0.0)
    if study is None or not study.settings:
        st.info("No growth study yet: run `hkestate report --settings standard`.")
        return
    main, sens = study.main, study.sensitivities
    sb = st.sidebar
    sb.header("Growth study")
    names = {s.name: _setting_name(s) for s in study.settings}
    wanted = st.query_params.get("setting")
    keys = list(names)
    pick = sb.selectbox("Setting", keys, index=keys.index(wanted) if wanted in keys else 0,
                        format_func=names.get)
    if pick != wanted:
        st.query_params["setting"] = pick
    ptype = sb.radio("Pairs", list(dd.PAIRS_TYPES), format_func=dd.PAIRS_TYPES.get,
                     help="Recorded = a flat's two registered sales. Estimated = the "
                          "second sale is a model price for today (a cross-check).")
    setting = study.get(pick)
    st.caption(
        f"Study of {study.date}. Each pair is one flat sold twice; the study asks which "
        "features go with faster annual price growth between the two sales, after allowing "
        "for the other features. Numbers are percentage points of growth a year: "
        "+1.00 means 1 point a year faster, which compounds over a holding period.")

    # ---- verdicts across settings (main settings only) --------------------------------
    st.subheader("Do the findings hold across market phases?")
    vt = dd.verdict_table(main, ptype)
    if vt.empty:
        st.info("No attribute results for this pair type.")
    else:
        st.dataframe(vt.set_index("Attribute"), use_container_width=True)
        st.caption(
            "**robust** = clearly different from zero and the same direction in both halves "
            "of the data (by time and by district). **significant only** = clear but not "
            "steady across halves. **no effect found** = indistinguishable from zero. "
            "**too few pairs** = the period holds too few flats sold twice for a verdict. "
            "The number in brackets is the effect in percentage points a year.")
    if sens:
        with st.expander("Sensitivity checks (shorter minimum holding period)"):
            st.caption(
                "These re-run the recent phases allowing pairs held only "
                f"{', '.join(sorted({f'{s.min_hold_years:g}' for s in sens if s.min_hold_years}))}"
                " year(s) instead of 3, because those phases hold almost no 3-year pairs. "
                "Short holds are dominated by trading costs and noise: read them as hints, "
                "not as findings, and they never feed the table above.")
            st.dataframe(dd.verdict_table(sens, ptype).set_index("Attribute"),
                         use_container_width=True)
            for s in sens:
                if s.note:
                    st.caption(f"{s.label}: {s.note}")

    # ---- one attribute across settings ------------------------------------------------
    st.subheader("One attribute across settings")
    ev = dd.effect_rows(study.settings, ptype)
    if ev.empty:
        st.info("No effects to chart.")
    else:
        labels = ev.drop_duplicates("attribute").set_index("attribute")["label"]
        attr = st.selectbox("Attribute", list(labels.index), format_func=labels.get)
        st.markdown(f"**{labels[attr]}**: {dd.attribute_sentence(attr, main, ptype)}")
        one = ev[ev["attribute"] == attr].copy()
        one["setting"] = one["setting"].map(names)
        st.altair_chart(effect_chart(one, [names[s.name] for s in study.settings]),
                        use_container_width=True)
        st.caption("Dot = estimated effect; line = 95% range. A line crossing zero means the "
                   "data can't tell the effect from nothing. Sensitivity settings are "
                   "labelled in the axis.")

    # ---- districts for the selected setting -------------------------------------------
    st.subheader(f"Districts: {setting.label}")
    if setting.sensitivity:
        st.warning("This is a sensitivity setting (short minimum hold): hints only.")
    if setting.note:
        st.info(setting.note)
    dt = dd.district_table(setting, ptype)
    if dt.empty:
        st.caption("No district results for this setting.")
        return
    st.dataframe(
        dt[["District", "pairs", "median_years", "growth", "net_growth", "rental_yield",
            "total_return", "drawdown", "peak", "trough"]],
        hide_index=True, use_container_width=True, column_config={
            "pairs": st.column_config.NumberColumn("Pairs", format="%d"),
            "median_years": st.column_config.NumberColumn("Median hold (yrs)", format="%.1f"),
            "growth": st.column_config.NumberColumn("Growth a year", format="percent"),
            "net_growth": st.column_config.NumberColumn("After costs", format="percent"),
            "rental_yield": st.column_config.NumberColumn("Rental yield", format="percent"),
            "total_return": st.column_config.NumberColumn("Total return a year",
                                                         format="percent"),
            "drawdown": st.column_config.NumberColumn("Worst fall", format="percent"),
            "peak": "Fall from", "trough": "Fall to"})
    st.caption("Growth = median annual price growth between a flat's two sales. After costs "
               "= after stamp duty and agent fees. Total return = after-cost growth plus "
               "rental yield. Worst fall = biggest peak-to-trough drop of the district's price "
               "index in this period (blank if under 100 pairs).")
    idx = dd.district_index(setting, ptype)
    if idx.empty:
        st.caption("No price index for this setting.")
        return
    avail = sorted(idx["District"].unique())
    default = [d for d in dt["District"].head(4) if d in avail]
    shown = st.multiselect("Districts on the chart", avail, default=default)
    if shown:
        st.altair_chart(index_chart(idx, shown), use_container_width=True)
        st.caption("Price index built from the same flats sold twice (repeat sales), so it "
                   "tracks like-for-like price moves; the first month of each line is 100. "
                   "Log scale: equal slopes mean equal percentage changes.")


def main() -> None:
    page = nav()
    banner()
    if page == "growth":
        page_growth()
        return
    df, deals, meta = load()
    if page == "ranked":
        page_ranked(df, meta)
    elif page == "detail":
        page_detail(df, deals)
    else:
        page_model()


main()
