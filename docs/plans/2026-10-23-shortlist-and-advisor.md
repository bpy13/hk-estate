# Plan: a usable bargain list, better location data, and a decision helper (2026-10-23)

## Hand-over for the next session (written 2026-10-23 ~04:30; update when helpers finish)

**Model rule (user, 2026-10-23):** wherever a skill or this plan says Fable, use Opus
(`model: "opus"`), including the end-of-run pre-release reviewer. Sonnet stays Sonnet.
Reason: the Fable usage limit.

**The user's go-ahead covers every phase of this plan.** The user paused the run on
2026-10-23 at ~04:30 to move to a fresh session. Carry on from here without asking again.

**First, check what landed:** run `git log --oneline -15` and `git status -s`, then re-read
this plan and `docs/open-items.md`.

**Still to do, in order:**
1. **Task 18b + 19 (advisor fixes; buy vs rent, sell vs let).**
   - A helper was running at the hand-over.
   - If its ticks below are missing, start a fresh Opus helper for what's left.
   - Find what's left from `git log` and `git diff`, and keep any committed work.
2. **Task 22 ("Market today" page).**
   - A Sonnet helper was running at the hand-over.
   - If it's unticked, start a fresh Sonnet helper for what's left.
   - Then do a real-browser check at 390 px width.
3. **Task 21 ("Plan a move" page).** Sonnet. Starts after 19 and 22, because they share
   `app/dashboard.py`.
4. **Geocoding (Task 6), main session.**
   - Process 16964 (`python -m hkestate.geo`) was started 01:19 on 10-23.
   - Log: `data\logs\geo_20261023_011933.log`. Expected end: ~16:30 on 10-23.
   - Check it with `Get-Content data\logs\geo_20261023_011933.log -Tail 2` and
     `Get-Process -Id 16964`.
   - If it died, rerun `python -m hkestate.geo` in its own process. It resumes where it
     stopped.
   - When it ends:
     - (a) Spot-check 30 geocoded buildings: ALS `geo_district` against the estate's
       district. Every lookup so far says "ok", and ALS always returns its closest guess.
     - (b) Run `python -m hkestate.geo --stations-only`. This sets MTR distances and runs
       `fill_districts` (Task 7's live part).
     - (c) Report coverage against acceptance A2.
5. **Task 17: load school nets.**
   - `sources/schools.load(fetcher, db_path)` is not wired into the CLI yet. Add a
     `python -m hkestate.sources.schools` entry or a CLI subcommand.
   - Run it after geocoding, in short database sessions.
6. **Task 11: retrain, rank and report.**
   - Run `hkestate train` (fresh snapshot), then `rank --from-snapshot`, then
     `report --settings standard --from-snapshot`.
   - This applies the `low_psf` rule (Task 8), the 72-month first-hand window (8b) and the
     geocoded MTR distances.
7. **Task 5: manual check.**
   - Open 20 URLs from the new `data\reports\shortlist_<date>.md`.
   - Check that 御海灣 and Grand Yoho no longer top it.
8. **Task 23: docs.** README, architecture, CLAUDE.md commands. `docs/sources.md` already has
   the EDB and HKMA entries.
9. **End of run:**
   - The pre-release reviewer (Opus) on this run's commits: `7170056..HEAD`.
   - Fix plain bugs, then wrap-up and ship.

**Rules that still apply:**
- Never hold the live database for more than a few seconds.
- Dashboard checks use port 8531.
- Don't start or stop the scheduled jobs.
- Waiting on the user (don't act alone): delete the stored SRPE glued rows (open-items);
  register the scheduled tasks.

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
*(Task 1 done b39f27f. Top 200: plausible bargain 105, few comps 39, duplicate 17, wrong
match 13, saleable/gross or two flats 10, first-hand in comps 8, area typo 6, subsidised 2.
Checked 御海灣 in the main session: since 2024-10, 22 first-ever sales at median HK$20,033/sq
ft vs 20 second sales at HK$15,044. The master list's `me_excluded` is False for every
building, so it finds no subsidised estate. Tasks 2-3 below follow
`docs/ranking-diagnosis.md` section 4.)*
- [x] **Task 2: listing checks** (Tasks 2 and 3 go to one helper, in order)
  - Files: modify `src/hkestate/rank.py` and `src/hkestate/features.py` (`listing_features`
    only). Create `src/hkestate/subsidised_estates.csv` (tracked). Test `tests/test_rank.py`.
  - Done when: tests are written first and fail before the change. Each rule from
    `docs/ranking-diagnosis.md` gets a reason in a new `exclude_reason` column:
    - `subsidised`: estate in `subsidised_estates.csv`, seeded with 銀蔚苑 and any others
      the diagnosis names
    - `check_subsidised`: an estate named …苑 or …邨 asking under 0.7× the district median
    - `check_area`: under 150 or over 5,000 sq ft, or 1.5× or more the nearest recorded unit
      area (3+ recorded areas; not for 複式/天台/相鄰)
    - `check_area_basis`: 1.12-1.45× the flat's (or nearest) recorded saleable area, which
      looks like a gross area. `check_combined` for 相鄰 (two flats in one listing).
    - `check_price`: asking per sq ft under 0.6× or over 2.5× the building's index-adjusted
      median
    - `check_model`: 5+ comps and fair price over 1.25× the comps median
    - `below_all_recent`: an information flag only, not an exclusion

    Excluded rows go to the end, never deleted. Full pytest and ruff green.
  - Notes: record the final thresholds in decisions.md as Claude's call (from the
    diagnosis).
- [x] **Task 2b: stop wrong `contains` matches** `[parallel-ok]`
  - Files: modify `src/hkestate/match.py`. Test `tests/test_match.py`.
  - Done when: tests written first show two things. First, text that continues an estate
    name with another Chinese character (逸瓏灣 against estate 逸瓏) is no match. Second, text
    naming a block the estate doesn't have gets match method `check_match`. Existing match
    tests pass. Green.
  - Notes: rematching stored listings happens in the next daily run. Task 5 notes how many
    listings changed.
- [x] **Task 2c: rematch already-matched territory listings** *(added after Task 2b: the
  territory rematch in `listing_run.py` (~line 826) only touches rows with no building, so
  the wrong matches 2b now rejects, e.g. 逸瓏灣, keep their old building.)*
  - Files: modify `src/hkestate/listing_run.py`. Test `tests/test_territory_listings.py`.
  - Done when: a test written first shows a territory listing matched by `contains` or
    `single` to a building the new rules reject loses that building (or gets `check_match`)
    at the next rematch. Rows matched `manual` or `exact` are left alone. Green.
  - Notes: no direct database edit. The next daily run applies it.
- [x] **Task 3: collapse duplicate listings**
  - Files: modify `src/hkestate/rank.py`. Test `tests/test_rank.py`.
  - Done when: a test shows two listings become one row when they share building, area
    within 2% and price within 2%, and their floor band and flat are equal **or either is
    blank**. The new `also_listed` column names the other. Applies across and within
    sources. Green.
- [x] **Task 4: shortlist file and dashboard default**
  - Files: modify `src/hkestate/rank.py` (`write`), `src/hkestate/dashboard_data.py` and
    `app/dashboard.py`. Tests in `tests/test_rank.py` and `tests/test_dashboard.py`.
  - Done when:
    - `hkestate rank --from-snapshot` writes `data/reports/shortlist_<date>.md`: the top 50
      rows with no exclusion, each with estate, flat, asking, fair range, discount, top 3
      reasons, flags and URL
    - the dashboard's ranked list hides excluded rows by default (a checkbox shows them)
    - a dashboard check on port 8531 shows both
- [ ] **Task 5 (main session): real run and manual check** *(first run 2026-10-23 02:59 on
  the 10-22 snapshot and the 10-21 model: 24,983 ranked, 5,233 duplicates merged, 598
  excluded. 14,661 active sale listings have no building match, so they are not ranked.
  Shortlist ranks 2-3 are 御海灣 and rank 4 is Grand Yoho: newer estates whose fair price is
  still inflated by first-hand sales. Task 8b fixes that only at the Task 11 retrain, so the
  manual check of 20 URLs moves to after Task 11.)*
  - Run `hkestate rank --from-snapshot`. Open 20 shortlist URLs.
  - Done when: acceptance A1 holds. The result (data errors found, if any) is noted in this
    plan.

## Phase 2 – Location coverage and data leftovers (sonnet; Task 8 fable)

- [ ] **Task 6 (main session): geocode every master building (long job)** *(started
  2026-10-23 01:19, PID 16964, log `data\logs\geo_20261023_011933.log`; 40,406 buildings at
  ~35 a minute, so about 19 hours, ending around 20:00 on 10-23.)*
  - Run the existing resumable `geo.geocode_buildings` in the background through ALS. It
    reads about 40,000 buildings at ALS's polite pace, roughly 11-12 hours at 1 s. Then rerun
    the MTR distance step (`python -m hkestate.geo`). Check command: `geo_status` counts in
    the log.
  - Done when: at least 90% of master buildings have `geo_status` set. The share with
    coordinates is reported. Tasks 7-10 run meanwhile.
  - Notes: ALS is a government service, so record its host and pace in `docs/sources.md`.
    Use a new lock `geo`. Never hold the database while waiting on ALS (short sessions per
    batch, as the master pass does).
- [ ] **Task 7: fill missing districts** `[parallel-ok]` *(code and tests done 570bd54,
  `geo.fill_districts`; live run pending: after the geocoding job, run
  `python -m hkestate.geo --stations-only`, which now calls it.)*
  - Files: modify `src/hkestate/geo.py`. Test `tests/test_geo.py`.
  - Done when:
    - a test shows a building with no district gets one: from its ALS answer, or from the
      nearest geocoded building in the same estate
    - a function writes it to `master_building`
    - run on the live database by the main session after Task 6: under 2% of clean sales
      lack a district

    Green.
- [x] **Task 8: cheap-sale rule (fable)** `[parallel-ok]` *(fd6b00f. Accepted by the main
  session with one deviation: the sample held only 3 suspected part shares, and the rule
  catches 1 of them. 47 of 50 were real 1950s-60s tenement prices, so the stricter 0.25
  floor would have dropped 14 real sales. The rule removes 485 clean sales in all.)*
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
  - Notes (2026-10-23, done): on the 2026-10-22 snapshot 1,558 clean sales (deal_features
    `exclude_reason` None) are under HK$1,000/sq ft, not 2,134: the datacheck counted the
    `txn` view, which lacks the model's first-hand rule and other exclusions. All Centadata;
    1990s 158, 2000s 1,391, 2010s 8, 2020s 1. Sample of 50 (seed 8), classified from the
    flat's other sales, the building's same-year median and the flat's area against the
    building's: **real old price 47, part share (suspected) 3, area error 0, other 0**. The
    real ones are 1950s-60s tenement flats (Sham Shui Po, Yau Tsim Mong, Kowloon City) sold
    1995-2006 for HK$0.5-0.8m, at 0.2-0.35 of the district's index-adjusted median (e.g.
    福華街564號 5/F: 2002 $0.52m, 2004 $0.50m, 2014 $1.99m, 2018 $4.0m). The 3 suspected
    part shares: 嘉湖山莊 8座 30/H 2002 at half the block's price that year; 鈞德樓 2/K 2009 at
    0.56 of the building's; 九龍城道24號 7/F 2012 at HK$0.5m (0.11 of the district).
    Rule chosen: `low_psf` = index-adjusted psf under 0.15x the district's clean median, or
    under 0.5x the median of the flat's other clean sales within 5 years (the plan's 0.25
    district floor would remove 14 of the 47 real ones; 0.15 removes 1 of 47 and 1 of the 3
    part shares; a 25-row check of the flat-level part found about 20 part shares). Not met:
    "most of the non-real ones" in the sample (1 of 3; the other two sit at 0.27 and 0.52 of
    the district, among real prices, and have no clean sale of the same flat within 5 years).
    Removes 485 clean sales on the snapshot (1990s 125, 2000s 193, 2010s 100, 2020s 67; 45
    under HK$1,000). Decision recorded in docs/decisions.md. Takes effect at the Task 11
    retrain.
- [x] **Task 8b: longer first-hand window for newer buildings**
  - Files: modify `src/hkestate/features.py` (first-hand rule). Test
    `tests/test_features.py`.
  - Done when: a test written first shows a flat's first-ever sale 50 months after
    completion, in a building completed 2010 or later, is marked first-hand. The diagnosis
    counts 4,153 such sales, priced 1.12× resales. The window becomes 72 months for those
    buildings and stays 24 for older ones. Green.
  - Notes: after Task 8, same file. Takes effect at the Task 11 retrain. Record in
    decisions.md as Claude's call.
  - Result: done. Window 72 months when the completion date is 2010 or later (completion
    anchors only). Removes 5,012 clean sales on the snapshot (diagnosis counted 4,153; its
    filter differs); median price 1.05x same-building-year resales (diagnosis: 1.12x).
    Existing completion-date test fixture moved to a 2027 sale. Numbers in decisions.md.
- [x] **Task 9: SRPE glued prices** `[parallel-ok]`
  - Files: modify `src/hkestate/sources/srpe.py`. Test `tests/test_srpe.py`.
  - Done when: a test built from row `3085:3 3:2 5:A A:2016-12-03` (HK$728,100,014,452,000)
    gives one row per flat with one price each. Existing SRPE tests pass. Green.
- [x] **Task 10: remove `QUARANTINE_SQL`** `[parallel-ok]`
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

- [x] **Task 12: rename "pairs" and add the caveat**
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

- [x] **Task 13: primary school nets** `[parallel-ok]`
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
- [x] **Task 14: commute estimate** `[parallel-ok]`
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
- [x] **Task 14b: distance-based train times** *(added after Task 14, 2e3ad21: a flat 2
  minutes a stop gives Tuen Mun → Admiralty 31 min against roughly 45; Hong Kong and Central
  aren't linked.)*
  - Files: modify `src/hkestate/commute.py`. Test `tests/test_commute.py`.
  - Done when: tests written first show that each stop's time is 0.5 min of dwell plus the
    straight-line distance between the two stations at an average line speed. The speed is
    set so Tuen Mun → Admiralty lands at 40-50 min and Kowloon Tong → Admiralty at 8-11 min.
    A walking link of 5 min joins Hong Kong (HOK) and Central (CEN), and one of 4 min joins
    Tsim Sha Tsui (TST) and East Tsim Sha Tsui (ETS). The stop-count estimate stays as a
    fallback when coordinates are missing. Green.
- [x] **Task 15: rent estimate** `[parallel-ok]`
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
- [x] **Task 16: mortgage rules** `[parallel-ok]`
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

- [x] **Task 18: user profile and scoring** *(done: `advisor.Profile`, `AdvisorData`,
  `load_data`, `recommend`; scoring in decisions.md "Decision helper scoring". The snapshot
  carries neither `school_net` nor the `mtr_station` table, so `load_data` takes a stations
  CSV (code, name_en, lat, lon) and a building->net Series; Task 21 should dump both from the
  database in one short read, or Task 11's snapshot should add them. Real run 2026-10-23 on
  the 10-22 snapshot: investor (budget 8m) 13,091 listings in 2,180 estates, 6 s; family
  (600+ sq ft, 3 rooms, 45 min to Admiralty) 604 listings in 24 estates, since only
  geocoded buildings have a commute.)*
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
- [x] **Task 18b: advisor fixes** *(done with Task 19: the snapshot now carries
  `buildings.school_net` (guarded column check) and `stations.parquet`, read by `load_data`
  with the CSV / Series arguments kept as overrides; the rent-yield part of the investment
  score counts in full only for building or estate rents, a district estimate is pulled
  halfway to the district's median yield, an RVD-average estimate scores unknown, and the
  reason names the basis; two listings of one flat at prices more than 2% apart keep the
  cheaper (809 merged on the 10-22 snapshot). The investor's top 5 kept the same five
  listings, ranks 4 and 5 swapped: all five rest on district rents at 6-10% yields, which
  the pull halves but still scores near 100. Decisions in decisions.md.)*
- [x] **Task 19: buy vs rent, sell vs let** *(done: `buy_vs_rent`, `sell_vs_let`,
  `growth_range` (mid from `districts.csv`, low/high from N-year windows of the district's
  `index.csv`), `fair_value` and `advise_owner`; buy listings carry a `vs_rent` line and
  `vs_rent_hkd` (recommend takes 14 s instead of 7 s for 12,708 listings). Assumptions and
  sources in decisions.md "Buy vs rent, sell vs let".)*
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
- [x] **Task 20: market today summary** `[parallel-ok]`
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
- [x] **Task 22: "Market today" page** `[parallel-ok]` with Task 23
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
