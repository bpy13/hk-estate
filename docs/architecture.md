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

hkestate daily  (= listings, all sources, + cache prune; Task Scheduler 08:00, docs/operations.md)
  └─ listing_run.py                          per source, per pilot estate group
       ├─ sources/centanet.py: sitemap -> detail pages (new + rotating 1/7)
       ├─ sources/hse28_listings.py: estate search pages -> detail if new / price changed
       │    (skipped while data/locks/hse28.lock is held by a backfill or update)
       ├─ sources/spacious.py: neighbourhood search pages (cards only)
       ├─ listings.store_raw -> listing_raw   append when changed; free text never stored
       ├─ listings.record_seen -> listing, listing_snapshot (one per HK day), mark_delisted
       ├─ match.py -> listing.building_id, floor_band, dup_group; relink_relistings
       └─ flags.py (keywords.yml) -> risk_flag
hkestate update (weekly) -> backfill.update_estates: newest 28Hse deal-list pages

hkestate train                               (fair price, Phase 4)
  └─ features.snapshot: one short DB session -> data/model/snapshot/*.parquet
       (txn_all query minus db.QUARANTINE_SQL rows, buildings, listings, RVD index, run_log)
  └─ features.deal_features: one row per deal, exclude_reason (None = trainable)
  └─ model.train: 3 LightGBM fits (calibration, out-of-time test, final)
       -> data/model/fair_price.txt + .json, test_predictions.parquet, deals.parquet
       -> data/reports/model_eval.md
hkestate rank
  └─ snapshot -> features.listing_features -> model (+ TreeSHAP) -> gap.py (district gaps)
       -> data/reports/ranked_listings_<date>.csv, asking_gap_<date>.csv
       -> data/model/ranked_latest.parquet
streamlit run app/dashboard.py -> dashboard_data.py reads data/model/ (+ run_log read-only)
       and data/reports/growth/<newest date>/ (page "Growth study")
hkestate datacheck
  └─ snapshot (+ Land Registry yearly counts, cached) -> datacheck.py
       -> data/reports/datacheck_<date>.md (verdict per check; nothing deleted)
hkestate report                              (growth study, Phase 5)
  └─ snapshot (+ deal_pair, RVD series) -> features.deal_features (clean = exclude_reason None)
       -> pairs.py: real, ricacorp, gain (% change; check only), estimated (model value now)
       -> costs.py: stamp duty (dated IRD tables), round-trip costs, RVD yields x listing anchor
       -> growth.py: regression + time/district checks, repeat-sales index, drawdown
       -> study.py (all of it) -> report.py + charts.py
       -> data/reports/findings_<date>.md + findings_<date>/*.svg
       --start/--end: one window (pairs.in_window); --settings standard: study.STANDARD,
       pairs built once (study.prepare), one analysis per window (study.analyse)
       -> data/reports/growth/<date>/<setting>/{effects,districts,index,returns}.csv
          + settings.json
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
| `backfill.py` | crawl plan per estate, resumable steps, loading; `update_estates` (weekly) |
| `listing_types.py` | the `Listing` record every listing parser returns |
| `sources/centanet.py`, `sources/hse28_listings.py`, `sources/spacious.py` | listing parsers and URLs (agent details never read) |
| `listing_run.py` | the daily listing run: pilot targets per site plus the territory group (Centanet sitemap, Spacious building pages), fetch, store, finish in short sessions |
| `listings.py` | listing storage, snapshots, delisting, relisting links |
| `match.py` | listing → building (manual / single / exact / range / contains / fuzzy), floor band, cross-site duplicates |
| `flags.py`, `keywords.yml` | keyword risk flags with negations |
| `sources/ricacorp.py` | Ricacorp estate pages (robots-allowed, never `/api/`): parsed deals in `ricacorp_deal`, placement in buildings, `txn_raw` rows (source `ricacorp`) and `deal_pair` previous-deal links; cached pages deleted after parsing |
| `link.py` | links 28Hse estates/buildings (`h28-`) to master ones (`cd-`) in `source_map` and `hse28_estate`; `txn_all` reads it so both sources' deals share one unit |
| `hse28_territory.py` | territory 28Hse backfill: estate list (sitemap, CSV, listing cards), largest-first order, resumable run |
| `locks.py` | job lock files (`hse28`, `listings`, `centanet`, `cd-history`, `model`) |
| `maintenance.py` | deletes cached listing pages 7+ days old |
| `features.py` | Phase 4 feature table: database extract/snapshot, one row per deal and per listing, model exclusions (parking, first-hand rule, houses, price check, quarantine), 28Hse agreement dates, RVD deflator |
| `model.py` | fair-price LightGBM: time split, index-adjusted target, conformal intervals, comparables check, TreeSHAP reasons, `model_eval.md` |
| `gap.py` | asking-vs-deal pairs and per-district gap (5% `assumed` under 30 pairs) |
| `rank.py` | values active sale listings, discount, confidence, cross-site dedupe, ranked CSV |
| `dashboard_data.py` | data and filters for `app/dashboard.py` (Streamlit: ranked list, detail, model page, growth-study page, run banner) |
| `datacheck.py` | checks on the snapshot: sales per year vs the Land Registry, duplicates, odd values, coverage (`hkestate datacheck`) |
| `pairs.py` | repeat-sales pairs: real, Ricacorp, % change (check), estimated (Phase 5) |
| `costs.py` | stamp duty by date (IRD tables, cited), round-trip costs, RVD yields, listing anchor |
| `growth.py` | attribute regression (clustered by building), out-of-sample checks, robust verdicts, repeat-sales index, drawdown, RVD long-run |
| `study.py` | the growth study end to end from a snapshot (`hkestate report`) |
| `report.py`, `charts.py` | findings Markdown and SVG charts |
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
| `listing_raw` | every listing reading that changed something: parsed fields as columns (kind, building text, floor, flat, price or monthly rent, areas, rooms, facing, age, dates, feature tags) + `raw_json`; never agent names, phones, licences, agencies or free text | (source, listing_id, fetched_at) |
| `listing` | current state per listing: estate group, matched `building_id` + `match_method`, `floor_band`, price, first/last seen, `status` (active/delisted), `relist_of`, `dup_group`, `last_detail_at` | (source, listing_id) |
| `listing_snapshot` | one row per listing per HK day: price, building, band, status (new, seen, price_change, relisted, delisted) | (source, listing_id, snapshot_date) |
| `listing_run_day` | days a source's run covered an estate group (delisting counts these) | (source, scope, run_date) |
| `risk_flag` | keyword flags per listing (haunted, tenanted, sea_view, renovated, illegal_structure, studio) with the matched term | |
| `attribute` | flat attributes (later phases) | |
| `ricacorp_deal` | parsed Ricacorp rows (address, price, days held, % gain), placed in a building once the master list has the estate | `deal_id` |
| `deal_pair` | a resale and its previous deal (matched stored deal or derived from days held and % gain), with `prev_derived` | (`cur_source`, `cur_id`) |
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
| `excl_quarantine` | the wrongly-attached LOHAS Park Centadata rows (`db.QUARANTINE_SQL`); also ignored by the duplicate, bulk, outlier and price-check tests of other rows (Phase 5) |
| `excl_parking` | the price includes a parking space (28Hse 連車位 `with_parking`, SRPE `has_parking`) (Phase 5) |
| `excl_price_check` | under half or over double the building's median price per sq ft, adjusted to today's market by the RVD index of the flat's size class (5+ otherwise-clean deals; part-share transfers) (Phase 5) |

The first-hand rule (developer sales SRPE doesn't flag) and the house/subsidised-estate filter
need building-level logic and the master tables, so they live in `features.deal_features`
(`exclude_reason`), which both the fair-price model and the growth study use. So does
`low_psf` (2026-10-23, `features.low_psf_check`): index-adjusted price per sq ft under 0.15×
the district's clean median, or under 0.5× the same flat's other clean sales within 5 years
(part shares the price check missed).

The fair-price model adds its own exclusions on top (`features.py` `exclude_reason`,
decisions.md "Phase 4 build"); the stored views are unchanged. `db.txn_all_sql(latest)`
runs the `txn_all` query over any `txn_latest`-shaped source, which is how the model leaves
out the quarantined rows (`db.QUARANTINE_SQL`) before the flags are computed.

The views are recomputed on every query. At territory scale (Phase 3) `txn_all` may need to
be materialised as a table after each load.

## External services and costs

| Task | Service | Typical cost |
|---|---|---|
| Transactions | 28Hse (free pages, 2018 on) | free; ~1.4 s per page |
| Cross-check, building list | Centadata pages on hk.centanet.com | free |
| Indices | RVD `.xls` files | free |
