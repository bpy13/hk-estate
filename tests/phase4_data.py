"""A small synthetic database for the Phase 4 tests (features, model, gap, ranking, dashboard).

Two estates: a pilot estate (28Hse ids, group `taikoo-shing`) and a territory estate
(Centadata ids) with two buildings, plus a house estate the coverage filter excludes.
Prices follow a simple rule (higher floor, newer month = dearer) so the model has something
to learn. Everything here is made up.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta

from hkestate import db, geo
from hkestate.sources import cd_master

FETCHED = datetime(2026, 10, 17, 9)


def make_db(path, *, n_deals: int = 400, seed: int = 1, end: date = date(2026, 9, 30)):
    con = db.connect(path)
    db.init_db(con)
    cd_master.ensure_tables(con)
    geo.ensure_tables(con)
    con.execute("""INSERT INTO estate VALUES
        ('h28-1', '太古城', NULL, NULL, 'taikoo-shing', 'hse28', '1', now()),
        ('h28-9', '日出康城', NULL, NULL, 'lohas-park', 'hse28', '9', now()),
        ('cd-2-AAA', '綠悠雅苑', NULL, '元朗區', NULL, 'centadata', '2-AAA', now()),
        ('cd-2-HHH', '洋房村', NULL, '西貢區', NULL, 'centadata', '2-HHH', now())""")
    con.execute("""INSERT INTO building (building_id, estate_id, name_zh, completion_date,
                                         floors, flats, saleable_to_gross) VALUES
        ('h28-1-b1', 'h28-1', '金殿台', NULL, NULL, NULL, 0.8),
        ('h28-9-b1', 'h28-9', 'LP6', NULL, NULL, NULL, 0.8),
        ('cd-1-AAA', 'cd-2-AAA', '第1座', DATE '2005-06-01', 30, 240, NULL),
        ('cd-1-BBB', 'cd-2-AAA', '第2座', DATE '2005-06-01', 30, 240, NULL),
        ('cd-1-NEW', 'cd-2-AAA', '第3座', NULL, 40, 8, NULL),
        ('cd-1-HHH', 'cd-2-HHH', '洋房', DATE '1999-01-01', 3, 10, NULL)""")
    con.execute("""INSERT INTO master_estate (code, name_zh, estate_type, district, units,
                                              excluded, exclude_reason, pilot) VALUES
        ('2-AAA', '綠悠雅苑', 'Normal', '天水圍 (元朗區)', 800, false, NULL, false),
        ('2-HHH', '洋房村', 'Normal', '西貢區', 10, true, 'house', false)""")
    con.execute("""INSERT INTO master_building (building_id, estate_code, block_name, district,
                                                lat, lon, mtr_m, built_year, built_basis) VALUES
        ('h28-1-b1', NULL, '金殿台', NULL, 22.287, 114.217, 300, NULL, NULL),
        ('cd-1-AAA', '2-AAA', '第1座', '元朗區', 22.46, 114.0, 800, 2005, 'completion'),
        ('cd-1-BBB', '2-AAA', '第2座', '元朗區', 22.461, 114.001, NULL, 2005, 'completion'),
        ('cd-1-NEW', '2-AAA', '第3座', '元朗區', NULL, NULL, NULL, NULL, NULL),
        ('cd-1-HHH', '2-HHH', '洋房', '西貢區', 22.3, 114.3, 3000, 1999, 'completion')""")
    # the RVD price index rises 0.5% a month from 2015
    rows = []
    for cls in ("A", "B", "C", "D", "E", "ALL"):
        d, v = date(2015, 1, 1), 100.0
        while d <= date(2026, 8, 1):
            rows.append(("rvd_price_index", "month", d, cls, "ALL", v))
            d = (d.replace(day=28) + timedelta(days=5)).replace(day=1)
            v *= 1.005
    con.executemany("INSERT INTO price_index (series, freq, period, class, region, value) "
                    "VALUES (?, ?, ?, ?, ?, ?)", rows)

    rng = random.Random(seed)
    deals = []
    start = date(2019, 1, 1)
    span = (end - start).days
    for i in range(n_deals):
        b = rng.choice(["h28-1-b1", "cd-1-AAA", "cd-1-BBB"])
        floor = rng.randint(2, 30)
        flat = rng.choice("ABCDEFGH")
        day = start + timedelta(days=rng.randint(0, span))
        area = {"A": 500.0, "B": 520.0, "C": 610.0, "D": 700.0}.get(flat, 450.0)
        base = 18000 if b == "h28-1-b1" else 9000
        months = (day.year - 2019) * 12 + day.month
        psf = base * (1 + 0.01 * floor) * (1.005 ** months) * rng.uniform(0.97, 1.03)
        src = "hse28" if b.startswith("h28") else "centadata"
        deals.append({
            "source": src, "source_id": f"d{i}", "fetched_at": FETCHED,
            "url": "https://example.com/x", "building_id": b,
            "estate_id": "h28-1" if src == "hse28" else "cd-2-AAA",
            "floor_label": str(floor), "flat": flat, "deal_date": day,
            "instrument_date": day - timedelta(days=14) if src == "centadata" else None,
            "registration_date": day if src == "centadata" else None,
            "deal_type": "registry", "price_hkd": int(psf * area), "saleable_sqft": area,
            "rooms": 2 if area < 600 else 3,
            "raw_json": json.dumps({"with_parking": False, "feature": ""}),
        })
    # the pilot building's recorded history starts in the 1990s (a Centadata latest deal), as
    # it does for the real pilot estates
    deals.append({
        "source": "centadata", "source_id": "old0", "fetched_at": FETCHED,
        "url": "https://example.com/x", "building_id": "h28-1-b1", "estate_id": "h28-1",
        "floor_label": "3", "flat": "H", "deal_date": date(1996, 5, 1),
        "instrument_date": date(1996, 4, 20), "registration_date": date(1996, 5, 1),
        "deal_type": "registry", "price_hkd": 2_000_000, "saleable_sqft": 450.0,
        "raw_json": json.dumps({"feature": ""}),
    })
    db.insert_txn_raw(con, deals)
    for d in deals:
        fl = int(d["floor_label"])
        con.execute("INSERT OR IGNORE INTO unit (unit_id, building_id, floor_label, floor_num, "
                    "floor_kind, flat) VALUES (?, ?, ?, ?, 'number', ?)",
                    [db.unit_id(d["building_id"], d["floor_label"], d["flat"]),
                     d["building_id"], d["floor_label"], fl, d["flat"]])
    con.close()
    return path


def add_deal(con, source_id, building, floor, flat, day, price, *, area=500.0, source="hse28",
             raw=None, first_hand=False, fetched_at=FETCHED, estate_id=None):
    db.insert_txn_raw(con, [{
        "source": source, "source_id": source_id, "fetched_at": fetched_at,
        "url": "https://example.com/x", "building_id": building, "estate_id": estate_id,
        "floor_label": str(floor), "flat": flat, "deal_date": day,
        "deal_type": "first_hand" if source == "srpe" else "registry",
        "price_hkd": price, "saleable_sqft": area, "first_hand": first_hand,
        "raw_json": json.dumps(raw or {}),
    }])


def add_listing(con, source, listing_id, building, *, price, area, flat=None, floor_text=None,
                band="unknown", facing=None, features=None, rooms=2, status="active",
                first_seen=date(2026, 10, 10), last_seen=date(2026, 10, 17), kind="sale",
                dup_group=None, age_years=None):
    con.execute(
        "INSERT INTO listing (source, listing_id, kind, group_name, url, building_text, "
        "building_id, match_method, floor_band, flat, price_hkd, saleable_sqft, first_seen, "
        "last_seen, status, dup_group) VALUES (?, ?, ?, 'g', ?, 'x', ?, 'exact', ?, ?, ?, ?, ?, "
        "?, ?, ?)",
        [source, listing_id, kind, f"https://example.com/{listing_id}", building, band, flat,
         price, area, first_seen, last_seen, status, dup_group or f"{source}:{listing_id}"])
    con.execute(
        "INSERT INTO listing_raw (source, listing_id, fetched_at, url, row_hash, raw_json, kind, "
        "floor_text, flat, price_hkd, saleable_sqft, rooms, facing, features, age_years) "
        "VALUES (?, ?, ?, 'u', 'h', '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [source, listing_id, datetime.combine(last_seen, datetime.min.time()), kind, floor_text,
         flat, price, area, rooms, facing, features, age_years])
