"""Listing storage: raw versions, the current listing table, daily snapshots and delisting.

- `store_raw` appends a listing to `listing_raw` only when something changed. A search card
  and a detail page show different fields, so a new reading is first merged onto the newest
  stored version (missing fields keep their stored value; feature tags are combined).
- Free text (`Listing.text_for_flags`) and anything about agents is never stored.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime

from hkestate.db import LISTING_RAW_COLUMNS
from hkestate.listing_types import Listing

# Fields that come from the parsed listing (all columns of listing_raw except bookkeeping).
CONTENT_FIELDS = [
    c for c in LISTING_RAW_COLUMNS if c not in ("scope", "page_type")
]


def _jsonable(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def listing_row(lst: Listing) -> dict:
    """The storable fields of a listing. text_for_flags is deliberately left out."""
    row = {c: getattr(lst, c, None) for c in CONTENT_FIELDS}
    row["features"] = "|".join(sorted(set(lst.features))) if lst.features else None
    row["url"] = lst.url
    row["extra"] = dict(lst.extra) if lst.extra else {}
    return row


def _merge(new: dict, old: dict | None) -> dict:
    if not old:
        return new
    out = dict(new)
    for k, v in new.items():
        if k == "features":
            feats = set(filter(None, (old.get(k) or "").split("|"))) | set(
                filter(None, (v or "").split("|"))
            )
            out[k] = "|".join(sorted(feats)) or None
        elif k == "extra":
            out[k] = {**(old.get("extra") or {}), **(v or {})}
        elif v is None or v == "":
            out[k] = old.get(k)
    return out


def _hash(row: dict) -> str:
    content = {k: _jsonable(row.get(k)) for k in CONTENT_FIELDS + ["extra"]}
    blob = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def latest_rows(con, source: str, ids: list[str]) -> dict[str, dict]:
    """Newest stored version of each listing id (as a field dict)."""
    if not ids:
        return {}
    cols = ", ".join(CONTENT_FIELDS)
    rows = con.execute(
        f"""
        SELECT listing_id, url, raw_json, {cols} FROM (
            SELECT *, row_number() OVER (PARTITION BY listing_id ORDER BY fetched_at DESC) rn
            FROM listing_raw WHERE source = ? AND listing_id IN (SELECT unnest(?)))
        WHERE rn = 1
        """,
        [source, ids],
    ).fetchall()
    out = {}
    for r in rows:
        d = dict(zip(["url", "raw_json", *CONTENT_FIELDS], r[1:], strict=True))
        raw = json.loads(d.pop("raw_json") or "{}")
        d["extra"] = raw.get("extra") or {}
        out[r[0]] = d
    return out


def store_raw(
    con, items: Iterable[Listing], fetched_at: datetime, *, group: str, page_type: str = ""
) -> int:
    """Append changed listings to listing_raw. Returns rows added."""
    by_source: dict[str, list[Listing]] = {}
    for lst in items:
        by_source.setdefault(lst.source, []).append(lst)
    added = 0
    for source, lsts in by_source.items():
        old = latest_rows(con, source, [x.listing_id for x in lsts])
        batch = []
        for lst in lsts:
            prev = old.get(lst.listing_id)
            row = _merge(listing_row(lst), prev)
            h = _hash(row)
            if prev is not None and _hash(prev) == h:
                continue
            old[lst.listing_id] = row  # the same id twice in one batch stores once
            raw = {k: _jsonable(v) for k, v in row.items() if k != "url"}
            batch.append(
                [source, lst.listing_id, fetched_at, row["url"], h,
                 json.dumps(raw, ensure_ascii=False, sort_keys=True), group,
                 page_type or None, *[row.get(c) for c in CONTENT_FIELDS]]
            )
        if batch:
            cols = ["source", "listing_id", "fetched_at", "url", "row_hash", "raw_json",
                    "scope", "page_type", *CONTENT_FIELDS]
            marks = ", ".join("?" for _ in cols)
            con.executemany(
                f"INSERT INTO listing_raw ({', '.join(cols)}) VALUES ({marks})", batch
            )
            added += len(batch)
    return added


# ---- current state, snapshots, delisting ---------------------------------------------------

MISSED_RUNS_TO_DELIST = 3  # decisions.md: delisted after 3 missed days
RELIST_WINDOW_DAYS = 180  # a new id for a flat delisted within this many days is a relisting
RELIST_AREA_TOL = 0.02


def record_seen(
    con, source: str, scope: str, items: list[dict], day: date, *, complete: bool = True
) -> dict:
    """Listings a run of `source` over `scope` found on HK date `day`.

    Each item: listing_id, kind, url, price_hkd, saleable_sqft, flat, building_text. Writes
    one snapshot per listing per day (a second run the same day overwrites it) with status
    new, seen, price_change or relisted. Only a run that covered its whole scope
    (`complete`) records a run day, so an interrupted run never causes delistings."""
    counts = {"new": 0, "seen": 0, "price_change": 0, "relisted": 0}
    ids = [str(i["listing_id"]) for i in items]
    known = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT listing_id, status, price_hkd, first_seen FROM listing "
            "WHERE source = ? AND listing_id IN (SELECT unnest(?))",
            [source, ids],
        ).fetchall()
    }
    prev_snap = {
        r[0]: (r[1], r[2])
        for r in con.execute(
            "SELECT listing_id, arg_max(price_hkd, snapshot_date), "
            "arg_max(status, snapshot_date) FROM listing_snapshot "
            "WHERE source = ? AND snapshot_date < ? AND listing_id IN (SELECT unnest(?)) "
            "GROUP BY listing_id",
            [source, day, ids],
        ).fetchall()
    }
    today_status = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT listing_id, status FROM listing_snapshot WHERE source = ? "
            "AND snapshot_date = ? AND listing_id IN (SELECT unnest(?))",
            [source, day, ids],
        ).fetchall()
    }
    for it in items:
        lid = str(it["listing_id"])
        price = it.get("price_hkd")
        k = known.get(lid)
        if lid in today_status and today_status[lid] in ("new", "relisted"):
            st = today_status[lid]  # a second run today keeps the day's first verdict
        elif k is None:
            st = "new"
        elif k[0] == "delisted":
            st = "relisted"
        elif lid in prev_snap and prev_snap[lid][0] != price:
            st = "price_change"
        else:
            st = "seen"
        counts[st] += 1
        con.execute(
            """
            INSERT INTO listing (source, listing_id, kind, group_name, url, building_text,
                flat, price_hkd, saleable_sqft, first_seen, last_seen, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT DO UPDATE SET kind = excluded.kind, group_name = excluded.group_name,
                url = excluded.url, building_text = excluded.building_text,
                flat = excluded.flat, price_hkd = excluded.price_hkd,
                saleable_sqft = excluded.saleable_sqft,
                last_seen = greatest(listing.last_seen, excluded.last_seen),
                status = 'active', delisted_on = NULL
            """,
            [source, lid, it.get("kind"), scope, it.get("url"), it.get("building_text"),
             it.get("flat"), price, it.get("saleable_sqft"), day, day],
        )
        con.execute(
            "INSERT OR REPLACE INTO listing_snapshot (source, listing_id, snapshot_date, "
            "building_id, floor_band, flat, price_hkd, saleable_sqft, status) "
            "SELECT source, listing_id, ?, building_id, floor_band, flat, price_hkd, "
            "saleable_sqft, ? FROM listing WHERE source = ? AND listing_id = ?",
            [day, st, source, lid],
        )
    if complete:
        con.execute(
            "INSERT OR IGNORE INTO listing_run_day VALUES (?, ?, ?)", [source, scope, day]
        )
    return counts


def mark_delisted(con, source: str, scope: str, day: date) -> int:
    """Active listings of this source and scope that missed the last 3 run days (days the
    source actually ran over the scope; days the laptop was off don't count)."""
    rows = con.execute(
        """
        SELECT l.listing_id FROM listing l
        WHERE l.source = ? AND l.group_name = ? AND l.status = 'active'
          AND (SELECT count(*) FROM listing_run_day d
               WHERE d.source = l.source AND d.scope = l.group_name
                 AND d.run_date > l.last_seen AND d.run_date <= ?) >= ?
        """,
        [source, scope, day, MISSED_RUNS_TO_DELIST],
    ).fetchall()
    for (lid,) in rows:
        con.execute(
            "UPDATE listing SET status = 'delisted', delisted_on = ? "
            "WHERE source = ? AND listing_id = ?",
            [day, source, lid],
        )
        con.execute(
            "INSERT OR REPLACE INTO listing_snapshot (source, listing_id, snapshot_date, "
            "building_id, floor_band, flat, price_hkd, saleable_sqft, status) "
            "SELECT source, listing_id, ?, building_id, floor_band, flat, price_hkd, "
            "saleable_sqft, 'delisted' FROM listing WHERE source = ? AND listing_id = ?",
            [day, source, lid],
        )
    return len(rows)


def relink_relistings(con, source: str, day: date) -> int:
    """A listing first seen on `day` for a flat (same building, floor band, flat and area
    within 2%) whose earlier listing on the same site was delisted recently is the same
    flat relisted: it gets `relist_of` = the original listing and today's snapshot is marked
    'relisted'. Needs matching (building_id, floor_band) to have run."""
    pairs = con.execute(
        f"""
        SELECT n.listing_id, arg_max(coalesce(o.relist_of, o.listing_id), o.delisted_on)
        FROM listing n JOIN listing o
          ON o.source = n.source AND o.listing_id <> n.listing_id
         AND o.status = 'delisted' AND o.kind = n.kind
         AND o.building_id = n.building_id AND o.floor_band = n.floor_band
         AND o.flat = n.flat
         AND abs(o.saleable_sqft - n.saleable_sqft) <= {RELIST_AREA_TOL} * o.saleable_sqft
         AND o.delisted_on >= n.first_seen - INTERVAL {RELIST_WINDOW_DAYS} DAY
        WHERE n.source = ? AND n.first_seen = ? AND n.relist_of IS NULL
          AND n.building_id IS NOT NULL AND n.flat IS NOT NULL
        GROUP BY n.listing_id
        """,
        [source, day],
    ).fetchall()
    for new_id, old_id in pairs:
        con.execute(
            "UPDATE listing SET relist_of = ? WHERE source = ? AND listing_id = ?",
            [old_id, source, new_id],
        )
        con.execute(
            "UPDATE listing_snapshot SET status = 'relisted' "
            "WHERE source = ? AND listing_id = ? AND snapshot_date = ?",
            [source, new_id, day],
        )
    return len(pairs)
