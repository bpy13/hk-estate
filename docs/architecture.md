# Architecture

A command-line Python package (`hkestate`, in `src/hkestate/`) run on the owner's laptop.
Every web page is read through one polite fetcher (`fetch.py`) that obeys robots.txt, waits
between requests and keeps a compressed copy of each page in `data/cache/`. Parsers turn
pages into rows, which are stored in one DuckDB file, `data/hkestate.duckdb`. The `data/`
folder is git-ignored and can be moved with the `HKESTATE_DATA_DIR` environment variable.

## Flow

```
hkestate backfill --estates <names>          (cli.py; resumable, see run_progress)
  └─ backfill.py                             one estate at a time
       ├─ hse28: estate deal list pages      every deal since March 2021 (10 per page)
       ├─ hse28: phase -> block grid pages   every flat, with its deal count since 2018
       └─ hse28: per-flat pages              only flats with deals the lists don't cover
            └─ db.insert_txn_raw             append-only; identical rows are skipped

hkestate rvd                                 (sources/rvd.py)
  └─ RVD .xls files -> price_index, rent_index, market_stat (replaced per series)

hkestate crosscheck                          (sources/centadata.py)
  └─ Centadata block pages (latest deal per flat, 1995 on)
       ├─ match to 28Hse deals -> match-rate report in data/reports/
       └─ deals 28Hse doesn't have -> txn_raw with source='centadata'

txn_raw -> txn_latest -> txn_all (flags) -> txn (clean)   views, recomputed on query
```

## Modules

| Module | Role |
|---|---|
| `config.py` | data folder (`HKESTATE_DATA_DIR`), user agent |
| `fetch.py` | polite HTTP: robots.txt (protego), ≥1 s per host + jitter, Crawl-delay, cache, back-off, block detection |
| `db.py` | DuckDB DDL, `init_db()`, `insert_txn_raw()`, run bookkeeping |
| `normalise.py` | floor labels, TC/SC names, areas and prices from Chinese text |
| `sources/hse28.py` | 28Hse parsers (deal lists, block grids, per-flat pages) and URLs |
| `sources/rvd.py` | RVD spreadsheet parser and loader |
| `sources/centadata.py` | Centadata block-page parser (reads the page's embedded data without running it) and cross-check |
| `backfill.py` | crawl plan per estate, resumable steps, loading |
| `cli.py` | `hkestate` command |

## Data

Ids are text and deterministic, minted from the source that created them, so reruns give
the same ids: estate `h28-<id>`, phase `h28-<id>-s<n>`, building `h28-<id>-b<n>`, unit
`<building_id>|<floor label>|<flat>`. Other sources' names and codes map to these ids in
`source_map`.

| Table / view | Holds | Key |
|---|---|---|
| `estate`, `phase`, `building` | the estate tree; building has completion date, floors, flats, saleable-to-gross ratio, skipped floor labels | `estate_id`, `phase_id`, `building_id` |
| `unit` | one flat: floor label (text), `floor_num` (G/F = 0), `floor_kind`, flat, areas, rooms | `unit_id` |
| `txn_raw` | every deal as scraped, append-only: source, source_id, fetched_at, url, all fields, `raw_json`; a changed deal adds a new version | (source, source_id, fetched_at) |
| `txn_latest` (view) | newest version of each source row | (source, source_id) |
| `txn_all` (view) | every deal with `unit_id`, `area_sqft` + `area_basis` (`saleable`, `converted`, `gross_only`), price per sq ft and exclusion flags | |
| `txn` (view) | clean deals for analysis: no flagged rows | |
| `price_index`, `rent_index`, `market_stat` | RVD series by class and region, monthly or annual, with provisional and fewer-than-20-deals marks | (series, freq, period, class, region) |
| `listing_raw`, `listing_snapshot` | listings (Phase 2) | |
| `attribute`, `risk_flag` | flat attributes and risk flags (later phases) | |
| `source_map` | other sources' estate/building/unit codes → master ids | (source, kind, source_key) |
| `run_log` | one row per command run: status, rows added, message | `run_id` |
| `run_progress` | finished steps of a resumable command; a rerun skips them | (command, step_key) |

Dates: `deal_date` is the date the source shows; `instrument_date` (agreement) and
`registration_date` are kept separately when a source gives them (Centadata does). 28Hse
dates its Land Registry deals by registration date, so `txn_all` fills
`registration_date` from `deal_date` for them.

Exclusion flags in `txn_all` (decisions.md "Schema"):

| Flag | Rule |
|---|---|
| `excl_duplicate` | the same sale reported again: same unit, price within 1%, dates within 90 days; kept copy by rank: 28Hse Land Registry > 28Hse agency report > Centadata |
| `excl_low_price` | under HK$500,000 |
| `excl_first_hand` | developer sale |
| `excl_non_market` | non-market transfer (when a source marks it) |
| `excl_bulk` | 3 or more flats in one building on the same day |
| `excl_outlier` | price per sq ft more than 5 median absolute deviations from the building's median for that year (MAD scaled by 1.4826, floor 5% of the median; needs ≥5 deals in the building-year) |

The views are recomputed on every query. At territory scale (Phase 3) `txn_all` may need to
be materialised as a table after each load.

## External services and costs

| Task | Service | Typical cost |
|---|---|---|
| Transactions | 28Hse (free pages, 2018 on) | free; ~1.4 s per page |
| Cross-check, building list | Centadata pages on hk.centanet.com | free |
| Indices | RVD `.xls` files | free |
