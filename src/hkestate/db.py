"""DuckDB schema and helpers.

Shape: estate -> phase -> building -> unit, plus raw transactions (append-only), listings,
index series, attributes, risk flags, a map of other sources' names to master ids, and run
bookkeeping. Raw rows are never updated or deleted; the clean views on top decide what the
analysis sees. See docs/architecture.md for the full picture.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import duckdb

from hkestate.config import get_data_dir

# Exclusion thresholds (docs/decisions.md, "Schema").
MIN_PRICE_HKD = 500_000
OUTLIER_MADS = 5
OUTLIER_MIN_DEALS = 5  # fewer deals in a building-year: no outlier test
# The MAD is scaled by 1.4826 (so 5 MADs ~ 5 standard deviations for normal data) and never
# taken below 5% of the median: in a building of near-identical flats the raw MAD is tiny
# and would flag ordinary floor or view premiums.
MAD_SCALE = 1.4826
MAD_FLOOR = 0.05
BULK_MIN_UNITS = 3  # this many flats in one building on one day = bulk deal
DUP_PRICE_TOL = 0.01  # same sale reported twice: price within 1% ...
DUP_DAYS = 90  # ... and dates within 90 days
# Price check (decisions.md "Phase 4 build", added to the shared view in Phase 5): a deal at
# under half or over double its building's median index-adjusted price per sq ft (buildings
# with 5+ clean deals) is not a normal sale of a whole flat, e.g. a part-share transfer.
PRICE_CHECK_LOW, PRICE_CHECK_HIGH, PRICE_CHECK_MIN_DEALS = 0.5, 2.0, 5
SQFT_PER_SQM = 10.7639

# Which copy of a sale wins when it is reported more than once (lower = preferred).
SOURCE_RANK_SQL = """
    CASE
        WHEN source = 'srpe' THEN 0  -- the developer's own register: the flagged first-hand copy
        WHEN source = 'hse28' AND deal_type = 'registry' THEN 1
        WHEN source = 'hse28' THEN 2
        WHEN source = 'centadata' THEN 3
        ELSE 9
    END
"""


def default_db_path() -> Path:
    return get_data_dir() / "hkestate.duckdb"


LOCK_WAIT_S = 30 * 60  # how long to wait for another process to let go of the file
LOCK_BACKOFF_FIRST_S = 5.0
LOCK_BACKOFF_MAX_S = 60.0


def _is_lock_error(e: Exception) -> bool:
    """True for the 'another process has the file open' IOException, and nothing else."""
    text = str(e).lower()
    # Linux/Mac say "lock"; Windows says "used by another process" / "already open".
    return any(k in text for k in ("lock", "another process", "already open"))


def connect(path: Path | str | None = None, *,
            wait_s: float = LOCK_WAIT_S) -> duckdb.DuckDBPyConnection:
    """Open the database. DuckDB allows one process at a time, so if another command holds
    the file we retry (5 s, doubling to 60 s between tries) for up to `wait_s` (30 minutes:
    the scheduled train and rank hold it for several minutes). Each wait is logged to stderr,
    which the task wrappers capture. Any other error is raised at once."""
    path = Path(path) if path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_s
    delay = LOCK_BACKOFF_FIRST_S
    while True:
        try:
            return duckdb.connect(str(path))
        except duckdb.IOException as e:
            if not _is_lock_error(e) or time.monotonic() + delay > deadline:
                raise
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} database busy (another process has "
                  f"{path.name} open); retrying in {delay:.0f}s", file=sys.stderr, flush=True)
            time.sleep(delay)
            delay = min(delay * 2, LOCK_BACKOFF_MAX_S)


@contextmanager
def session(path: Path | str | None = None):
    """A short-lived connection: long jobs open the file only while writing, so other
    commands can use the database in between."""
    con = connect(path)
    try:
        yield con
    finally:
        con.close()


def unit_id(building_id: str, floor_label: str, flat: str) -> str:
    """Unit key: building + floor label (as text) + flat."""
    return f"{building_id}|{floor_label}|{flat}"


# ---- DDL ----------------------------------------------------------------------------------

TABLES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS estate (
        estate_id   VARCHAR PRIMARY KEY,
        name_zh     VARCHAR NOT NULL,
        name_en     VARCHAR,
        district    VARCHAR,
        group_name  VARCHAR,               -- estate group, e.g. LOHAS Park's developments
        source      VARCHAR NOT NULL,      -- source the id was minted from
        source_key  VARCHAR NOT NULL,
        created_at  TIMESTAMP DEFAULT current_timestamp
    )""",
    """
    CREATE TABLE IF NOT EXISTS phase (
        phase_id    VARCHAR PRIMARY KEY,
        estate_id   VARCHAR NOT NULL,
        name_zh     VARCHAR NOT NULL,
        source_key  VARCHAR
    )""",
    """
    CREATE TABLE IF NOT EXISTS building (
        building_id       VARCHAR PRIMARY KEY,
        estate_id         VARCHAR NOT NULL,
        phase_id          VARCHAR,
        name_zh           VARCHAR NOT NULL,
        name_en           VARCHAR,
        source_key        VARCHAR,
        completion_date   DATE,
        floors            INTEGER,
        flats             INTEGER,
        saleable_to_gross DOUBLE,          -- building's own ratio, for gross-only conversion
        skipped_floors    VARCHAR          -- e.g. '4,13,14,24' (labels the building skips)
    )""",
    """
    CREATE TABLE IF NOT EXISTS unit (
        unit_id         VARCHAR PRIMARY KEY,  -- building_id|floor_label|flat
        building_id     VARCHAR NOT NULL,
        floor_label     VARCHAR NOT NULL,
        floor_num       INTEGER,              -- label as a number (G/F = 0); NULL if unknown
        floor_kind      VARCHAR,              -- number, ground, podium, basement, top, unknown
        flat            VARCHAR NOT NULL,
        saleable_sqft   DOUBLE,
        gross_sqft      DOUBLE,
        rooms           INTEGER,
        source_unit_key VARCHAR,              -- e.g. 28Hse unit-id
        src_deal_count  INTEGER,              -- deals the source says it holds for the flat
        src_url         VARCHAR
    )""",
    """
    CREATE TABLE IF NOT EXISTS txn_raw (
        source            VARCHAR NOT NULL,   -- hse28, centadata, ...
        source_id         VARCHAR NOT NULL,   -- stable id of the deal within the source
        fetched_at        TIMESTAMP NOT NULL,
        url               VARCHAR,
        row_hash          VARCHAR NOT NULL,   -- hash of the content fields (change detection)
        estate_id         VARCHAR,
        building_id       VARCHAR,
        source_unit_key   VARCHAR,
        estate_name       VARCHAR,
        phase_name        VARCHAR,
        building_name     VARCHAR,
        floor_label       VARCHAR,
        flat              VARCHAR,
        deal_date         DATE,               -- the date the source shows (see date_basis)
        instrument_date   DATE,               -- agreement date, when the source gives it
        registration_date DATE,
        deal_type         VARCHAR,            -- registry, market, unknown
        price_hkd         BIGINT,
        saleable_sqft     DOUBLE,
        gross_sqft        DOUBLE,
        rooms             INTEGER,
        gain_pct          DOUBLE,
        first_hand        BOOLEAN DEFAULT false,
        non_market        BOOLEAN DEFAULT false,
        raw_json          VARCHAR             -- every parsed field, as the source gave it
    )""",
    # Listings. No column ever holds an agent's name, phone, licence or agency (PDPO; a test
    # checks the column names). Free text (titles, blurbs) is read for keyword flags only.
    """
    CREATE TABLE IF NOT EXISTS listing_raw (
        source        VARCHAR NOT NULL,
        listing_id    VARCHAR NOT NULL,
        fetched_at    TIMESTAMP NOT NULL,
        url           VARCHAR,
        row_hash      VARCHAR,
        raw_json      VARCHAR               -- parsed fields; never agent names or phones
    )""",
    """
    CREATE TABLE IF NOT EXISTS listing_snapshot (
        source        VARCHAR NOT NULL,
        listing_id    VARCHAR NOT NULL,
        snapshot_date DATE NOT NULL,        -- HK date
        building_id   VARCHAR,
        floor_band    VARCHAR,
        flat          VARCHAR,
        price_hkd     BIGINT,
        saleable_sqft DOUBLE,
        status        VARCHAR,
        PRIMARY KEY (source, listing_id, snapshot_date)
    )""",
    # Current state of each listing (one row per source + id), rebuilt by every run.
    """
    CREATE TABLE IF NOT EXISTS listing (
        source        VARCHAR NOT NULL,
        listing_id    VARCHAR NOT NULL,
        kind          VARCHAR,               -- sale, rent
        group_name    VARCHAR,               -- pilot estate group the run was reading
        url           VARCHAR,
        building_text VARCHAR,
        building_id   VARCHAR,               -- master building, NULL if not matched
        match_method  VARCHAR,               -- single, exact, fuzzy, manual, ambiguous, none
        floor_band    VARCHAR,               -- low, mid, high, unknown
        flat          VARCHAR,
        price_hkd     BIGINT,
        saleable_sqft DOUBLE,
        first_seen    DATE,
        last_seen     DATE,
        status        VARCHAR,               -- active, delisted
        delisted_on   DATE,
        relist_of     VARCHAR,               -- earlier listing id of the same flat (same source)
        dup_group     VARCHAR,               -- cross-site duplicates share this key
        last_detail_at TIMESTAMP,            -- last detail-page read
        PRIMARY KEY (source, listing_id)
    )""",
    # Days on which a source's listing run covered its whole scope (for delisting).
    """
    CREATE TABLE IF NOT EXISTS listing_run_day (
        source      VARCHAR NOT NULL,
        scope       VARCHAR NOT NULL,        -- estate group
        run_date    DATE NOT NULL,           -- HK date
        PRIMARY KEY (source, scope, run_date)
    )""",
    # RVD series share one shape. price_index: price indices, popular developments, average
    # prices. rent_index: rental indices, average rents. market_stat: completions, stock,
    # vacancy, take-up (annual).
    *[
        f"""
    CREATE TABLE IF NOT EXISTS {name} (
        series      VARCHAR NOT NULL,       -- e.g. rvd_price_index
        freq        VARCHAR NOT NULL,       -- month, year
        period      DATE NOT NULL,          -- first day of the month / year
        class       VARCHAR NOT NULL,       -- A..E, ABC, DE, ALL
        region      VARCHAR NOT NULL,       -- ALL, HK, KLN, NT, URBAN
        value       DOUBLE,
        provisional BOOLEAN DEFAULT false,
        few_deals   BOOLEAN DEFAULT false,  -- RVD brackets: fewer than 20 transactions
        source_url  VARCHAR,
        fetched_at  TIMESTAMP,
        PRIMARY KEY (series, freq, period, class, region)
    )"""
        for name in ("price_index", "rent_index", "market_stat")
    ],
    """
    CREATE TABLE IF NOT EXISTS attribute (
        unit_id     VARCHAR,
        building_id VARCHAR,
        name        VARCHAR NOT NULL,       -- facing, view, mtr_m, ...
        value       VARCHAR,
        source      VARCHAR,
        fetched_at  TIMESTAMP
    )""",
    """
    CREATE TABLE IF NOT EXISTS risk_flag (
        source      VARCHAR NOT NULL,
        listing_id  VARCHAR,
        unit_id     VARCHAR,
        flag        VARCHAR NOT NULL,       -- haunted, tenanted, ...
        evidence    VARCHAR,
        created_at  TIMESTAMP DEFAULT current_timestamp
    )""",
    """
    CREATE TABLE IF NOT EXISTS source_map (
        source      VARCHAR NOT NULL,       -- centadata, hse28, ...
        kind        VARCHAR NOT NULL,       -- estate, phase, building, unit
        source_key  VARCHAR NOT NULL,
        master_id   VARCHAR,
        name        VARCHAR,
        matched_by  VARCHAR,                -- exact, fuzzy, manual
        created_at  TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (source, kind, source_key)
    )""",
    "CREATE SEQUENCE IF NOT EXISTS run_log_seq",
    """
    CREATE TABLE IF NOT EXISTS run_log (
        run_id      BIGINT PRIMARY KEY DEFAULT nextval('run_log_seq'),
        command     VARCHAR NOT NULL,
        args        VARCHAR,
        started_at  TIMESTAMP NOT NULL,
        finished_at TIMESTAMP,
        status      VARCHAR NOT NULL,       -- running, ok, failed, interrupted
        rows_added  BIGINT,
        message     VARCHAR
    )""",
    """
    CREATE TABLE IF NOT EXISTS run_progress (
        command     VARCHAR NOT NULL,       -- e.g. backfill
        step_key    VARCHAR NOT NULL,       -- e.g. hse28:1140:list
        run_id      BIGINT,
        finished_at TIMESTAMP NOT NULL,
        detail      VARCHAR,
        PRIMARY KEY (command, step_key)
    )""",
]

def txn_all_sql(latest: str = "txn_latest") -> str:
    """The query behind the `txn_all` view, reading deals from `latest` (a view or table
    shaped like `txn_latest`). The fair-price model passes a filtered copy so rows it must
    ignore (see QUARANTINE_SQL) never take part in the duplicate, bulk or outlier tests.

    Added in Phase 5 (so the growth study and any other reader of `txn` share them):
    `excl_quarantine` (QUARANTINE_SQL rows: flagged, and ignored by the duplicate, bulk,
    outlier and price-check tests of other rows), `excl_parking` (the price includes a
    parking space) and `excl_price_check` (the fair-price model's price check)."""
    return f"""
    WITH latest AS (
        SELECT *, {QUARANTINE_SQL} AS excl_quarantine,
            CASE WHEN json_valid(raw_json) THEN
                coalesce(json_extract_string(raw_json, '$.with_parking') = 'true'
                         OR json_extract_string(raw_json, '$.has_parking') = 'true', false)
                ELSE false END AS excl_parking
        FROM {latest}
    ),
    base AS (
        -- 28Hse dates Land Registry deals by registration date (Centadata cross-check)
        -- a 28Hse deal in a building linked to a master (Centadata) building takes that
        -- building's id, so both sources' deals share one unit (src/hkestate/link.py)
        SELECT t.* REPLACE (
                coalesce(t.registration_date,
                         CASE WHEN t.source = 'hse28' AND t.deal_type = 'registry'
                              THEN t.deal_date END) AS registration_date,
                coalesce(sm.master_id, t.building_id) AS building_id),
            coalesce(sm.master_id, t.building_id) || '|' || t.floor_label || '|' || t.flat
                AS unit_id,
            {SOURCE_RANK_SQL} AS source_rank,
            CASE
                WHEN t.saleable_sqft > 0 THEN t.saleable_sqft
                WHEN t.gross_sqft > 0 AND b.saleable_to_gross > 0
                    THEN t.gross_sqft * b.saleable_to_gross
                WHEN t.gross_sqft > 0 THEN t.gross_sqft
            END AS area_sqft,
            CASE
                WHEN t.saleable_sqft > 0 THEN 'saleable'
                WHEN t.gross_sqft > 0 AND b.saleable_to_gross > 0 THEN 'converted'
                WHEN t.gross_sqft > 0 THEN 'gross_only'
            END AS area_basis
        FROM latest t
        LEFT JOIN building b ON b.building_id = t.building_id
        LEFT JOIN (SELECT source_key, master_id FROM source_map
                   WHERE source = 'hse28' AND kind = 'building') sm
          ON t.source = 'hse28' AND sm.source_key = t.building_id
    ),
    dup AS (  -- the same sale reported again (agency report, other source): keep best copy
        SELECT DISTINCT a.source, a.source_id
        FROM base a JOIN base o
          ON (a.unit_id = o.unit_id
              -- an agency report giving only a floor band: same building and flat
              OR (a.floor_label IN ('高層', '中層', '低層')
                  AND a.building_id = o.building_id AND a.flat = o.flat))
         AND NOT (a.source = o.source AND a.source_id = o.source_id)
         AND NOT o.excl_quarantine
         AND abs(a.price_hkd - o.price_hkd) <= {DUP_PRICE_TOL} * o.price_hkd
         AND abs(date_diff('day', a.deal_date, o.deal_date)) <= {DUP_DAYS}
         AND (o.source_rank < a.source_rank
              OR (o.source_rank = a.source_rank AND o.source <> a.source
                  AND o.source < a.source))
    ),
    flagged AS (
        SELECT base.*,
            (dup.source_id IS NOT NULL) AS excl_duplicate,
            coalesce(price_hkd < {MIN_PRICE_HKD}, false) AS excl_low_price,
            coalesce(first_hand, false) AS excl_first_hand,
            coalesce(non_market, false) AS excl_non_market
        FROM base LEFT JOIN dup USING (source, source_id)
    ),
    bulk AS (
        SELECT building_id, deal_date
        FROM flagged WHERE NOT excl_duplicate AND NOT excl_quarantine
        GROUP BY building_id, deal_date
        HAVING count(DISTINCT unit_id) >= {BULK_MIN_UNITS}
    ),
    psf AS (
        SELECT f.*,
            (bulk.building_id IS NOT NULL) AS excl_bulk,
            CASE WHEN area_basis IN ('saleable', 'converted') AND area_sqft > 0
                 THEN price_hkd / area_sqft END AS psf_hkd
        FROM flagged f LEFT JOIN bulk USING (building_id, deal_date)
    ),
    yr AS (
        SELECT building_id, year(deal_date) AS yr,
            median(psf_hkd) AS med, count(*) AS n
        FROM psf
        WHERE psf_hkd IS NOT NULL
          AND NOT (excl_duplicate OR excl_low_price OR excl_bulk OR excl_quarantine)
        GROUP BY ALL
    ),
    mad AS (
        SELECT p.building_id, yr.yr, yr.med, yr.n,
            greatest({MAD_SCALE} * median(abs(p.psf_hkd - yr.med)), {MAD_FLOOR} * yr.med)
                AS mad
        FROM psf p JOIN yr
          ON p.building_id = yr.building_id AND year(p.deal_date) = yr.yr
        WHERE p.psf_hkd IS NOT NULL
          AND NOT (p.excl_duplicate OR p.excl_low_price OR p.excl_bulk OR p.excl_quarantine)
        GROUP BY p.building_id, yr.yr, yr.med, yr.n
    ),
    outl AS (
        SELECT psf.*,
            coalesce(m.n >= {OUTLIER_MIN_DEALS} AND m.mad > 0
                     AND abs(psf.psf_hkd - m.med) > {OUTLIER_MADS} * m.mad, false)
                AS excl_outlier,
            -- RVD size class (saleable m2: A < 40, B < 70, C < 100, D < 160, E) and month,
            -- for the index-adjusted price check
            CASE WHEN area_sqft IS NULL THEN 'ALL'
                 WHEN area_sqft / {SQFT_PER_SQM} < 40 THEN 'A'
                 WHEN area_sqft / {SQFT_PER_SQM} < 70 THEN 'B'
                 WHEN area_sqft / {SQFT_PER_SQM} < 100 THEN 'C'
                 WHEN area_sqft / {SQFT_PER_SQM} < 160 THEN 'D' ELSE 'E' END AS _cls,
            date_trunc('month', coalesce(psf.instrument_date, psf.deal_date)) AS _month
        FROM psf
        LEFT JOIN mad m ON psf.building_id = m.building_id AND year(psf.deal_date) = m.yr
    ),
    idx AS (  -- RVD price index by class, relative to its last published month
        SELECT i.class, i.period, i.value / l.last_value AS rel
        FROM price_index i
        JOIN (SELECT class, arg_max(value, period) AS last_value FROM price_index
              WHERE series = 'rvd_price_index' AND region = 'ALL' AND freq = 'month'
              GROUP BY class) l USING (class)
        WHERE i.series = 'rvd_price_index' AND i.region = 'ALL' AND i.freq = 'month'
    ),
    adj AS (  -- price per sq ft at today's market level (unadjusted where no index)
        SELECT o.*, o.psf_hkd / coalesce(idx.rel, 1.0) AS _adj_psf
        FROM outl o ASOF LEFT JOIN idx ON idx.class = o._cls AND o._month >= idx.period
    ),
    bmed AS (  -- each building's median adjusted price per sq ft over its otherwise-clean deals
        SELECT building_id, median(_adj_psf) AS med, count(*) AS n
        FROM adj
        WHERE _adj_psf IS NOT NULL
          AND NOT (excl_duplicate OR excl_low_price OR excl_first_hand OR excl_non_market
                   OR excl_bulk OR excl_outlier OR excl_quarantine OR excl_parking)
        GROUP BY building_id
    )
    SELECT adj.* EXCLUDE (source_rank, _cls, _month, _adj_psf),
        coalesce(bm.n >= {PRICE_CHECK_MIN_DEALS}
                 AND (adj._adj_psf < {PRICE_CHECK_LOW} * bm.med
                      OR adj._adj_psf > {PRICE_CHECK_HIGH} * bm.med), false)
            AS excl_price_check,
        u.floor_num, u.floor_kind
    FROM adj
    LEFT JOIN bmed bm ON bm.building_id = adj.building_id
    LEFT JOIN unit u ON u.unit_id = adj.unit_id
    """


# Rows stored wrongly and kept out of analysis without deleting them (raw data is
# append-only; deleting needs the owner's OK, docs/open-items.md). Evaluated on txn_latest.
# - The first LOHAS Park cross-check run (2026-10-16 19:28-19:31, before the matching fix)
#   stored ~14,500 Centadata rows against the wrong LOHAS Park buildings. The fixed run
#   started 19:44; every LOHAS Park Centadata row fetched before that is from the bad run.
QUARANTINE_SQL = """
    (source = 'centadata'
     AND fetched_at < TIMESTAMP '2026-10-16 19:44:00'
     AND estate_id IN (SELECT estate_id FROM estate WHERE group_name = 'lohas-park'))
"""


VIEWS_DDL = [
    # Newest version of every source row (raw is append-only; a changed row adds a version).
    """
    CREATE OR REPLACE VIEW txn_latest AS
    SELECT * EXCLUDE (rn) FROM (
        SELECT *, row_number() OVER (
            PARTITION BY source, source_id ORDER BY fetched_at DESC) AS rn
        FROM txn_raw)
    WHERE rn = 1
    """,
    # Every deal with its unit, comparable area and exclusion flags.
    f"CREATE OR REPLACE VIEW txn_all AS {txn_all_sql()}",
    # What the analysis uses: market resale deals only, one copy each.
    """
    CREATE OR REPLACE VIEW txn AS
    SELECT * FROM txn_all
    WHERE NOT (excl_duplicate OR excl_low_price OR excl_first_hand OR excl_non_market
               OR excl_bulk OR excl_outlier OR excl_quarantine OR excl_parking
               OR excl_price_check)
    """,
]


# Parsed listing fields kept as columns of listing_raw (added in Phase 2; ALTERed onto
# databases created before it). Never an agent name, phone, licence or agency.
LISTING_RAW_COLUMNS = {
    "kind": "VARCHAR",
    "scope": "VARCHAR",
    "page_type": "VARCHAR",
    "estate_name": "VARCHAR",
    "building_text": "VARCHAR",
    "floor_text": "VARCHAR",
    "floor_range": "VARCHAR",
    "flat": "VARCHAR",
    "price_hkd": "BIGINT",
    "saleable_sqft": "DOUBLE",
    "gross_sqft": "DOUBLE",
    "rooms": "INTEGER",
    "facing": "VARCHAR",
    "age_years": "INTEGER",
    "posted_date": "DATE",
    "updated_date": "DATE",
    "features": "VARCHAR",
    "source_estate_key": "VARCHAR",
    "source_building_key": "VARCHAR",
}


def init_db(con: duckdb.DuckDBPyConnection) -> None:
    """Create every table and view. Safe to run again: existing data is kept."""
    for ddl in TABLES_DDL:
        con.execute(ddl)
    for col, typ in LISTING_RAW_COLUMNS.items():
        con.execute(f"ALTER TABLE listing_raw ADD COLUMN IF NOT EXISTS {col} {typ}")
    for ddl in VIEWS_DDL:
        con.execute(ddl)


# ---- txn_raw ------------------------------------------------------------------------------

TXN_COLUMNS = [
    "source",
    "source_id",
    "fetched_at",
    "url",
    "row_hash",
    "estate_id",
    "building_id",
    "source_unit_key",
    "estate_name",
    "phase_name",
    "building_name",
    "floor_label",
    "flat",
    "deal_date",
    "instrument_date",
    "registration_date",
    "deal_type",
    "price_hkd",
    "saleable_sqft",
    "gross_sqft",
    "rooms",
    "gain_pct",
    "first_hand",
    "non_market",
    "raw_json",
]
# The fields that define a deal. Only a change in these adds a new version of a source row;
# the same deal read from a different page (list vs per-flat page, which show different
# extras such as gross area or rooms) does not.
HASHED_COLUMNS = [
    "source",
    "source_id",
    "building_id",
    "floor_label",
    "flat",
    "deal_date",
    "instrument_date",
    "registration_date",
    "deal_type",
    "price_hkd",
    "saleable_sqft",
    "first_hand",
    "non_market",
]


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def row_hash(row: Mapping) -> str:
    content = {k: _jsonable(row.get(k)) for k in HASHED_COLUMNS}
    blob = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def insert_txn_raw(con: duckdb.DuckDBPyConnection, rows: Iterable[Mapping]) -> int:
    """Append deals to txn_raw. A row identical to the newest stored version of the same
    (source, source_id) is skipped, so re-scraping adds nothing. Returns rows added."""
    batch: dict[tuple[str, str], list] = {}
    for row in rows:
        r = dict(row)
        r.setdefault("first_hand", False)
        r.setdefault("non_market", False)
        r["row_hash"] = row_hash(r)
        if r.get("raw_json") is None:
            r["raw_json"] = json.dumps(
                {k: _jsonable(v) for k, v in row.items()}, ensure_ascii=False, sort_keys=True
            )
        batch[(r["source"], r["source_id"])] = [r.get(c) for c in TXN_COLUMNS]
    if not batch:
        return 0
    cols = ", ".join(TXN_COLUMNS)
    con.execute(f"CREATE OR REPLACE TEMP TABLE _txn_in AS SELECT {cols} FROM txn_raw LIMIT 0")
    placeholders = ", ".join("?" for _ in TXN_COLUMNS)
    con.executemany(f"INSERT INTO _txn_in VALUES ({placeholders})", list(batch.values()))
    before = con.execute("SELECT count(*) FROM txn_raw").fetchone()[0]
    con.execute(
        f"""
        INSERT INTO txn_raw ({cols})
        SELECT i.* FROM _txn_in i
        LEFT JOIN (
            SELECT source, source_id, arg_max(row_hash, fetched_at) AS h
            FROM txn_raw GROUP BY source, source_id
        ) cur USING (source, source_id)
        WHERE cur.h IS NULL OR cur.h <> i.row_hash
        """
    )
    con.execute("DROP TABLE _txn_in")
    return con.execute("SELECT count(*) FROM txn_raw").fetchone()[0] - before


# ---- run bookkeeping ----------------------------------------------------------------------


def start_run(con: duckdb.DuckDBPyConnection, command: str, args: str = "") -> int:
    return con.execute(
        "INSERT INTO run_log (command, args, started_at, status) "
        "VALUES (?, ?, current_timestamp, 'running') RETURNING run_id",
        [command, args],
    ).fetchone()[0]


def finish_run(
    con: duckdb.DuckDBPyConnection,
    run_id: int,
    status: str,
    rows_added: int | None = None,
    message: str = "",
) -> None:
    con.execute(
        "UPDATE run_log SET finished_at = current_timestamp, status = ?, rows_added = ?, "
        "message = ? WHERE run_id = ?",
        [status, rows_added, message[:2000], run_id],
    )


def step_done(con: duckdb.DuckDBPyConnection, command: str, step_key: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM run_progress WHERE command = ? AND step_key = ?", [command, step_key]
        ).fetchone()
        is not None
    )


def mark_step(
    con: duckdb.DuckDBPyConnection, command: str, step_key: str, run_id: int, detail: str = ""
) -> None:
    con.execute(
        "INSERT OR REPLACE INTO run_progress VALUES (?, ?, ?, current_timestamp, ?)",
        [command, step_key, run_id, detail],
    )


def clear_steps(con: duckdb.DuckDBPyConnection, command: str, prefix: str = "") -> None:
    con.execute(
        "DELETE FROM run_progress WHERE command = ? AND starts_with(step_key, ?)",
        [command, prefix],
    )

