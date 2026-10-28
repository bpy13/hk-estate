# Plan 2026-10-28: rebuild from scratch, and the Yoho fixes

**Goal.** Anyone who clones the repo, including the owner later, can rebuild today's
database, model and reports from an empty data folder by following one document and one
command. All rebuilds follow the same steps and rules; there are no choices about what to
scrape. First, three small ranking fixes found by the Yoho check on 2026-10-28.

**Approach.**
1. Yoho fixes A-C (small, in `rank.py` and `features.py`).
2. Audit every step and hand repair since the project began (git log, docs, logs): is it in
   code, or was it done by hand?
3. Build `hkestate rebuild` (runs every step in order, resumable), turn each hand repair into
   code, write `docs/rebuild.md`.
4. Small test: rebuild 3 estates into an empty test data folder, reading the saved pages,
   compare with today's database, time each step and estimate the full run.

**Owner's answers (2026-10-28, this conversation).**
- Rebuilds give the same result for everyone; no options about what data to scrape.
- No data snapshot is shared.
- Limit accepted: listing history (daily asking prices since 2026-10-16, listings since
  gone) exists only in today's database and cannot be rebuilt. `docs/rebuild.md` says so.
- Test small and extrapolate; no full-territory test and no 13 GB copy.
- Build Yoho fixes A-C.

**Constraints (verbatim from CLAUDE.md project rules).**
- "Scrape politely: obey robots.txt, at least 1 request/second per site, identify a cache so
  pages are never fetched twice. Never call paths robots.txt disallows (e.g. centanet
  `/centanetapi/api/`)."
- "Never store personal data: no owner/buyer names, no agent names or phone numbers (keep
  only the listing URL). Hong Kong's privacy law (PDPO) applies."
- "Raw scraped data and the database stay out of git (data/ is ignored)."
- Database: `data/hkestate.duckdb` is shared with running jobs (daily job, 28Hse territory
  backfill, watchdog restarts). Short sessions only; no `txn`/`txn_all`/`txn_latest` views in
  ad-hoc queries; never hold the file for minutes.

**Acceptance criteria.**
- A1. The 9 Yoho listings from the 2026-10-28 check get `exclude_reason = 'check_peer_ask'`
  after `rank --from-snapshot`; the shortlist shows the `below_all_recent` flag clearly.
- A2. A sale repeating the same flat's earlier price (within 0.1%) is excluded as
  `repeat_price`; the 5 Grand Yoho 5座 repeats are excluded.
- A3. `docs/rebuild.md` lists every step from an empty data folder in order, with run times,
  and states the listing-history limit. Every hand repair from the audit is either in code or
  shown to be unneeded on a fresh run.
- A4. `hkestate rebuild` runs the steps in order, resumes after a stop, and has no options
  except the test-only `--estates` subset and `--max-requests`.
- A5. A 3-estate rebuild into an empty test folder matches today's database for those
  estates (row counts per table within a stated tolerance, differences explained), and the
  full-run time is estimated from the timings.
- A6. Tests and lint pass.

## Phase 1 – Yoho fixes (sonnet)

- [x] **Task 1: peer-ask check and shortlist flag**
  - Files: modify `src/hkestate/rank.py` (`listing_checks`, `shortlist_markdown`, README's
    `exclude_reason` table); test `tests/test_rank.py`.
  - Do (failing tests first): new `exclude_reason` `check_peer_ask`: asking per sq ft under
    `PEER_ASK_RATIO = 0.8` times the median asking per sq ft of at least
    `PEER_ASK_MIN = 3` other active sale listings in the same building whose area is within
    3%. Order: after the existing checks (an earlier reason wins). The shortlist block shows
    "below every recent sale" as a flag in the Flags line when `below_all_recent` is true.
  - Done when: `conda run -n hk-estate python -m pytest -q tests/test_rank.py` passes with new
    tests for: flagged at 0.7x with 3 peers; not flagged with 2 peers; not flagged when peers
    differ in area by more than 3%; shortlist text shows the flag.

- [x] **Task 2: repeat-price rule** `[parallel-ok]`
  - Files: modify `src/hkestate/features.py` (`deal_features`, `EXCLUDE_ORDER`); test
    `tests/test_features.py`.
  - Do (failing tests first): new condition `repeat_price`: a deal whose same flat (same
    `building_id`, `floor_num`, `flat`, all non-null) has an earlier eligible deal with a price
    within 0.1%, any gap. The earlier deal stays; the later one is excluded. Place it in
    `EXCLUDE_ORDER` just after `duplicate`.
  - Done when: `conda run -n hk-estate python -m pytest -q tests/test_features.py` passes with
    tests for: 20,565,750 then 20,566,000 five years later excludes the second; a 1% different
    price is kept; a flat with null floor is untouched.

- [x] **Task 3: check on real data** (main session, after Tasks 1-2) *(Done 2026-10-28: all
  9 Yoho listings `check_peer_ask` (394 in all); 28,570 `repeat_price`, including Grand Yoho
  5座 10/C, 8/C, 12/C, 13/D, 27/B. Numbers in open-items.)*
  - Do: `conda run -n hk-estate hkestate rank --from-snapshot`; read the CSV for the 9 Yoho
    ids (CVY014, GAN573, GAN591, CPJ448, CQA348, CQA401, CQS644, CQT017, CQS970) and count
    `repeat_price` rows in the snapshot, including Grand Yoho 5座 10/C.
  - Done when: A1 and A2 hold on real data; counts recorded in `docs/open-items.md`.

## Phase 2 – Audit (opus, read-only)

- [x] **Task 4: list every step and hand repair** *(Done 2026-10-28: Notes "Audit".)*
  - Files: write findings into this plan's Notes (section "Audit").
  - Do: from `git log` (all ~195 commits), `docs/` (decisions, open-items, operations,
    known-issues, sources, older plans) and the command-line lines in `data/logs/*.log`,
    list (a) every data-building step that produced today's database (command, arguments,
    order, dependencies, run time from logs), and (b) every hand repair or one-off command
    (e.g. the LOHAS Park delete, the SRPE `--reparse-cached`, `master --recheck-house`,
    `link-ids`, `crosscheck`, manual CSVs). For each repair say: fixed in code so a fresh run
    never needs it / still needed / unclear. Also list files the code reads that are not in
    git (e.g. `data/manual/*.csv`).
  - Done when: the Notes table covers every `hkestate` subcommand, `hkestate.geo`,
    `hkestate.sources.schools` and `hkestate.sources.srpe`, and every repair named in the docs.

## Phase 3 – Build (sonnet)

Tasks 5-7 are fixed; Task 8 is filled in from the audit before dispatch.

*(Revised 2026-10-28 after the audit, Notes "Audit": the daily prune keeps only 7 days of
pages on the Centanet, 28Hse and Spacious hosts, and the fetcher ignores transaction pages over
7 days old, so a test rebuild makes live requests. A test with its own data folder would not
see the live jobs' locks.)*

Tasks run in order (5, then 6, then 7); they share `cli.py` and `fetch.py`.

- [x] **Task 5: folder overrides, request cap and small fixes**
  - Files: modify `src/hkestate/config.py`, `src/hkestate/fetch.py`, `src/hkestate/locks.py`,
    `src/hkestate/sources/srpe.py`, `src/hkestate/watchdog.py`, `src/hkestate/sources/schools.py`
    (logger only); tests in the matching `tests/test_*.py`.
  - Do (failing tests first):
    - `HKESTATE_CACHE_DIR` sets the page cache folder (default `<data dir>/cache`) and
      `HKESTATE_LOCK_DIR` the lock folder (default `<data dir>/locks`), so a test rebuild
      writes its own database but shares the live cache and sees the live jobs' locks.
    - A process-wide request cap: `fetch.set_request_cap(n)`; every `Fetcher` instance and
      SRPE's own `httpx` client count towards it, and a request past the cap raises
      `RequestCapReached`. Cached pages don't count.
    - SRPE: a development's detail step key no longer holds today's date, so a resume on a
      later day skips finished details (audit 4, "Other rebuild traps").
    - Watchdog: while `rebuild.lock` is held by a live process it restarts nothing.
    - Tests no longer write `schools_*.log` into the real `data/logs` (audit, smaller errors).
  - Done when: the affected test files and the full suite pass, with a test per bullet.

- [ ] **Task 6: `hkestate rebuild`** (after Task 5)
  - Files: create `src/hkestate/rebuild.py`; modify `src/hkestate/cli.py` and the step
    modules that need an estate filter (`sources/cd_master.py`, `hse28_territory.py`,
    `sources/centadata_history.py`, `geo.py`, `sources/schools.py`, `link.py`,
    `sources/srpe.py`, `sources/ricacorp.py`); test `tests/test_rebuild.py`.
  - Do (failing tests first):
    - Steps 1-16 of the audit's table, in that order. Each step is a call to the existing
      command function. Progress goes in `run_progress` under keys `rebuild:<step>`. One log
      `data/logs/rebuild_<time>.log`; holds `rebuild.lock`.
    - A step is done when its own to-do count is 0, not when it exits 0 (audit 4, "Done is
      not the exit code"). Rerun a step up to 3 times; then stop and name what is left.
    - Hand repairs from the audit that a fresh run still needs, built in: `--discover` on the
      first `backfill-territory` run when `hse28_estate` is empty (R9); `link-ids` after the
      master list and the territory backfill (R8); `backfill_gain_pct` again after the
      cross-check, so the cross-check-before-last-master order rule (R14) goes.
    - A step whose host lock is held by another process is deferred: the rebuild carries on
      with steps that don't depend on it and lists the deferred ones at the end. A rerun
      picks them up.
    - Options: `--estates` (test only: estate names or Centadata codes, passed to every step;
      add the filter where the audit says it is missing) and `--max-requests N` (uses
      `fetch.set_request_cap`). No other options.
  - Done when: `pytest -q tests/test_rebuild.py` passes with tests for: steps run in order; a
    rerun skips done steps; a step with to-do left is rerun, then stops the run; a held lock
    defers a step and its dependants only; `--estates` reaches every step.

- [ ] **Task 7: `docs/rebuild.md` and doc fixes** (after Task 6)
  - Files: create `docs/rebuild.md`; modify `README.md` (pointer, `playwright install
    chromium` in setup), `docs/operations.md` (cd-history gap is 2 s, not 6 s; Ricacorp pass
    ~30 h, not ~7 h; the rebuild lock), `docs/architecture.md` (new module).
  - Do: setup from a fresh clone, then `hkestate rebuild`; each step with what it does, its
    host lane and measured run time; the full-run estimate (~13 days, set by 28Hse); the
    scheduled tasks to register afterwards; the limits: listing history and Ricacorp deals
    (Ricacorp shows only ~90 days) cannot be rebuilt, and a rebuild on another day sees other
    listings, so the model differs slightly.
  - Done when: every step 0-17 of the audit appears in order with a run time.

- [ ] **Task 8: hand repairs** *(Folded into Tasks 5-7 after the audit. 14 of 20 repairs are
  already in code; R8, R9, R14 go into Task 6; R1, R15 stay "unclear" and go to open items.)*

## Phase 4 – Small test (main session)

- [ ] **Task 9: 2-estate rebuild, 28Hse lane deferred**
  - Do: `HKESTATE_DATA_DIR` = `data\rebuild_test\`, `HKESTATE_CACHE_DIR` = `data\cache`,
    `HKESTATE_LOCK_DIR` = `data\locks`; run `hkestate rebuild --estates 爾巒,<one ordinary
    territory estate> --max-requests 1500`. The live territory backfill holds the 28Hse lock,
    so the 28Hse steps defer; their times come from the existing logs. Compare row counts per
    table for those estates with today's database (short read-only sessions), time each step,
    estimate the full run. Delete `data\rebuild_test\` afterwards.
  - Done when: A5 holds for the steps that ran; deferred steps listed; results and the
    estimate recorded in `docs/rebuild.md`.

## Notes

## Audit

Task 4, 2026-10-28, read-only. Sources: `git log --stat` (196 commits, 10-16 00:52 to 10-28
12:37), every file in `docs/` and `docs/plans/`, `README.md`, `src/hkestate/cli.py`, the
`main()` of `geo.py`, `sources/schools.py` and `sources/srpe.py`, `scripts/*.ps1`, and the
head, `command line:` line and tail of every `data/logs/*.log` (285 files). The database was
not opened. **V** = seen in code or a log. **I** = inferred; the note says from what.

Logs before 2026-10-20 have no `command line:` line. For those runs the arguments come from
the docs and the log text.

### 1. Steps in the order a fresh rebuild must run them

"Lane" names the host a step uses. Steps in different lanes can run at the same time. Run times
come from the named log unless marked I.

| # | Step | Exact command | Loads (tables) | Depends on | Measured run time (log) | Resumable? | `--estates`? | Notes |
|---|---|---|---|---|---|---|---|---|
| 0 | Setup | `conda env create -f environment.yml`; `conda run -n hk-estate python -m pip install -e ".[dev]"`; **`conda run -n hk-estate python -m playwright install chromium`** | - | - | minutes | - | - | The Playwright browser step is in no doc (V: README and environment.yml have no `playwright install`). SRPE fails without it. |
| 1 | RVD | `hkestate rvd` | `price_index`, `rent_index`, `market_stat` | - | 56 s (`rvd_20261016_020609.log`) | rerun replaces each series | no (not needed) | Lane rvd.gov.hk. **Ran only on 10-16** (V: 2 rvd logs). No scheduled task refreshes it. |
| 2 | Pilot 28Hse backfill | `hkestate backfill --estates pilot` | `estate`, `phase`, `building`, `unit`, `txn_raw` (source hse28), `run_progress` | - | 16.7 h (`backfill_20261016_024618.log`, 02:46-19:26), after 1 h of 429 restarts | yes (`run_progress`) | yes, pilot names only (other names are refused) | Lane 28hse. Holds the `hse28` lock. |
| 3 | Pilot cross-check | `hkestate crosscheck --estates pilot` | `txn_raw` (source centadata: only deals 28Hse lacks), report `crosscheck_<date>.md` | 2 | about 8 min live: TKS 2 min, LOHAS Park 3.3 min (`crosscheck_20261016_070136`, `_192830`); 35 s from cache (`_20261018_215439`) | no, but a rerun adds 0 rows | yes, pilot groups only | Lane hk.centanet.com. Must finish before step 4 ends (see 2.2, item R14). |
| 4 | Master list | `hkestate master` (rerun until the log says `0 to do`) | `master_estate`, `master_building`, `estate`, `building`, `unit`, `txn_raw` (centadata latest deal per flat); fills `gain_pct` on all centadata rows at the end | 3 | 32.4 h at the old 2 s base (`master_20261016_173132` 20.6 h + `master_20261018_213111` 11.8 h). At today's 4 s base: **I ~2.5-3 days**, plus the house blocks every pass now reads (the recheck took 17.2 h: `master_20261023_184451` + `master_20261026_011441`) | yes (one step per estate code) | **no** (`--limit` only) | Lane hk.centanet.com. A non-throttle error leaves the estate undone for the next run (14 errors on 10-18, V). `cmd_master` always returns 0 (V). |
| 5 | 28Hse territory backfill | `hkestate backfill-territory --discover` on the first run, then `hkestate backfill-territory` until nothing is left | `hse28_estate`, `source_map`, `estate`/`building`/`unit`, `txn_raw` (hse28) | 2 (lock), 4 (estate order, linking, HOS skip) | **Still running**: started 10-16 19:40; at 10-28 12:20 it was at 994 of 4,492 left (`backfill_territory_20261027_233155.log`). About 9.8 days of run time so far. **I ~11-12 days in all** | yes | **no** (`--max-estates`, `--max-hours`, `--order`, `--mapped-only` only) | Lane 28hse. Without `--discover` a fresh database finds 0 estates (V: `backfill_territory_20261016_193430.log`). |
| 6 | Link ids | `hkestate link-ids` | `source_map` (pilot links, then every loaded estate re-linked), `hse28_estate` | 4, 5 | **Never run** (V: no `link_*.log`). Short (I) | rerun is safe | no | `link_pilot` and `link_loaded` run only here (V: grep). Estates backfilled before step 4 covered them are linked only by this step. |
| 7 | Centadata full history | `hkestate cd-history` (rerun until `0 buildings to do`) | `txn_raw` (centadata, dated sales back to 1995) | 4 (flat codes come from the master's stored latest deals), 3 for pilot flats | 16.6 h for 17,741 buildings (`cd-history_20261020_211940.log`). Plus ~2,700 done before. **I ~19-20 h** for the whole territory at 2 s | yes (per building) | yes, but only `estate.group_name` groups, which only pilot estates have (V: `cd_master.load_block` sets no group) | Lane hk.centanet.com, after step 4. |
| 8 | SRPE first-hand sales | `python -m hkestate.sources.srpe` | `srpe_development`, `txn_raw` (source srpe); PDFs saved to `data/cache/www.srpe.gov.hk/pdf/` | 4 (matching by `master_building.address`) | 7.6 h for 532 developments (`srpe_20261018_213111.log`) | yes, but a development's detail step is keyed by **today's date**, so a rerun on another day reads all 532 details again (~4.4 h, I) | **no** (`--limit` only) | Lane srpe.gov.hk, 30 s crawl-delay. Needs Playwright Chromium (headless, no clicks by a person). Its requests bypass `Fetcher` (own `httpx` client). |
| 9 | Ricacorp pairs | `hkestate ricacorp` | `ricacorp_deal`, `txn_raw` (ricacorp), `deal_pair`; placement runs at the end of every run | 4 (placement) | one week's pass of 15,280 pages: about 30 h wall with restarts (`ricacorp_20261019_211526`, `_20261020_211940`, `_20261020_214941`). docs/operations.md says ~7 h | within one ISO week only | no (`--limit`, `--max-hours`) | Lane ricacorp.com. Pages are deleted from the cache after parsing (V: `fetcher.forget`). |
| 10 | Geocoding + tail | `python -m hkestate.geo` | `master_building` (lat, lon, geo_status, MTR distance, building age, district), `mtr_station` | 2 and 4 (seeds pilot 28Hse buildings and master buildings) | 17.1 h for 40,406 buildings (`geo_20261023_011933.log`); the tail (mismatch, MTR, ages, districts) 3 s (`geo_20261023_182953.log`) | yes (`geo_status`) | no (`--limit`) | Lane als.gov.hk. The full run now runs the tail itself (V: `geo.main`). |
| 11 | School nets | `python -m hkestate.sources.schools` | school rows, `master_building.school_net` | 10 | 23 s (`schools_20261023_182957.log`) | rerun replaces | no | Lane edb.gov.hk. |
| 12 | Listings (one run) | `hkestate daily` | `listing_raw`, `listing`, `listing_snapshot`, `listing_run_day`, `risk_flag` | 4 (matching); 5 finished, or 28Hse listings are skipped | first uncapped territory pass ~14 h (docs/operations.md); measured daily runs 4.5 h (`daily_20261022_080001`) to 12.1 h (`daily_20261027_080003`) | partly (reads saved every 25 pages) | groups only: `pilot`, `territory`, `all` or pilot names | Lanes centanet, 28hse, spacious. Only today's listings: history can't be rebuilt. |
| 13 | Train | `hkestate train` | `data/model/snapshot/*`, `fair_price.txt/.json`, `test_predictions`, `deals.parquet`, `data/reports/model_eval.md` | 1-12 | 5.7 min (`train_20261023_183024`); 9.5 min (`train_20261025_120002`) | no (rerun) | n/a | No `--since`: the kept model uses every year (V: command lines). |
| 14 | Rank | `hkestate rank --from-snapshot` | `ranked_listings_<date>.csv`, `shortlist_<date>.md`, `asking_gap_<date>.csv`, `ranked_latest.parquet` | 13 | 1.8 min (`rank_20261023_183609`) | no | n/a | |
| 15 | Growth study | `hkestate report --settings standard --from-snapshot` | `findings_<date>.md`, `data/reports/growth/<date>/<setting>/` | 13 | 6.6 min (`report_20261023_183800`); 12-21 min on 10-21 | no | n/a | Up to ~9 GB memory (docs). |
| 16 | Data check | `hkestate datacheck` | `datacheck_<date>.md` | 13 | about 1 min (`datacheck_20261021_171546`) | no | n/a | Fetches 22 Land Registry files (cached 30 days). Report only. |
| 17 | Register tasks | `scripts\register_task.ps1`, `scripts\watchdog.ps1 -Register`, `scripts\register_dashboard_task.ps1` | - | 16 | seconds | - | - | Setup after the rebuild, not a data step. |

Maintenance only (not part of a rebuild):

| Step | Command | Why it is not a rebuild step |
|---|---|---|
| `update` | `hkestate update` (pilot by default) | Weekly newest 28Hse pages; step 2 already reads every deal list. 2 min (`update_20261018_070001.log`). Covers pilot estates only: territory 28Hse deals get no weekly refresh (V: default `--estates pilot`). |
| `listings` | `hkestate listings ...` | Manual form of step 12. |
| `prune-cache` | `hkestate prune-cache --days 7` | Deletes cache day folders of the three listing hosts. **Dangerous for a rebuild that reads saved pages** (see 4.). |
| `check-blocked` | `hkestate check-blocked` | Monthly robots.txt check, a few seconds. |
| `watchdog` | `hkestate watchdog [--dry-run]` | Restarts dead jobs; no database. |
| `status` | `hkestate status` | Queries the `txn` views: never on the live file (plan rules). |
| `master --recheck-house` | `hkestate master --recheck-house` | One-off repair (R5); not needed on a fresh run. |
| `ricacorp --place-only` | `hkestate ricacorp --place-only` | Placement already runs at the end of each pass (V: every ricacorp log ends with `ricacorp placement`). |
| `srpe --reparse-cached` | `python -m hkestate.sources.srpe --reparse-cached` | One-off repair (R3). |
| `geo --stations-only` | `python -m hkestate.geo --stations-only` | Needed only when buildings were added after the last geocoding run. |
| `schools --assign-only` | `python -m hkestate.sources.schools --assign-only` | Re-assigns nets without fetching. |
| `train --compare-since/--previous` | e.g. `train --from-snapshot --compare-since 2010 --previous data\model\backup_20261018` (10-21, V) | Comparison report only. |
| `scripts/spot_check.py` | `conda run -n hk-estate python scripts/spot_check.py` | Live 28Hse check, own cache folder. |
| `scripts/spike/*.py` | - | Phase 0 probes, throwaway. |

### 2. Hand repairs and one-off actions

| # | When | What it did | Verdict |
|---|---|---|---|
| R1 | 10-16 07:01-12:32, 7 cross-check runs (V: logs) | Reran `crosscheck` per estate as the block matching was fixed (274c025, 29a321a, f043efb). Mei Foo went 2.7% → 93.7% → 93.8% | **Fixed in code**: one `crosscheck --estates pilot` now gives the final rates. Today's DB: rows from the 07:09 Mei Foo run (80/99 blocks, 4,946 rows) stay in append-only `txn_raw`. **Unclear** whether any sit on a wrong block; check Mei Foo centadata rows fetched 10-16 07:09-07:10 against the later run. |
| R2 | 10-16 19:28 bad run; owner's SQL delete 10-18; `crosscheck --estates lohas-park` rerun 10-18 21:54 (V) | Deleted 14,562 wrong-building LOHAS Park Centadata rows; the rerun from cached pages added 164 rows | **Fixed in code**: `centadata.py` picks the 28Hse development before the block (a486814) and maps wing pages (30f06f5). A fresh cross-check never stores them. `QUARANTINE_SQL` removed (2fbbb24). |
| R3 | 10-23 13:18 and 14:56-16:56 (V: `srpe_20261023_131856`, `_145606`) | `--reparse-cached`: the first run deleted 1,285 real rows (bug); the rerun added back or fixed 3,864 rows and deleted 111 | **Fixed in code**: `register_deals` splits multi-flat rows (e4fe623, dc6ca45); a fresh `run_srpe` parses each PDF with today's parser. |
| R4 | open item | Two SRPE rows dated 2055-09-26 and in the future: "re-read those registers" | **Fixed in code**: the parser uses the PASP date for that typo; a future date gets `excl_bad_date` (d6596df). |
| R5 | 10-23 18:44 → 10-26 09:29, 2 runs; a Windows Update restart killed the first (V) | `master --recheck-house`: 1,735 house-flagged estates re-read; 650 tower blocks (516 + 134) and 28,979 deals loaded | **Fixed in code**: `process_estate` judges each block (2134192), so a normal pass loads tower blocks. On a fresh database the recheck would re-read ~1,545 pure-house estates for nothing (I ~15 h). |
| R6 | 10-23 18:29 night chain (V: `night_chain_20261023.log`) | `geo --stations-only`, because the running geo process (PID 16964) had the old tail | **Fixed in code**: `geo.main` runs `mark_mismatches`, `assign_mtr`, `set_building_ages`, `fill_districts` after geocoding (570bd54, 0297753, b9dcda9). |
| R7 | 10-16 18:25 (V: `ricacorp_20261016_182510`, 0 pages) | Placement-only Ricacorp run; open item asks for one more after the master pass | **Fixed in code**: `run_ricacorp` places deals at the end of every pass. Needed only if Ricacorp runs before the master list. |
| R8 | open items; never run (V: no `link_*.log`) | `hkestate link-ids` (pilot links + re-link every loaded estate) | **Still needed on a fresh run**, once, after steps 4 and 5. Today's DB: **unclear** whether pilot `source_map` rows exist; check `SELECT matched_by, count(*) FROM source_map GROUP BY 1` (short query). A scratch script launched the 10-16 territory run (V: `territory_28hse.err` path); it may have linked. |
| R9 | 10-16 19:34-19:40 (V) | `backfill-territory --discover` found 0 estates; the sitemap parser was fixed (df57c76) and the run restarted with `--discover` | **Still needed on a fresh run**: `--discover` on the first territory run, or the estate index is empty. The fix itself is in code. |
| R10 | 10-17 00:33, 10-19 20:12-20:25, 10-20 08:33 (V) | Long jobs died: empty `executemany`, database file busy, 429s; restarted by hand, then by the watchdog | **Fixed in code**: 3799c4f, 5bd9069/65d9d25 (wait for the file as long as it takes), 3384656 (patience, adaptive pacing). Restarts used default arguments (V: command lines). |
| R11 | 10-20 21:19 (V: `*_manual_2119.out`) | Daily, cd-history, ricacorp and backfill-territory restarted by hand to load the new pacing code | Not a data repair. Nothing needed. |
| R12 | 10-21 16:12 (open items) | `rank` killed by hand after 2 h 10 min holding the database; daily restarted | **Fixed in code**: 02b6e7c, ed1fad4 (database read about 1 min). |
| R13 | 10-16 01:37-02:46 (V) | Pilot backfill restarted three times while the 28Hse gap went 1 s → 2 s → 4 s | **Fixed in code**: 4 s gap and patient waits from the start. |
| R14 | 10-16 18:04 (9fa1ac7, e4b13d4) | `gain_pct` repaired on older centadata rows from `raw_json` | **Fixed in code, with an order rule**: `run_master` calls `backfill_gain_pct` at its end. The cross-check stores no `gain_pct` itself (V), so step 3 must finish before the last master run. |
| R15 | 10-21 17:49 (Task 4b), 10-23 13:39 (fd75ba5) | Floor parser improvements, "take effect at the next train" | **Fixed in code** for a fresh run: rows are parsed with today's parser at load. Today's DB: `unit.floor_num` keeps the parse of its load day (**unclear** whether `features.py` re-parses every label; plan 2026-10-21 Task 4b suggests it does). |
| R16 | 10-23 (878d84d), first daily run after it | Territory rematch of `contains`/`single` listings | Not a hand step: the daily job does it. A fresh run matches with today's rules. |
| R17 | 10-21 18:03 (V) | `train --compare-since 2010 --previous backup_20261018` | Comparison only. The kept model is a plain `train`. `data\model\backup_20261018\` can't be rebuilt. |
| R18 | 10-16 13:01-17:34 (V) | `listings` run by hand for the pilot before the daily task existed | Not needed: step 12. |
| R19 | 10-17 08:00 (V: exit 4551) | Device Guard blocked `hkestate.exe`; tasks moved to `python.exe -m hkestate` | Environment, not data. `register_task.ps1` handles it. |
| R20 | open item | Subsidised estates with no listing: "use `data/manual/master_overrides.csv`" | **Not done**: the file doesn't exist (V). Nothing to carry over. |

No other ad-hoc SQL appears in commits or docs (V: grep of docs and `git log --stat`; the only
by-hand SQL is R2).

#### 2.2 Steps whose fresh-run order differs from the historical order

| Fresh order | Historical order (V: log times) | Why it matters |
|---|---|---|
| Master (4) before territory backfill (5) | Backfill started 10-16 19:40 at ~1% of the master pass: 531 exact name matches, against 6,610 on 10-18 and 8,100 on 10-27 | Order "largest first", HOS/TPS skipping and per-estate building links all need the master list. Early estates ran unmatched. That is why step 6 exists. |
| Cross-check (3) before the last master run (4) | Same (10-16) | `gain_pct` on cross-check rows comes only from the master's end-of-run repair (R14). |
| SRPE (8) after the whole master list | SRPE ran 10-18 21:31 to 10-19 05:06 with the master pass 75% done; 261 of 532 developments unmatched; not rerun since (V) | Unmatched registers are stored without a building (step key `:u`). Today only 20.8% of SRPE rows have a building (open items). A fresh run will match far more, so SRPE counts will differ. |
| Geo (10), cd-history (7), schools (11) after **every** master building, house blocks included | Geocoding ended 10-23 18:29 and cd-history 10-21 13:58, before the 650 recheck blocks (10-23 → 10-26) | Today those 650 buildings have no coordinates, MTR distance, net or dated history (I from the log times; no geo or cd-history log after them). A fresh run has them. |
| Train, rank, report (13-15) last | Weekly train ran 10-25 12:00, in the middle of the recheck; the findings report is from 10-23, before it | Today's model and findings miss part of the recheck deals. |
| Ricacorp (9) after the master list | First pass 10-16, before the master list | Placement reruns every pass, so it mostly caught up (3,003 still unplaced on 10-25, V). |
| Link ids (6) after 4 and 5 | Never run | See R8. |
| Daily listings (12) after 4 and 5 | Daily since 10-16; 28Hse listings skipped almost every day because the backfill holds the `hse28` lock (V: daily logs) | Listing matching needs the master list; 28Hse listings need the backfill done. |

### 3. Files the code reads that are not in git

| Path | Read by | Exists today? | What a fresh clone needs |
|---|---|---|---|
| `data/manual/building_map.csv` | `match.load_manual_map` (listing matching) | no (V: `data/manual` missing) | nothing; optional |
| `data/manual/master_overrides.csv` | `cd_master.load_overrides` | no | nothing; optional |
| `data/manual/hse28_estates.csv` | `hse28_territory.read_csv` | no | nothing if `--discover` is used |
| `data/manual/mtr_stations.csv` | `geo.load_stations` | no | nothing (Wikipedia + ALS give all 97 stations) |
| `data/manual/mtr_lines_and_stations.csv`, `data/model/mtr_lines_and_stations.csv` | `dashboard_data.plan_data` | no | nothing (the snapshot's `mtr_lines.parquet` is used) |
| `data/cache/www.srpe.gov.hk/pdf/*` (377 register PDFs) | `srpe.run_srpe` (reuses a saved PDF), `srpe.reparse_cached` | yes; outside the dated cache, never pruned | downloaded by step 8 |
| `data/cache/<host>/<date>/*.html.gz` | `fetch.Fetcher` | yes, but see 4. | built by each step |
| `data/model/backup_20261018/` | `train --previous`; restore in docs/operations.md | yes | can't be rebuilt; optional |
| `data/model/snapshot/`, `fair_price.*`, `ranked_latest.parquet`, `data/reports/growth/<date>/` | rank, report, datacheck, dashboard, advisor | yes | built by steps 13-15 |
| `data/watchdog_state.json`, `data/locks/*.lock` | watchdog, `locks.py` | yes | created at run time |
| `data/raw/mortgage/*`, `data/raw/subsidised/*` | **nothing** (V: grep) | yes | nothing. They are the source pages behind the tracked `mortgage.py` tables and `src/hkestate/subsidised_estates.csv`. |
| Playwright Chromium binaries | `srpe.SrpeClient.login` | yes on this laptop | `python -m playwright install chromium` (undocumented) |

Tracked inputs that a rebuild relies on: `src/hkestate/keywords.yml`,
`src/hkestate/subsidised_estates.csv`, `PILOT_ESTATES` (backfill.py), `PILOT_ENTRIES`
(centadata.py), `listing_run.PILOT` (V: `git ls-files`).

### 4. Gaps for `hkestate rebuild`

**`--estates` (test subset) is missing on:** `master` (`--limit` only), `backfill-territory`
(no name filter), `cd-history` (works only for pilot groups: territory estates have no
`group_name`), `srpe`, `ricacorp`, `geo`, `schools`, `link-ids`. `backfill` and `crosscheck`
accept pilot names only. A 3-estate test with 爾巒 and a territory estate needs a new filter
in at least master, backfill-territory, cd-history and geo.

**`--max-requests` has nothing to hook into yet.** `Fetcher.requests_made` counts per
instance, and each step opens its own Fetcher. SRPE sends its requests through its own `httpx`
client (`SrpeClient.requests`) and a Playwright browser, so a Fetcher-only cap misses them.

**Interactive or browser needs:** only SRPE (headless Chromium ticks the disclaimer; no
person needed). It fails on a machine without `playwright install chromium`.

**"Done" is not the exit code.** `cmd_master` returns 0 even with errored estates left (14 on
10-18). `backfill-territory` returns 0 on status `partial`. `daily` returns 0 on `skipped` or
`partial`. cd-history leaves failed batches for the next run. The rebuild should rerun a step
until its own "to do" count is 0, or record the count it accepts.

**Saved pages can't be re-read for the test (Task 9).** Two causes, both V:
- The daily prune deletes cache day folders 7 or more days old for **every page** on
  hk.centanet.com, www.28hse.com and www.spacious.hk (`maintenance.LISTING_HOSTS`), not just
  listing pages. Today `hk.centanet.com` and `www.28hse.com` hold only 2026-10-21 to 10-28. All
  master-pass, pilot-backfill and most cd-history pages are gone. The 10-21 folder goes at the
  end of today's daily run.
- The fetcher ignores a cached page older than its type's lifetime (`PAGE_LIFETIME_DAYS`:
  listing 1, transaction 7, static 30 days). Master, cross-check, cd-history and Ricacorp
  pages are "transaction" type.

So a rebuild into an empty test folder with `HKESTATE_CACHE_DIR = data\cache` will make live
requests for nearly every 28Hse and Centadata page. Only ALS (30 days, 10-16 and 10-23 folders),
EDB, RVD and Wikipedia will come from the cache.

**A test rebuild beside the live jobs doubles the request rate.** Locks live in
`<data dir>\locks`. A test with its own data folder doesn't see the live `hse28` lock. It would
read 28Hse at 4 s next to the running territory backfill, which the lock exists to prevent.
The same applies to hk.centanet.com. Pace is per process, so neither job slows for the other.

**Other rebuild traps:**
- SRPE's resume key holds the date, so a resume on a later day repeats 532 detail calls.
- Ricacorp resumes only within an ISO week. The Sunday task starts on the last day of the
  week, so a pass begun Sunday restarts on Monday. On 10-25 the Sunday run read 0 pages because
  the week's pass was already done (V).
- The watchdog watches backfill-territory, cd-history and ricacorp logs in the data folder. If
  the rebuild dies, the watchdog may restart one step on its own. A later `hkestate rebuild`
  then finds its lock held (`not started`, exit 1).
- The running `daily` prune also deletes the rebuild's own cache after 7 days. That is harmless
  for correctness, but it means the rebuild can never be replayed from the cache.

**Why two rebuilds differ, apart from site data changing:**
1. Listing history (known) **and Ricacorp data** can't be rebuilt. Ricacorp pages show only the
   last ~90 days of deals, and the code deletes each page after parsing. Today's
   `ricacorp_deal`/`deal_pair` rows from the weeks of 10-16 and 10-19 exist only in this
   database. The plan names only the listing limit.
2. Listings feed the model and the study. Facing and sea view from a listing are lent to every
   deal of that flat. Rooms come almost only from listings. Rent yields, the asking gap and
   the rent anchor come from current listings (decisions.md, known-issues.md). A rebuild on a
   different day has different listings, so a different model, even on the same deals.
3. Step order (2.2): SRPE matching, geocoding and cd-history coverage depend on how complete
   the master list was when they ran.
4. The territory backfill's estate list is the sitemap plus every estate seen in 28Hse listing
   cards (`refs_from_listings`). Running `daily` before step 5 adds estates.
5. Errored items (master estates, cd-history batches, backfill estates) stay undone until a
   rerun.
6. The training data is date-relative. The 28Hse agreement lag is measured at each train (14
   or 15 days, V). RVD months depend on the load day. Report folders and the
   `recent-2024-now` window end on the run date.
7. ALS returns its closest guess, and Wikipedia coordinates can be edited.
8. The model itself is deterministic (`seed: 7`, `deterministic: True`, 4 threads; V
   `model.py`).

Smaller doc errors: docs/operations.md says cd-history waits "6 s between calls" while the code
is 2.0 s (`centadata_history.MIN_INTERVAL_S`). The Ricacorp pass is "about 7 hours" in the docs
and about 30 h in the logs. Test runs write `schools_*.log` files into the real `data/logs`
(28 files on 10-23 show "1 of 1 geocoded buildings"; some carry a stale `command line:
report ...`). So `_logger` isn't isolated in tests.

### 5. Full-rebuild time estimate

Lanes (one host each; lanes run at the same time):

| Lane | Steps in order | Time |
|---|---|---|
| www.28hse.com | 2 pilot backfill → 5 territory backfill | 17 h + I ~11-12 days ≈ **12-12.5 days** |
| hk.centanet.com | 3 cross-check (after 2) → 4 master → 7 cd-history; Centanet listings in 12 | 8 min + I ~2.5-3 days + ~20 h ≈ 3.5-4 days, starting after 17 h |
| www.als.gov.hk | 10 geo (after 4) → 11 schools | 17 h + 23 s |
| www.srpe.gov.hk | 8 SRPE (after 4) | 7.6 h |
| www.ricacorp.com | 9 Ricacorp (after 4) | ~30 h |
| small hosts | 1 RVD, EDB, Land Registry | minutes |
| no host | 6 link-ids → 12 daily (~14 h) → 13-16 train, rank, report, datacheck (~20 min) | ~15 h after the 28Hse lane ends |

- **In parallel by lane:** the 28Hse lane sets the length. Every other lane finishes inside
  it, since the centanet lane plus geo ends by about day 5. Then link-ids, one daily run and the
  model steps add about 15 h. Total **about 13 days** (I).
- **Strictly one step at a time:** 17 h + 8 min + ~2.75 days + ~11.5 days + ~20 h + 7.6 h +
  30 h + 17 h + 14 h + ~0.5 h ≈ **about 19 days** (I).
- Daily listings can run during the rebuild for the Centanet and Spacious lanes. Its 28Hse
  part waits for the backfill's lock. Running it before step 5 adds estates (4., item 4).
