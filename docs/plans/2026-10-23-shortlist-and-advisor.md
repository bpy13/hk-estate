# Plan: a usable bargain list, better location data, and a decision helper (2026-10-23)

**Goal.** Two things.
1. **Part A:** fix what limits today's results.
   - The ranked listings start with data errors, not bargains.
   - Only 1,313 of 41,474 buildings (3%) have map coordinates, so MTR distance is known for
     about 10% of clean sales.
   - A few data-quality leftovers touch the growth study.
   - The findings report's word "pairs" confuses readers.
2. **Part B:** a decision helper.
   - A user says what they want to do: buy, rent, sell or let.
   - They say why: investment, living near a place, a primary school for their children, space
     within a budget.
   - The helper answers with districts, estates and listings that fit, and buy-vs-rent or
     sell-vs-let figures.
   - It also has a "market today" view.
   - It is designed as if for the public (plain questions, phone-sized screens).
   - It runs privately in the existing Streamlit dashboard, which opens in a phone browser.

**What the 2026-10-22 ranking shows (probe of `ranked_listings_20261022.csv`, 29,744 rows).**
- Rank 1 is a 15,666 sq ft flat (an area typo).
- Ranks 2, 3, 8, 9, 18 and 19 are subsidised courts (景翠苑, 銀蔚苑, 豐盛苑).
  Subsidised flats are outside the project's coverage.
- Ranks 5/6, 10/11 and 14/15 are the same flat listed twice.
- 441 listings claim a discount over 20%; 103 claim over 30%.
- 12,025 of the 29,744 rows have `low` confidence.

**Data we already have for Part B.**
- 8,268 active rent listings.
- 44,924 active sale listings.
- The fair-price model, the growth study results, and stamp duty and fee rules (`costs.py`).

**Data we lack for Part B.**
- School nets.
- Commute times.
- Mortgage rules.
- Coordinates for most buildings (Phase 2 fixes this).

**Approach.** Phases run in order.
1. Clean the bargain list.
2. Geocode every building and fix the data leftovers. Geocoding means getting each building's
   coordinates from the government's free Address Lookup Service (ALS).
3. Reword the report.
4. Collect the decision helper's data.
5. Build its calculations.
6. Build its dashboard pages.

Background jobs keep running throughout: daily listings, backfill-territory, the watchdog and
scheduled tasks.

**Defaults for the open questions of 2026-10-22** (offered then, not yet answered; the
user's go-ahead on this plan accepts them unless changed).
1. Who uses it: designed for the public, running privately in the dashboard. No public app.
2. Decisions covered: buy, rent, sell and let. Buy gets the most detail.
3. Goals covered: investment, near a place, primary schools, space and budget.
4. Schools: primary school nets only, from the Education Bureau's open data.
5. Commute: a free estimate. It adds walking time to the nearest MTR station and MTR
   station-to-station time. No paid routing service.
6. Mortgage: the HKMA loan-to-value caps and stress test, plus a monthly payment at a rate the
   user sets.
7. Rents: estimates use the rent listings we already collect (8,268). Extending rental
   collection is not in this plan (see open-items).
8. A "market today" view is built as part of the helper.

**Constraints** (verbatim from `docs/decisions.md`):
- **Purpose: the owner's own investment research** (2026-10-16). Not a product, no clients,
  single user. Keeps scraping in personal-use territory and polish modest.
- **Coverage: whole territory, private residential flats only** (2026-10-16). Excludes HOS /
  subsidised flats (resale restrictions distort prices), houses/villas, commercial.
- **Data budget: HK$0** (2026-10-16). No paid APIs (e.g. Anysite) and no IRIS searches unless
  the owner approves a specific spend.
- **Collection method: slow, polite scraping** (2026-10-16): obey robots.txt, ≤1 request/s per
  site, raw-page cache so nothing is fetched twice, resumable runs. Never call disallowed
  paths (e.g. centanet `/centanetapi/api/`).
- **Deliverables: analysis notebooks, a local Streamlit dashboard listing ranked deals, and a
  written findings report** (2026-10-16).
- **Language: English reports, Chinese estate/building names kept alongside** (2026-10-16).
- **Public GitHub repo `hk-estate` on account `bpy13`** (2026-10-16). Because it is public:
  scraped data, the database and raw-page cache are never committed (`data/` ignored), and no
  credentials or personal data appear in code or docs.

The decision helper stays inside "single user": the owner uses it, and it is designed so it
could later serve others. Going public is on hold (user, 2026-10-23: monetisation on hold).

**Shared resources: rules for every helper.**
- Running scrapers use the live database `data\hkestate.duckdb`.
  - **Never open it for more than a few seconds.**
  - Work from `--from-snapshot` (`data\model\snapshot\`) or from your own copy of the
    database in your scratch folder.
  - Never run `hkestate status` or any `txn`/`txn_all` query on the live file.
- Only the main session writes to the live database (geocoding, district fill, school nets).
- Don't start, stop or restart scheduled tasks or background jobs.
- Dashboard checks use port 8531, never the default 8501. Stop it afterwards (by port).
- `src/hkestate/cli.py` is shared. Each task adds only its own subcommand or option.

**Acceptance.**
- A1. The top 50 of a fresh `hkestate rank` hold no subsidised estate, no area outside
  150-5,000 sq ft, and no flat listed twice. A manual check of 20 of them against the
  listing page finds no data error. `shortlist_<date>.md` lists the top 50 with reasons.
- A2. At least 80% of clean sales have an MTR distance. Coordinates come from ALS. Fewer
  than 2% of clean sales lack a district.
- A3. The cheap-sale rule and the SRPE parser fix are in. The unused `QUARANTINE_SQL` guard is
  gone. Retrain, rank and `report --settings standard` are rerun on the new data.
- A4. The findings report and the dashboard's growth page carry no "pair"/"pairs" in
  user-facing text. They use "flats sold twice", "model-valued flats" and "Ricacorp reported
  gains". Both carry the sentence saying model-valued flats understate differences.
- B1. Every geocoded building has a primary school net. A commute estimate exists between
  any building and any MTR station. A rent estimate exists for any flat. A dated, cited
  mortgage-rules table exists.
- B2. Given a user's profile, `advisor.py` returns ranked estates and listings with reasons.
  It also returns buy-vs-rent and sell-vs-let figures over the user's horizon. Tests check
  worked examples by hand.
- B3. The dashboard has "Plan a move" and "Market today" pages. Both work in a real browser at
  390 px width (phone size).

## Phase 1 – Usable bargain list (sonnet; Task 1 fable)

- [ ] **Task 1: diagnose the top of the ranking (fable)** `[parallel-ok]`
  - Files: create `docs/ranking-diagnosis.md`. Read only `data\model\snapshot\` and
    `data\reports\ranked_listings_20261022.csv`.
  - Done when: the top 200 rows are each put in one cause class, with counts per class:
    - subsidised estate
    - area typo
    - saleable vs gross area mixed up
    - duplicate listing
    - wrong building match
    - few comparable sales
    - first-hand prices in the comparable sales
    - plausible bargain

    For each class, the file names the field that detects it and the rule proposed.
  - Notes: check why `normal`-confidence rows such as 御海灣 (asking HK$9,000/sq ft against
    comparable sales at HK$16,901) rank high. If comparable sales or fair prices mix in
    first-hand or gross-area prices, say so with numbers. Tasks 2-3 follow this file. Where
    it contradicts them, update the plan.
- [ ] **Task 2: listing exclusions**
  - Files: modify `src/hkestate/rank.py` and `src/hkestate/features.py` (`listing_features`).
    Test `tests/test_rank.py`.
  - Done when: the tests are written first and fail before the change. They cover:
    - a listing in a subsidised estate is left out of the ranking with reason `subsidised`
    - an area under 150 or over 5,000 sq ft gets `check_area`
    - an asking price per sq ft under 0.4× or over 2.5× the building's index-adjusted median
      gets `check_price`

    Flagged rows go to the end with their reason in a new `exclude_reason` column, not
    deleted. Full pytest and ruff green.
  - Notes:
    - Subsidised comes from the master list's own exclusion (`buildings.me_excluded` /
      `exclude_reason`) where present. Otherwise use Task 1's rule (e.g. HOS court names
      ending in 苑 with no private sale history).
    - Thresholds may change after Task 1. Record the final values in decisions.md.
- [ ] **Task 3: collapse duplicate listings**
  - Files: modify `src/hkestate/rank.py`. Test `tests/test_rank.py`.
  - Done when: a test shows two listings with the same building, floor band, flat (or both
    blank), area within 2% and price within 2% become one row. The new `also_listed` column
    names the other. Applies across and within sources. Green.
- [ ] **Task 4: shortlist file and dashboard default**
  - Files: modify `src/hkestate/rank.py` (`write`), `src/hkestate/dashboard_data.py` and
    `app/dashboard.py`. Tests in `tests/test_rank.py` and `tests/test_dashboard.py`.
  - Done when:
    - `hkestate rank --from-snapshot` writes `data/reports/shortlist_<date>.md`: the top 50
      rows with no exclusion, each with estate, flat, asking, fair range, discount, top 3
      reasons, flags and URL
    - the dashboard's ranked list hides excluded rows by default (a checkbox shows them)
    - a dashboard check on port 8531 shows both
- [ ] **Task 5 (main session): real run and manual check**
  - Run `hkestate rank --from-snapshot`. Open 20 shortlist URLs.
  - Done when: acceptance A1 holds. The result (data errors found, if any) is noted in this
    plan.

## Phase 2 – Location coverage and data leftovers (sonnet; Task 8 fable)

- [ ] **Task 6 (main session): geocode every master building (long job)**
  - Run the existing resumable `geo.geocode_buildings` in the background through ALS. It
    reads about 40,000 buildings at ALS's polite pace, roughly 11-12 hours at 1 s. Then rerun
    the MTR distance step (`python -m hkestate.geo`). Check command: `geo_status` counts in
    the log.
  - Done when: at least 90% of master buildings have `geo_status` set. The share with
    coordinates is reported. Tasks 7-10 run meanwhile.
  - Notes: ALS is a government service, so record its host and pace in `docs/sources.md`.
    Use a new lock `geo`. Never hold the database while waiting on ALS (short sessions per
    batch, as the master pass does).
- [ ] **Task 7: fill missing districts** `[parallel-ok]`
  - Files: modify `src/hkestate/geo.py`. Test `tests/test_geo.py`.
  - Done when:
    - a test shows a building with no district gets one: from its ALS answer, or from the
      nearest geocoded building in the same estate
    - a function writes it to `master_building`
    - run on the live database by the main session after Task 6: under 2% of clean sales
      lack a district

    Green.
- [ ] **Task 8: cheap-sale rule (fable)** `[parallel-ok]`
  - Files: modify `src/hkestate/db.py` (`txn_all_sql`) or `src/hkestate/features.py`
    (`exclude_reason`, whichever the price check lives in). Tests in `tests/test_db.py` /
    `tests/test_features.py`. Update `docs/decisions.md` (proposal marked Claude's call).
  - Done when:
    - a sample of 50 of the 2,134 clean sales under HK$1,000/sq ft is classified (real old
      price, part share, area error), recorded in the task notes
    - a rule is chosen, e.g. under 0.25× the district's index-adjusted median per sq ft for
      that month
    - the rule is tested first, then implemented
    - it removes most of the non-real ones and under 10% of the real ones in the sample

    Green.
- [ ] **Task 9: SRPE glued prices** `[parallel-ok]`
  - Files: modify `src/hkestate/sources/srpe.py`. Test `tests/test_srpe.py`.
  - Done when: a test built from row `3085:3 3:2 5:A A:2016-12-03` (HK$728,100,014,452,000)
    gives one row per flat with one price each. Existing SRPE tests pass. Green.
- [ ] **Task 10: remove `QUARANTINE_SQL`** `[parallel-ok]`
  - Files: modify `src/hkestate/db.py`, `src/hkestate/features.py`, `src/hkestate/study.py`
    and their tests, plus `docs/architecture.md`.
  - Done when: `rg QUARANTINE_SQL src tests` finds nothing. The `excl_quarantine` flag and its
    report line go too. Green.
  - Notes: Task 8 also edits `db.py` and `features.py`. Run Task 10 after Task 8, not in
    parallel.
- [ ] **Task 11 (main session): retrain, rank and report on the new data**
  - After Tasks 6-10, take a fresh snapshot (`hkestate train` reads the database once).
    Then run `rank --from-snapshot` and `report --settings standard --from-snapshot`.
  - Done when: acceptance A2 and A3 hold. `model_eval.md` compares with the 2026-10-21
    model. The findings report shows MTR coverage at 80%+.

## Phase 3 – Report wording (sonnet)

- [ ] **Task 12: rename "pairs" and add the caveat**
  - Files: modify `src/hkestate/report.py`, `src/hkestate/charts.py`, `app/dashboard.py`,
    `src/hkestate/dashboard_data.py` (labels only) and `README.md`. Tests in
    `tests/test_report.py` and `tests/test_dashboard.py`.
  - Done when:
    - a test renders the report from the test fixtures and finds no `pair` (any case) outside
      code identifiers and file names
    - the words are "flats sold twice" (short: "sold twice"), "model-valued flats" (short:
      "model-valued") and "Ricacorp reported gains"
    - the "How it was done" text carries: "For flats whose last sale was before 2018, we
      compare that sale with the model's estimate of the flat's value today. The model
      learned partly from that same old sale, so its estimate leans back towards the old
      price. This hides part of any real difference between kinds of flat: when this check
      shows 'no effect', trust it less than the result from flats sold twice."
    - the summary line ends "(this check tends to understate differences; see How it was
      done)"
    - a dashboard check on port 8531 shows the new labels

    Green.
  - Notes: code names (`pairs.py`, column names) stay. Only text people read changes.

## Phase 4 – Decision helper data (sonnet)

- [ ] **Task 13: primary school nets** `[parallel-ok]`
  - Files: create `src/hkestate/sources/schools.py` and `tests/test_schools.py`, with a
    fixture.
  - Done when:
    - the source is probed and named in `docs/sources.md`. First choice: the Education
      Bureau's school net boundaries on the government's open data portals (DATA.GOV.HK /
      CSDI). Fallback: the EDB school location list with each school's net.
    - a parser gives net boundaries, or schools with net and coordinates
    - a function assigns each geocoded building its net (point in boundary). In the
      fallback, it assigns the net most common among the 3 nearest schools.
    - a fixture test covers it

    Green.
  - Notes: if neither source gives nets with coordinates, record it in open-items and use
    the nearest 5 primary schools within 1 km instead. Run against the live database
    happens in Task 17.
- [ ] **Task 14: commute estimate** `[parallel-ok]`
  - Files: create `src/hkestate/commute.py` and `tests/test_commute.py`.
  - Done when:
    - `minutes(building, station)` returns walk time plus train time
    - walk = straight line × 1.3 at 80 m a minute, to the 2 nearest stations
    - train = stops × 2 min + 5 min per change of line, over the MTR line graph from the open
      data `mtr_lines_and_stations.csv` already used in `geo.py`
    - tests: Kowloon Tong to Admiralty works out as expected by hand, and a change of line
      is counted

    Green.
  - Notes: if MTR open data publishes real station-to-station times, use them instead and
    say so in `docs/sources.md`.
- [ ] **Task 15: rent estimate** `[parallel-ok]`
  - Files: create `src/hkestate/rent.py` and `tests/test_rent.py`.
  - Done when:
    - `estimate(flat)` returns a monthly rent and its basis, from the first level with 5+
      active rent listings: building, then estate, then district
    - the basis level is median rent per sq ft × area, adjusted by the RVD rent index to the
      current month
    - otherwise the RVD average rent for the size class and region
    - the asking-to-achieved discount is the same anchor `costs.py` uses
    - tests cover each fallback level

    Green.
- [ ] **Task 16: mortgage rules** `[parallel-ok]`
  - Files: create `src/hkestate/mortgage.py` and `tests/test_mortgage.py`.
  - Done when:
    - the current HKMA loan-to-value caps and stress-test rule are read on HKMA's own pages
      and entered as a dated table with source links, like `costs.py`'s stamp duty
    - `max_loan(price, buyer)`, `monthly_payment(loan, rate, years)` and `affordable(price,
      income, rate)` exist
    - tests check one hand-worked example each

    Green.
  - Notes: if HKMA pages and press reports disagree, use HKMA and note the difference in
    open-items. Buyer types: first home vs owns another flat; salaried. Mortgage insurance
    (above the cap) appears as one line with its source, not modelled in detail.
- [ ] **Task 17 (main session): load school nets on the live database**
  - Done when: acceptance B1's school-net part holds (share of geocoded buildings with a net
    reported).

## Phase 5 – Decision helper calculations (fable)

- [ ] **Task 18: user profile and scoring**
  - Files: create `src/hkestate/advisor.py` and `tests/test_advisor.py`.
  - Done when:
    - a `Profile` holds the user's inputs:
      - intent (buy / rent / sell / let)
      - goal weights (investment, near a place, school, space)
      - budget or monthly rent limit
      - minimum size and rooms
      - target places (MTR stations)
      - longest commute in minutes
      - school net
      - years ahead
    - `recommend(profile, snapshot)` returns estates, then listings within them
    - each result shows a score per goal and a one-line reason per goal, e.g. "32 min to
      Admiralty; net 34; 9% below fair price; district grew 4.1%/yr"
    - investment uses the growth study's district and attribute results, rent yield and
      the listing discount
    - living uses commute, school net, size and building age
    - hard limits filter first (budget, size, commute, net), then weights rank
    - tests: two contrasting profiles (an investor and a family) on a small fixture give
      different, explainable orders

    Green.
  - Notes: the scores are simple and readable (0-100 per goal, a weighted average). Write
    the weighting in decisions.md as Claude's call.
- [ ] **Task 19: buy vs rent, sell vs let**
  - Files: modify `src/hkestate/advisor.py`. Test `tests/test_advisor.py`.
  - Done when: two functions exist, and a test checks each by hand on one example.
    - `buy_vs_rent(price, rent, years, rate, growth)` gives the total cost of each path.
      Buying counts deposit, stamp duty, fees, mortgage interest, rates and management
      fees, and the sale at the end at assumed growth. Renting counts rent plus what the
      deposit would earn.
    - `sell_vs_let(value, rent, years, growth)` gives the cash from selling now against
      letting for N years and selling then.

    Growth defaults to the district's long-run rate from the growth study, with a low/high
    range from its 25th/75th percentiles.
- [ ] **Task 20: market today summary** `[parallel-ok]`
  - Files: create `src/hkestate/market.py` and `tests/test_market.py`.
  - Done when: `summary(snapshot)` gives these per district and size class, and a fixture
    test checks one district's figures. Green.
    - median price per sq ft over the last 12 months
    - change over 3 and 12 months (RVD index and our own sales)
    - number of sales
    - active sale and rent listings
    - median asking vs recent deals

## Phase 6 – Dashboard pages (sonnet)

- [ ] **Task 21: "Plan a move" page**
  - Files: modify `app/dashboard.py` and `src/hkestate/dashboard_data.py`. Test
    `tests/test_dashboard.py`.
  - Done when:
    - a short questionnaire (intent, goals, budget, places, school net, years) shows results
      as cards: estates, then listings, with reasons and the buy-vs-rent or sell-vs-let
      figures
    - a real browser check on port 8531 at 390 px and at desktop width works with both
      profiles from Task 18, with no sideways scrolling
- [ ] **Task 22: "Market today" page** `[parallel-ok]` with Task 23
  - Files: modify `app/dashboard.py` (its own page function) and
    `src/hkestate/dashboard_data.py`.
  - Done when: a district table and a trend chart show from `market.summary`, checked in a
    browser at 390 px.
  - Notes: shares files with Task 21. Run after it, not in parallel.
- [ ] **Task 23: docs** `[parallel-ok]`
  - Files: `README.md`, `docs/architecture.md`, `docs/sources.md` and the repo `CLAUDE.md`
    (commands).
  - Done when: the new commands, modules, sources (ALS, school nets, HKMA) and pages are
    described. Links resolve.
