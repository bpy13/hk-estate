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

- [ ] **Task 1: peer-ask check and shortlist flag**
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

- [ ] **Task 2: repeat-price rule** `[parallel-ok]`
  - Files: modify `src/hkestate/features.py` (`deal_features`, `EXCLUDE_ORDER`); test
    `tests/test_features.py`.
  - Do (failing tests first): new condition `repeat_price`: a deal whose same flat (same
    `building_id`, `floor_num`, `flat`, all non-null) has an earlier eligible deal with a price
    within 0.1%, any gap. The earlier deal stays; the later one is excluded. Place it in
    `EXCLUDE_ORDER` just after `duplicate`.
  - Done when: `conda run -n hk-estate python -m pytest -q tests/test_features.py` passes with
    tests for: 20,565,750 then 20,566,000 five years later excludes the second; a 1% different
    price is kept; a flat with null floor is untouched.

- [ ] **Task 3: check on real data** (main session, after Tasks 1-2)
  - Do: `conda run -n hk-estate hkestate rank --from-snapshot`; read the CSV for the 9 Yoho
    ids (CVY014, GAN573, GAN591, CPJ448, CQA348, CQA401, CQS644, CQT017, CQS970) and count
    `repeat_price` rows in the snapshot, including Grand Yoho 5座 10/C.
  - Done when: A1 and A2 hold on real data; counts recorded in `docs/open-items.md`.

## Phase 2 – Audit (opus, read-only)

- [ ] **Task 4: list every step and hand repair**
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

- [ ] **Task 5: cache folder override**
  - Files: modify `src/hkestate/config.py`, `src/hkestate/fetch.py` (wherever the cache path
    is built); test `tests/test_config.py`.
  - Do: env var `HKESTATE_CACHE_DIR` sets the raw-page cache folder (default
    `<data dir>/cache`), so a test rebuild can read the saved pages while writing a new
    database.
  - Done when: `pytest -q tests/test_config.py tests/test_fetch.py` passes with a test that
    the override is honoured.

- [ ] **Task 6: `hkestate rebuild`**
  - Files: create `src/hkestate/rebuild.py`; modify `src/hkestate/cli.py`; test
    `tests/test_rebuild.py`.
  - Do: one ordered list of steps (from the audit), each a call to the existing command
    function; progress in `run_progress` under step keys `rebuild:<step>` so a rerun skips
    finished steps; one log `data/logs/rebuild_<time>.log`. Options: `--estates` (test-only
    subset, passed to every step that supports it; add support where missing) and
    `--max-requests N` (stop fetching once N live requests are made). No other options.
  - Done when: `pytest -q tests/test_rebuild.py` passes with tests for: steps run in order;
    a rerun skips done steps; a failing step stops the run and is retried next time.

- [ ] **Task 7: `docs/rebuild.md`**
  - Files: create `docs/rebuild.md`; modify `README.md` (one pointer), `docs/architecture.md`
    if it lists modules.
  - Do: setup from a fresh clone, then `hkestate rebuild`, each step with what it does and its
    run time, the scheduled tasks to register afterwards, and the limit on listing history.
  - Done when: every step in the audit appears, in order, with a run time.

- [ ] **Task 8: turn hand repairs into code** (contents from the audit)

## Phase 4 – Small test (main session)

- [ ] **Task 9: 3-estate rebuild**
  - Do: `HKESTATE_DATA_DIR` = `data\rebuild_test\`, `HKESTATE_CACHE_DIR` = `data\cache`;
    run `hkestate rebuild --estates <pilot estate>,爾巒,<one ordinary territory estate>
    --max-requests 500`. Compare row counts per table for those estates with today's database
    (short read-only sessions), time each step, estimate the full run from the timings.
    Delete `data\rebuild_test\` afterwards.
  - Done when: A5 holds; results and the estimate recorded in `docs/rebuild.md`.

## Notes
