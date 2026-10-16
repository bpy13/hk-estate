# Plan: HK Estate – data foundation to findings (2026-10-16)

## Goal
A personal research tool for Hong Kong private residential flats that
(1) ranks current listings by their discount to a modelled fair price, with risk flags, and
(2) measures which flat attributes are linked to better price growth and total return.

## Approach
Scrape slowly and politely from free sources. Store raw pages in a cache and structured rows in
DuckDB. Prove the pipeline on 5 pilot estates, then scale to the whole territory. Then build
the fair-price model and dashboard, then the repeat-sales growth study and report.

## Constraints (from docs/decisions.md, verbatim where possible)
- Purpose: the owner's own investment research. Single user.
- Whole territory, private residential flats only. History from 2000.
- Under-market: rank every listing by discount to fair price; **no fixed cut-off**; flags shown.
- Growth: price growth **and** total return, holding periods 3+ years, drawdown, out-of-sample.
- Slow, polite scraping: obey robots.txt, ≤1 request/s per site, cache, resumable. Never call
  disallowed paths (e.g. centanet `/centanetapi/api/`).
- HK$0 data budget. No paid APIs, no IRIS.
- DuckDB + raw-page cache in `data/` (git-ignored; path set in one config setting).
- No personal data (no owner/buyer/agent names or phones).
- Public repo: data, database, reports and real pages never committed; tests use trimmed or
  synthetic fixtures.
- Findings report and ranked deals are written to the data folder.
- Keyword rules for listing text; no LLM.
- Runs on the Windows laptop; daily listings at 08:00 via Task Scheduler; transactions weekly.
- All "Claude's calls" in decisions.md apply (pilot estates, schema, model, growth method,
  costs, tooling).

## Acceptance criteria
- **P0:** env builds; repo skeleton passes `pytest` and `ruff`; `docs/sources.md` records, for
  each source: robots.txt verdict, page structure, history depth, estimated page counts and
  backfill time, and keep/drop. Pilot single-block building chosen.
- **P1:** pilot estates' full transaction histories loaded; 50 random rows match the source
  site; RVD series loaded; rerunning the scraper adds 0 duplicate rows; schema in
  `docs/architecture.md`; tests pass.
- **P2:** listings from the kept listing sites are snapshotted daily for the pilot estates and
  matched to building + floor band (≥80% matched); risk-flag keywords applied; Task Scheduler
  job registered and a run is logged.
- **P3:** territory-wide transactions backfilled (resumable, coverage per estate reported);
  coordinates, MTR distance and building age attached; SRPE first-hand sales stored and
  flagged; daily listings cover the whole territory.
- **P4:** fair-price model with out-of-time error reported; asking-vs-deal gap estimated (or the
  5% default marked as an assumption); ranked listing table written to the data folder; the
  Streamlit dashboard shows the ranked list, filters and a detail page with comparables and
  explanations.
- **P5:** repeat-sales study by attribute with growth, total return, drawdown and out-of-sample
  checks; findings report (Markdown) written to the data folder.

Commands (set in Phase 0): env `hk-estate` (Python 3.12). Tests: `python -m pytest -q`.
Lint: `ruff check .`.

---

## Phase 0 – Setup and source spike

- [x] **Task 0.1: Environment and skeleton**
  - Files: `environment.yml`, `pyproject.toml` (package `hkestate` in `src/hkestate/`, ruff +
    pytest config), `src/hkestate/config.py` (`DATA_DIR`, default `<repo>/data`, overridable
    by env var `HKESTATE_DATA_DIR`), `tests/test_config.py`, `README.md`, CLAUDE.md
    Environment section filled in.
  - Done when: `conda run -n hk-estate python -m pytest -q` passes and `ruff check .` is clean.
  - Notes: follow the python-env skill. Dependencies: httpx, selectolax, beautifulsoup4,
    playwright (installed, used only if needed), duckdb, pandas, pyarrow, lightgbm, shap,
    statsmodels, scikit-learn, streamlit, pydeck, rapidfuzz, opencc (TC/SC normalisation),
    nbstripout, pytest, ruff.
- [x] **Task 0.2: Polite fetcher**
  - Files: `src/hkestate/fetch.py`, `tests/test_fetch.py`.
  - Done when: tests (written first, using a mock transport) show these behaviours, and pass:
    - robots.txt disallow is refused
    - ≥1 s between requests to the same host, with 0–0.5 s jitter
    - cache hit within the page type's lifetime (listings 1 d, transactions 7 d, static 30 d)
      makes no request
    - 429/5xx back off exponentially, up to 3 retries
    - 403 or a CAPTCHA marker raises `SourceBlocked`
    - cache stored gzip under `DATA_DIR/cache/<host>/<date>/<sha1>.html.gz`
  - Notes: user agent `hk-estate-research/0.1 (personal, non-commercial)`.
  - Added during the spike: robots.txt is parsed with `protego` (stdlib `urllib.robotparser`
    ignores `*`/`$` wildcards, which Centanet, 28Hse and Spacious use), and a robots.txt
    `Crawl-delay` longer than 1 s is honoured per host (SRPE asks for 30 s). robots.txt
    answering 401/403/5xx means disallow-all; 404 means allow-all.
- [x] **Task 0.3: Source spike**
  - Files: `scripts/spike/<source>.py` (throwaway probes), `docs/sources.md`.
  - Done when: `docs/sources.md` has a row per source with all the fields named in the P0
    acceptance criterion. Sources: Centadata, Centanet listings, 28Hse (transactions,
    listings, rents), Squarefoot (+ haunted list), Spacious, house730, Midland, SRPE, RVD
    (data.gov.hk), Address Lookup Service, CSDI (MTR, buildings).
  - Notes: fetch at most ~50 pages per source. Drop any source that blocks or whose
    robots.txt forbids what we need, and record it in decisions.md. If Centadata is
    unusable, 28Hse becomes the transaction master; record that too. Pick the single-block
    pilot building (a 1990s single tower in Kowloon or Island with ≥100 deals).
  - Result (see `docs/sources.md`, decisions.md "Phase 0 source spike"):
    - Transaction master is **28Hse** (free data from 2018). Centadata only shows the latest
      deal per flat (full history is behind the disallowed API) and becomes the building
      master list + one older deal per flat.
    - Dropped: Squarefoot (+ haunted list), house730, Midland (all 403); CSDI API (robots).
      MTR distance uses station centres (MTR open data + ALS).
    - SRPE is a JS app with a token API and 30 s crawl-delay: deferred to Task 3.4.
    - Pilot single-block building: **高雲臺 Goldwin Heights** (1994, 328 flats).
    - "History from 2000" is not reachable for free: open item for the user.

### Plan changes from the Phase 0 spike
- Task 1.2: RVD files are legacy `.xls` (read with `xlrd`, now a dependency).
- Task 1.3: `<master>` = `hse28` (28Hse). Backfill uses the estate deal lists (to March 2021)
  plus per-flat pages for 2018–2021. P1 "full transaction histories" means everything 28Hse
  holds (2018 on) plus Centadata's latest deal per flat.
- Task 1.5: spot-check against the 28Hse site.
- Task 1.6: becomes the **Centadata cross-check** (latest deal per flat, block pages) instead
  of a 28Hse cross-check; Centadata-only deals go to `txn_raw` with `source='centadata'`.
- Task 2.1: kept listing sites are 28Hse, Centanet (enumerated from its listing sitemap) and
  Spacious. Task 2.4: no Squarefoot haunted list.
- Task 3.1: the master estate/building list comes from Centadata block pages (via the
  Centanet estate sitemap).
- Task 3.3: distance to the nearest MTR **station** (not exit) unless the user supplies the
  CSDI exits file.

## Phase 1 – Data foundation (pilot)

- [x] **Task 1.1: Schema and DB layer**
  - Files: `src/hkestate/db.py` (DDL + `connect()`), `docs/architecture.md`, `tests/test_db.py`.
  - Done when: tests show `init_db()` creates the tables, is idempotent, and that clean
    views drop the excluded rows.
  - Notes:
    - Tables: `estate`, `phase`, `building`, `unit`, `txn_raw` (append-only: source,
      source_id, fetched_at, all raw fields), `txn` (clean view), `listing_raw`,
      `listing_snapshot`, `rent_index`, `price_index`, `attribute`, `risk_flag`,
      `source_map` (other sources' names → master ids), `run_log`.
    - Unit key: building_id + floor_label + flat; `floor_num` derived.
    - Areas: `saleable_sqft`, `gross_sqft`, `area_basis` (`saleable`, `converted` or
      `gross_only`).
    - Dates: `instrument_date`, `registration_date`.
    - Exclusion flags per decisions.md: under HK$500k, 5-MAD outlier, bulk deals,
      non-market transfers, first-hand.
    - Added while building: `market_stat` (RVD completions, stock, vacancy, take-up),
      `run_progress` (finished steps, for resuming), views `txn_latest` → `txn_all` (flags)
      → `txn` (clean), and an `excl_duplicate` flag (the same sale from an agency report or
      a second source). Rules in `docs/architecture.md`.
- [x] **Task 1.2: RVD loader**
  - Result (2026-10-16): `hkestate rvd` loads 8 series. Monthly: price index 3,232, rent
    index 3,232, popular developments 3,744, average prices 4,950, average rents 4,973
    values, all to Aug 2026. Annual (RVD publishes these yearly, so the 3-month test
    doesn't apply): completions 252 (2026 provisional to date), stock 246 and vacancy 246
    (to year-end 2025). Take-up is not loaded (different layout, not needed yet). Fixture
    CSVs are trimmed rows of the real sheets; a moved header column stops the load.
  - Files: `src/hkestate/sources/rvd.py`, `tests/test_rvd.py` (fixture CSV).
  - Done when:
    - parser test passes
    - a live run loads monthly price and rent indices by class, popular developments, and
      completions and stock
    - `SELECT count(*)` per series > 0, and the latest month is within 3 months of today.
- [x] **Task 1.3: Transaction scraper (master source)**
  - Files: `src/hkestate/sources/<master>.py`, `src/hkestate/normalise.py` (floor labels,
    TC/SC, areas), `tests/test_<master>.py`, `tests/test_normalise.py`, trimmed fixtures in
    `tests/fixtures/`.
  - Done when: parser and normaliser tests pass (skipped floors, G/F, podium, 頂層,
    gross-only conversion).
  - Found while building: 28Hse deal lists mix Land Registry deals (linked to a flat) and
    agency-reported "market" deals (linked to a deal id, often only a floor band such as
    高層). Market deals are stored too and dropped as duplicates once the registry deal
    for the same building + flat + price (±1%, 90 days) arrives. Some deals are marked
    連車位 (price includes a parking space); kept in `raw_json` for now (open item).
- [ ] **Task 1.4: Pilot backfill, idempotent**
  - Status 2026-10-16 12:30: Taikoo Shing, Mei Foo, City One and Goldwin Heights loaded;
    LOHAS Park (22 developments on 28Hse) loading, 7 of 22. Tests for rerun/resume pass.
    28Hse rate-limits (429) long runs: requests now every 4 s with 15-minute cool-downs,
    which makes the pilot load take ~12 h instead of ~3.
  - Files: `src/hkestate/cli.py` (`hkestate backfill --estates <list>`), `tests/test_backfill.py`.
  - Done when:
    - backfill loads all 5 pilot estates
    - a second run adds 0 rows to `txn` (the test proves this on fixtures, the live run
      confirms it)
    - an interrupted run resumes from `run_log`.
- [ ] **Task 1.5: Spot-check**
  - Files: `scripts/spot_check.py`, result note in `docs/sources.md`.
  - Done when: 50 random pilot rows match the live site on price, date, floor, flat and area
    (50/50). Mismatches are fixed or explained.
- [ ] **Task 1.6: Second-source cross-check (Centadata, per the Phase 0 change)**
  - Files: `src/hkestate/sources/centadata.py`, `tests/test_centadata.py`.
  - Done when: for the pilot estates, a report (`data/reports/crosscheck_<date>.md`) shows
    the match rate of Centadata deals (2018 on) to 28Hse deals; Centadata-only deals are in
    `txn_raw` with `source='centadata'`.
  - Status: done for 4 of 5 estates (93.8–99.6%, see `docs/sources.md`); LOHAS Park after
    its load.

## Phase 2 – Listings (pilot)

- [ ] **Task 2.1: Listing scrapers**: one module per kept site, under `sources/`.
  - Done when:
    - parser tests on fixtures pass
    - a live run stores the pilot estates' listings in `listing_raw`
    - no agent names or phones are stored (a test asserts the columns don't exist).
  - Notes: search-result pages daily; detail pages only for new listings or a changed
    price.
- [ ] **Task 2.2: Matching**
  - Files: `src/hkestate/match.py`, `data/` manual map CSV (path in config), tests.
  - Done when: tests cover name fuzz, TC/SC, floor-band mapping and cross-site dedupe
    (building + band + area ±2%), and ≥80% of live pilot listings match a building.
- [ ] **Task 2.3: Snapshots and delisting**
  - Done when: tests show one snapshot per listing per HK day, delisted after 3 missed days,
    and a relisting at a new price becomes a new snapshot of the same listing.
- [ ] **Task 2.4: Risk-flag keywords**
  - Files: `src/hkestate/flags.py`, `src/hkestate/keywords.yml`, tests.
  - Done when: tests pass for the seed terms, including negation cases ("非凶宅").
  - Notes: seed terms:
    - 凶宅 / haunted
    - 連租約 / 連租 / tenanted
    - 海景 / sea view
    - 全新裝修 / renovated
    - 僭建 / illegal structure
    - 開放式 / studio
  - Squarefoot's haunted list is added as a flag source if Phase 0 kept it.
- [ ] **Task 2.5: Daily job**
  - Files: `scripts/daily.ps1`, `scripts/register_task.ps1`, `docs/operations.md`.
  - Done when:
    - the task is registered (daily 08:00, run after a missed start)
    - a manual run writes `run_log` and a log file
    - a weekly transaction refresh is registered as well.

## Phase 3 – Territory-wide scale and enrichment

- [ ] **Task 3.1: Master estate/building list**
  - Done when: territory-wide estate and building lists load (count recorded in
    `docs/sources.md`), with HOS, TPS, villas and village houses excluded by rule, and a
    test checks the rule.
- [ ] **Task 3.2: Full backfill**
  - Done when: the backfill completes, resuming across runs, and `scripts/coverage.py`
    writes per-estate coverage (first deal year, deal count) to `data/reports/coverage.csv`.
  - Notes: run it in the background in chunks and record elapsed time.
- [ ] **Task 3.3: Geocoding and location attributes**
  - Done when:
    - each building has coordinates from the Address Lookup Service (≥95%)
    - each building has walking-proxy distance to the nearest MTR exit (straight-line, in
      metres)
    - each building has its district and building age (from the first deal or the occupation
      year where a source gives it).
- [ ] **Task 3.4: SRPE first-hand sales**
  - Done when: first-hand transactions for developments since the portal began are stored
    and flagged `first_hand`, with price-list facing and size attributes attached to units
    where available.
- [ ] **Task 3.5: Territory-wide listings**
  - Done when: the daily job covers all estates. One run's elapsed time is under 1 hour, or
    the split across the day is documented.

## Phase 4 – Fair price and dashboard

- [ ] **Task 4.1: Feature table**
  - Done when: `features.py` builds one row per deal and per listing, and tests check feature
    definitions:
    - saleable area, rooms, floor_num and relative floor (floor ÷ building top floor)
    - facing bins: N, NE, E, SE, S, SW, W, NW, unknown
    - view flags, age, district, MTR distance, estate size, first-hand flag
    - month index.
- [ ] **Task 4.2: Fair-price model**
  - Done when:
    - LightGBM on log price per sq ft, trained on deals up to month T−6 and tested on the
      last 6 months
    - the out-of-time median absolute % error is written to `data/reports/model_eval.md`
    - a same-building comparables check is computed alongside
    - conformal 80% intervals
    - `shap` values are stored per prediction.
- [ ] **Task 4.3: Asking-vs-deal gap**
  - Done when: pairs of a listing and a deal on the same unit within 90 days are counted per
    district. Where a district has ≥30 pairs, its median gap is used. Otherwise 5%, marked
    `assumed` in the output.
- [ ] **Task 4.4: Ranking output**
  - Done when: `hkestate rank` writes `data/reports/ranked_listings_<date>.csv` sorted by %
    discount, with fair price, interval, confidence (low if <10 comparables in the last 24
    months), flags and the top 3 SHAP reasons.
- [ ] **Task 4.5: Dashboard**
  - Files: `app/dashboard.py`.
  - Done when: launched with `streamlit run`; the ranked list, filters (district, price,
    size, flags, confidence), and a detail page (comparables, unit price history, SHAP
    reasons, listing link) render and are checked in the browser; the run-failure banner
    shows when the last `run_log` entry failed.

## Phase 5 – Growth study and report

- [ ] **Task 5.1: Repeat-sales pairs**
  - Done when: tests show pairs are built only from clean deals on the same unit held ≥3
    years, with first-hand starts tagged.
- [ ] **Task 5.2: Costs and rents**
  - Files: `src/hkestate/costs.py` (dated stamp-duty table from IRD pages, with sources
    cited in code comments), tests for known amounts at several dates.
  - Done when:
    - the cost tests pass
    - net growth subtracts round-trip costs (agent 1% on each side, legal 0.3%, stamp duty
      at the deal date)
    - total return adds rental yield from RVD rent indices, anchored to current rental
      listings.
- [ ] **Task 5.3: Attribute regression and indices**
  - Done when:
    - annualised growth is regressed on attributes with district, age, start-period and
      start-price controls
    - a monthly repeat-sales index per attribute group gives drawdown
    - out-of-sample checks: pairs ending ≤2018 vs later, and a district holdout
    - an attribute is marked "robust" only if significant (p<0.05) and the same sign in both
      checks.
- [ ] **Task 5.4: Findings report**
  - Done when: `hkestate report` writes `data/reports/findings_<date>.md`. It contains:
    - a plain-language summary
    - per-attribute growth, total return and drawdown tables with charts
    - the robust/not-robust verdicts
    - first-hand vs second-hand results
    - data coverage and limits.
- [ ] **Task 5.5: Notebook**
  - Done when: `notebooks/explore.ipynb` reproduces the key tables from the DB, and
    nbstripout is installed as a git filter (outputs stripped on commit).
