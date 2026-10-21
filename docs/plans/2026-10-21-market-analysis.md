# Plan: market analysis on the full Centadata data (2026-10-21)

**Goal.** Centadata's per-flat sale history back to 1995 is now loaded for the whole territory
(about 2.0 million sales, every building with a recorded sale). Use it for both goals:
re-value and re-rank current listings, and rerun the attribute growth study on real recorded
pairs (two sales of the same flat) instead of mostly model-estimated ones. The 2026-10-18
findings had 4,987 real pairs, 81% from the five pilot estates.

**Approach.**
1. Make the analysis read fast on 2.3 million rows, so `rank`, `train` and `report` hold the
   database for a minute or two, not hours (the 2026-10-21 `rank` held it 2 h+ and the daily
   listings run died on the lock).
2. Check the new data before trusting it.
3. Retrain the fair-price model and rerank listings.
4. Rerun the growth study on real pairs with an adjustable date window, in several
   settings (all data, each market phase), plus per-district results.
5. Add a growth-study page to the dashboard.

Background jobs (daily listings, backfill-territory, watchdog, scheduled tasks) keep running
throughout. The 28Hse territory backfill (about 7% done) is not waited for.

**Constraints** (verbatim from `docs/decisions.md`):
- **Purpose: the owner's own investment research** (2026-10-16). Not a product, no clients,
  single user. Keeps scraping in personal-use territory and polish modest.
- **Coverage: whole territory, private residential flats only** (2026-10-16). Excludes HOS /
  subsidised flats (resale restrictions distort prices), houses/villas, commercial.
- Two goals: 1. Under-market listings: score every current listing by its discount to a
  modelled fair price (after allowing for the usual asking-vs-deal gap) and **rank them; no
  fixed cut-off**. 2. Attribute growth study: which flat attributes are linked to better
  growth, measured as **both price growth and total return (growth + rental yield)**, over
  holding periods of 3+ years, with risk (drawdown) shown and out-of-sample checks.
- **Data budget: HK$0** (2026-10-16). No paid APIs (e.g. Anysite) and no IRIS searches unless
  the owner approves a specific spend.
- **Collection method: slow, polite scraping** (2026-10-16): obey robots.txt, ≤1 request/s per
  site, raw-page cache so nothing is fetched twice, resumable runs. Never call disallowed
  paths (e.g. centanet `/centanetapi/api/`).
- **Deliverables: analysis notebooks, a local Streamlit dashboard listing ranked deals, and a
  written findings report** (2026-10-16).
- **Language: English reports, Chinese estate/building names kept alongside** (2026-10-16).
- **History: all data, adjustable window** (2026-10-21, user; replaces "History from 2000").

**Shared resources: rules for every helper.**
- The live database `data\hkestate.duckdb` is used by running scrapers. **Never open it for
  more than a few seconds.** Work from `--from-snapshot` (`data\model\snapshot\`, refreshed by
  the main session in Phase 1) or from your own copy of the database in your scratch folder.
  Never run `hkestate status` or any `txn`/`txn_all` query on the live file.
- Don't start, stop or restart scheduled tasks or background jobs.
- Dashboard checks use port 8531, never the default 8501; stop it afterwards (by port).

**Acceptance.**
- A1. `hkestate rank` on the live database finishes with its database read under 3 minutes,
  and flags the same duplicates as before on the test fixtures.
- A2. A data-check report shows sales per year 1995-2026 against an independent count,
  cross-source duplicates and odd values, each with a verdict.
- A3. The model is retrained on the full data, `data/reports/model_eval.md` compares it with
  the 2026-10-18 run, and today's ranked listings CSV is written.
- A4. `hkestate report` takes `--start`/`--end`, a preset set of settings runs in one command,
  and the findings report shows verdicts per setting and per district, with real pairs as
  the main evidence.
- A5. The dashboard has a growth-study page that reads the saved results and switches
  between settings.

## Phase 1 – Fast database read (fable)

- [x] **Task 1: duplicate check without the OR join**
  - Files: modify `src/hkestate/db.py` (`txn_all_sql`, the `dup` CTE); test
    `tests/test_db.py`
  - Done when: a new test builds sales where (a) two sources report the same unit and
    (b) an agency report gives only a floor band (高層/中層/低層) for the same building and flat,
    and checks `excl_duplicate` is set on exactly the expected rows. It passes before and
    after the change (write it first against the old SQL). Full pytest and ruff green.
  - Notes: replace `ON (a.unit_id = o.unit_id OR (band AND same building/flat))` with the
    UNION of two equi-joins, one per arm, keeping every other condition and the
    `SELECT DISTINCT`. Measured on a copy of the database 2026-10-21: the OR join was still
    running after 600 s, the split takes 0.7 s, on 2.28 million latest rows.
- [x] **Task 2: extract reads `txn_latest` once**
  - Files: modify `src/hkestate/features.py` (`extract`); test `tests/test_features.py`
  - Done when: `extract` materialises `txn_latest` once into a temp table and uses it for the
    quarantine count, the agreement lag, the model's input and `srpe_buildings`; existing
    feature tests pass. Timed on a copy of the database: the whole `extract` under 3 minutes
    (report the time).
- [x] **Task 3 (main session): real run** *(2026-10-21 16:58: database read ~1 min
  (snapshot written 16:59:22), whole rank 2 min 49 s, 12,472 listings ranked; daily and
  backfill logs show no lock failure.)*
  - Run `hkestate rank` on the live database while the scrapers run.
  - Done when: the rank log shows the snapshot read and the ranked CSV written, the extract
    took under 3 minutes, and the daily and backfill logs show no lock failure in that window.
    Tick the open-items entry about `rank`.

## Phase 2 – Data checks (fable)

- [x] **Task 4: `hkestate datacheck`** *(2026-10-21 17:23: run on the snapshot, report
  `data/reports/datacheck_20261021.md`; sales per year ok (61-84% of the Land Registry
  count 2005-2026, no free count before 2005), duplicates left ok (4,475 pairs, 0.2%), odd
  values and coverage "look at this": Chinese-numeral floor labels (五, 十二) are not parsed,
  285k Centadata deals without a floor; SRPE's 2055 date is the register's own typo, the
  parser now falls back to the PASP date.)*
  - Files: create `src/hkestate/datacheck.py`, `tests/test_datacheck.py`; modify
    `src/hkestate/cli.py` (new subcommand), `docs/operations.md` (command list)
  - Works from the snapshot (`features.load_snapshot()`); writes
    `data/reports/datacheck_<date>.md` and returns the same numbers as a dict.
  - Checks, each with a plain verdict (ok / look at this):
    1. Clean resales per year 1995-2026 against an independent yearly count. Use the
       Land Registry's monthly sale-and-purchase statistics if robots.txt allows them (one
       probe, cached; record the source in `docs/sources.md`), otherwise RVD's published
       figures already in the database. Flag years below 50% or above 110% of the count.
    2. Duplicate sales across Centadata history rows, Centadata latest-deal rows and 28Hse
       (same unit, price within the duplicate tolerance, dates within the duplicate window),
       per year and source, after `excl_duplicate`.
    3. Odd values: deal dates in the future or before 1990 (e.g. SRPE dates in 2055), prices
       under HK$500k or over HK$500m, saleable area under 100 or over 5,000 sq ft, price per
       sq ft outside 1,000-100,000, units with two sales on the same day.
    4. Coverage: share of sales with a building, with saleable area, with floor position, by
       decade.
  - Done when: tests on a small synthetic frame cover every check; the command runs on the
    current snapshot and the report exists. Odd values found go to the report, not deleted.
    Fixes that are plain parsing bugs (e.g. a 2055 date) are made with a test; anything that
    needs deleting rows goes to open items.

- [x] **Task 4b: fix what the data check found** *(added 2026-10-21 after Task 4; done
  2026-10-21 17:45 on the database copy: Centadata floor coverage 85.2% -> 99.7%, duplicate
  pairs left 4,473 -> 21, pre-1990 rows in clean data 38 -> 0; rows with two sales of one
  unit on one day 4,138 -> 827 (309 clean, different prices). Centadata's own label moved
  18,600 sales to first-hand and 5,351 to resale. No `refresh-floors` needed: the parser fix
  is applied on read.)*
  - Files: `src/hkestate/normalise.py`, `src/hkestate/db.py` (`txn_all_sql`),
    `src/hkestate/features.py`, `src/hkestate/sources/centadata_history.py` if needed; tests
  - (a) `parse_floor` reads Chinese numerals (五, 十二, 二十三, 三十, plus forms like 地下, 低座
    left as now): ~285k Centadata deals (13%) have no floor today. The fix must reach the
    analysis without rewriting stored raw rows; if the `unit.floor_num` column has to be
    refilled, add a `hkestate refresh-floors` command that updates in short batches (each
    session a few seconds) for the main session to run.
  - (b) Same-source duplicates: two rows of one source for the same unit, same day and same
    price (Centadata stores some sales under two ids, 4,193 pairs) are flagged
    `excl_duplicate` on all but one copy (keep the lowest `source_id`).
  - (c) Deal dates before 1990 are excluded (new flag or `excl_outlier`), 38 rows.
  - (e) Centadata history rows carry `firstOrSecondHand` in `raw_json`: use it for
    `first_hand` where present (it is the source's own label), keeping the first-hand rule
    only where the label is missing. Report how many sales change status.
  - Done when: tests for each; on the database copy, `hkestate datacheck --no-fetch` (pointed
    at a fresh extract of the copy) shows floor coverage for Centadata ≥ 97%, same-day
    duplicate pairs left under 100, no pre-1990 dates in clean data. Full pytest and ruff
    green.

## Phase 3 – Fair price and ranking on the full data (fable)

- [x] **Task 5: retrain and compare** *(2026-10-21 18:08: trained on 1,500,603 clean deals
  from the 17:54 snapshot in 4 min 16 s, peak 4.7 GB; `model_eval.md` compares with the
  2026-10-18 run; old model files in `data/model/backup_20261018/`. Recent sales: 6.3% median
  error on 22,043 test deals (old run 6.0% on 15,655; on the 13,356 deals in both tests, old
  6.10% vs new 6.25%). Held-out older sales: 5.0% / 5.5% / 4.4% / 4.3% by decade 1990s-2020s
  against the old model's 7.5% / 7.8% / 6.0% / 5.9%; the old model knew 14,187 buildings
  (90.8% of today's listings), the new 19,151 (99.0%), so the new model stays. Window: all
  years kept; 2010+ was 0.2 points better on recent sales (6.1% vs 6.3%), not "clearly".
  `rank --from-snapshot` 18:27: 12,472 listings ranked; 68 of the old top 100 moved more than
  10 places, 62 are still in the top 100; fair prices moved by a median -0.7%.)*
  - Files: modify `src/hkestate/model.py` only if training fails or exceeds 30 minutes or
    16 GB on the full data; `data/reports/model_eval.md` (generated)
  - Done when: `hkestate train --from-snapshot` finishes; `model_eval.md` gains a section
    comparing typical error overall, by district and by decade with the 2026-10-18 run (5.2%
    pilot / 5.0% territory); `hkestate rank --from-snapshot` writes today's ranked CSV and its
    counts. Report how many listings moved more than 10 places in the top 100.
  - Notes: if the model needs a training window (e.g. the price index cannot adjust 1995
    prices well), train on all years and on 2010+, compare, and keep the more accurate one
    on recent sales; say which in the report.

## Phase 4 – Growth study on real pairs, adjustable window (fable)

- [x] **Task 6: date window for the study** *(2026-10-21 21:45: `pairs.in_window` keeps a
  pair only when both sales are inside; `study.prepare` builds every pair once and
  `study.analyse` runs one window; the time split falls inside the window (2018 end-cut
  where both sides have 200+ pairs, else the median purchase year). The cluster SE and the
  repeat-sales index were vectorised for 660k pairs.)*
  - Files: modify `src/hkestate/study.py`, `src/hkestate/pairs.py`, `src/hkestate/growth.py`,
    `src/hkestate/cli.py`; tests `tests/test_study.py`, `tests/test_pairs.py`
  - `hkestate report --start YYYY-MM-DD --end YYYY-MM-DD`: a pair counts when both its sales
    fall inside the window. Defaults: earliest and latest data (all data).
  - Done when: a test shows pairs outside the window are dropped and the out-of-sample time
    split adapts to the window (median purchase date inside it).
- [x] **Task 7: preset settings in one command** *(2026-10-21 21:43: `report --from-snapshot
  --settings standard` on the 17:54 snapshot: 22 min 35 s wall (pair building 17 min, of
  which costs and yields 13.5 min; the eight analyses 4 min), peak working set 8.9 GB.
  Pairs: all 663,400 real + 107 Ricacorp + 564,113 estimated; since-2000 464,833 real;
  crash-1997-2003 6,941; recovery-2003-2008 12,855; boom-2008-2021 136,823; fall-2021-2024
  1 and recent-2024-now 0 (both say "too few recorded pairs", no verdicts);
  bought-after-crash 374,660 real + 396,373 estimated. Saved under
  `data/reports/growth/20261021/`; column docs in `study.py`'s docstring.)*
  - Files: modify `src/hkestate/study.py`, `src/hkestate/report.py`, `src/hkestate/cli.py`;
    tests
  - `hkestate report --settings standard` runs, from one snapshot:
    - `all`: all data (default headline);
    - `since-2000`: purchases from 2000;
    - `crash-1997-2003`, `recovery-2003-2008`, `boom-2008-2021`, `fall-2021-2024`,
      `recent-2024-now`: pairs bought and sold within each phase (3-year minimum hold still
      applies; a phase too short for enough pairs says so instead of a verdict);
    - `bought-after-crash`: purchases from 2003-07 (checks whether the crash drives `all`).
  - Saves results per setting under `data/reports/growth/<date>/<setting>/` as CSV
    (`effects.csv`, `districts.csv`, `index.csv`, `returns.csv`) plus `settings.json`
    (windows, pair counts, run time). Phase 5 reads these.
  - Done when: tests cover the settings list and the saved file layout; the command runs on
    the snapshot within 2 hours (report the time).
- [ ] **Task 8: report on real pairs, by setting and by district**
  - Files: modify `src/hkestate/report.py`, `src/hkestate/charts.py`; tests
    `tests/test_study.py`
  - Real (recorded) pairs are the main evidence; estimated pairs are a cross-check section.
  - New sections: a verdict table of attribute × setting (robust / significant / not, with
    the effect), with a plain-language line per attribute ("helps in rising and falling
    markets", "only in the 2008-2021 boom", ...); growth, total return and worst fall per
    district (18 districts) for `all`; the price index per district from 1995.
  - Done when: the generated `findings_<date>.md` has these sections on the current
    snapshot; a reader can say for each attribute whether it holds across phases. Spot-check
    three numbers by hand against the saved CSVs and state them in the hand-back.

## Phase 5 – Dashboard growth page (sonnet)

- [x] **Task 9: growth-study page** *(2026-10-21: `?page=growth`; AppTest on the real folder
  for `all`, `boom-2008-2021`, `fall-2021-2024` with no exceptions; live server on 8531 gave
  200, stopped by port)*
  - Files: modify `app/dashboard.py`, `src/hkestate/dashboard_data.py`; test
    `tests/test_dashboard.py`
  - Reads the newest `data/reports/growth/<date>/`; a setting selector; the attribute ×
    setting verdict table; effect chart per attribute across settings; district table and
    district price-index chart. Plain-language captions; Chinese district names alongside
    English.
  - Done when: tests for the data loader pass; the dashboard is launched on port 8531, the
    page opens without errors for `all` and one phase setting (checked in a real browser or
    with Streamlit's AppTest), then stopped by port.
- [ ] **Task 10: docs**
  - Files: `README.md`, `docs/operations.md`, `CLAUDE.md` (command list)
  - Done when: `report --start/--end/--settings` and `datacheck` are documented.
