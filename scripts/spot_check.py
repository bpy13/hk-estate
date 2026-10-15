"""Spot-check (Task 1.5): 50 random stored pilot deals against the live 28Hse site.

    conda run -n hk-estate python scripts/spot_check.py [--seed N] [--per-estate 10]

Draws 10 random clean Land Registry deals per pilot estate group from the `txn` view,
fetches each flat's page fresh from 28Hse (a new, empty cache folder forces a live request;
robots.txt and the rate limit still apply) and compares floor, flat, saleable area, date and
price. Writes data/reports/spot_check_<date>.md.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from hkestate import db, spotcheck
from hkestate.config import get_data_dir
from hkestate.fetch import Fetcher
from hkestate.sources import hse28


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=float, default=0.42)
    p.add_argument("--per-estate", type=int, default=10)
    args = p.parse_args()

    with db.session() as con:
        con.execute("SELECT setseed(?)", [args.seed])
        rows = con.execute(
            """
            SELECT * FROM (
                SELECT e.group_name, t.source_id, t.floor_label, t.flat, t.deal_date,
                       t.price_hkd, t.saleable_sqft, u.src_url,
                       row_number() OVER (PARTITION BY e.group_name ORDER BY random()) AS rn
                FROM txn t
                JOIN estate e USING (estate_id)
                JOIN unit u ON u.unit_id = t.unit_id
                WHERE t.source = 'hse28' AND t.deal_type = 'registry' AND u.src_url IS NOT NULL
            ) WHERE rn <= ? ORDER BY group_name, rn
            """,
            [args.per_estate],
        ).fetchall()
    cols = ["group", "source_id", "floor_label", "flat", "deal_date", "price_hkd",
            "saleable_sqft", "url"]
    sample = [dict(zip(cols, r[:-1], strict=True)) for r in rows]

    stamp = datetime.now()
    fresh_cache = get_data_dir() / "spotcheck_cache" / f"{stamp:%Y%m%d_%H%M%S}"
    results = []
    with Fetcher(cache_dir=fresh_cache) as f:
        for r in sample:
            try:
                page = hse28.parse_unit_page(f.get(r["url"], "transaction"))
                diffs = spotcheck.compare(r, page)
            except Exception as e:  # report, don't stop
                diffs = [f"fetch: {type(e).__name__} {e}"]
            results.append((r, diffs))
            print(("OK  " if not diffs else "DIFF"), r["group"], r["source_id"], diffs)

    ok = sum(1 for _, d in results if not d)
    lines = [
        "# Spot-check against the live 28Hse site",
        "",
        f"Run {stamp:%Y-%m-%d %H:%M}, seed {args.seed}. {len(results)} random clean Land "
        f"Registry deals ({args.per_estate} per pilot estate) compared with each flat's live "
        "page on floor, flat, saleable area, date and price.",
        "",
        f"**Result: {ok}/{len(results)} match.**",
        "",
        "| Estate | Deal | Floor | Flat | Date | Price HK$ | Saleable sq ft | Result |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r, d in results:
        lines.append(
            f"| {r['group']} | {r['source_id']} | {r['floor_label']} | {r['flat']} | "
            f"{r['deal_date']} | {r['price_hkd']:,} | {r['saleable_sqft']} | "
            f"{'match' if not d else '; '.join(d)} |"
        )
    out = get_data_dir() / "reports" / f"spot_check_{stamp:%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{ok}/{len(results)} match; report {out}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
