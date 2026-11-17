# Weekly Centadata sales, and Spacious off (2026-11-17)

**Goal.** New sales across the whole territory reach the database every week. Today only the
5 pilot estates get new sales (the Sunday `update`, 28Hse pages). The ~2 million Centadata
sales came from one-off runs (`master`, `cd-history`), so territory sales stop in late October.
Also stop reading Spacious, which a Cloudflare check has blocked since 2026-10-29.

**Approach.** Reuse the master pass. Each Centadata block page shows every flat's latest sale;
a flat sold since the last read shows a new sale. A new `hkestate master --refresh` re-reads the
block pages of every estate the master pass loaded (not excluded, not pilot), once per ISO
week, resumable. Cached `transaction` pages live 7 days (`fetch.PAGE_LIFETIME_DAYS`), so a
weekly read fetches fresh pages. A scheduled task runs it Friday 20:00 so it ends before the
Sunday 12:00 `train`.

**Constraints (from docs/decisions.md and CLAUDE.md, verbatim):**
- "Scrape politely: obey robots.txt, at least 1 request/second per site, identify a cache so
  pages are never fetched twice. Never call paths robots.txt disallows (e.g. centanet
  `/centanetapi/api/`)."
- "Never store personal data: no owner/buyer names, no agent names or phone numbers (keep
  only the listing URL)."
- "Dropped, blocked: Squarefoot (and its haunted-flat list), house730 and Midland all answer
  HTTP 403 to our user agent, even for robots.txt. We do not try to get round it."

**Owner's calls (2026-11-17):** option (a), a weekly territory Centadata job; mark Spacious
blocked.

**Acceptance criteria.**
1. `hkestate master --refresh` re-reads every loaded estate's blocks once per ISO week, adds
   only sales not already stored, resumes after a stop, and does nothing more that week once
   done.
2. The daily job no longer reads Spacious; `check-blocked` checks Spacious monthly.
3. A scheduled task runs the refresh weekly; docs say how.
4. A first live run adds new territory sales, with its pace and length measured.

## Phase 1 – Build (sonnet)

Both tasks edit `src/hkestate/cli.py`: each helper works in its own git worktree; the
coordinator merges.

- [ ] **Task 1: `master --refresh`** `[parallel-ok]`
  - Files: modify `src/hkestate/sources/cd_master.py`, `src/hkestate/cli.py` (flag on the
    `master` parser); test `tests/test_cd_master.py`.
  - Done when: (1) new tests pass: a refresh reads only estates with `master_estate.blocks > 0`
    and not pilot or excluded; a second refresh in the same week reads nothing; a refresh the
    next week reads them again; a sale already in `txn_raw` is not added twice; a stopped
    refresh resumes where it stopped. (2) `pytest tests/test_cd_master.py -q` and `ruff check .`
    pass. (3) Live trial on a copy-free subset: `hkestate master --refresh --estates <two small
    territory estates>` logs pages read and rows added, and a second run logs 0 to do.
  - Notes: step keys `refresh:<ISO year>-W<week>:<code>` in `run_progress`, so normal master
    steps are untouched. Refresh pace 3 s between requests (`REFRESH_INTERVAL_S = 3.0`; master
    stays 4 s): ~27,000 pages ≈ 22 h. Takes the same error, patience and `SourceBlocked`
    handling as `run_master`. Logs a final summary line: estates, blocks, deals added, errors,
    left. Accepts `--max-hours` (stop cleanly, resume next start). Excluded and house blocks
    are skipped as today; new blocks found on a page are loaded as the master pass would.

- [x] **Task 2: Spacious off** `[parallel-ok]`
  - Files: modify `src/hkestate/cli.py` (daily and `listings` default sources),
    `src/hkestate/blocked_check.py` (add Spacious), `scripts/daily.ps1` and
    `scripts/register_task.ps1` (descriptions only); tests `tests/test_blocked_check.py`,
    `tests/test_listing_run.py` if a test pins the source list.
  - Done when: tests pass; `hkestate check-blocked` run live logs a line for
    www.spacious.hk; `hkestate listings --help` shows default sources without spacious. The
    Spacious code stays (a `--sources spacious` run still works if the block lifts).

## Phase 2 – Schedule, docs, first run (sonnet)

- [ ] **Task 3: weekly task and docs** needs: Task 1
  - Files: `scripts/register_task.ps1` (new task `weekly centadata`, Fridays 20:00,
    `daily.ps1 -Command "master --refresh"`, 14-day limit not needed: 2-day limit), 
    `docs/operations.md`, `docs/sources.md` (Spacious blocked; Centadata weekly),
    `CLAUDE.md` (command list), `README.md` if it lists scheduled jobs.
  - Done when: the task is registered and `Get-ScheduledTask -TaskPath '\hk-estate\'` shows
    `weekly centadata` with next run Friday 20:00; docs name the command, schedule and lock.

- [ ] **Task 4: first live run** `(checkpoint)` needs: Task 3
  - Done when: started through the scheduled task (`Start-ScheduledTask`), not as a child
    process; after its first hour the log shows estates read, deals added and errors, and the
    measured pages per hour; the projected length is written to `docs/operations.md`. If it
    projects over 40 h, record the numbers in open-items for the owner.

## Shared resources
- Database `data/hkestate.duckdb`: the 28Hse territory backfill (process 37668) writes to it
  until ~2026-11-20. Tests use their own temp databases. Live trials keep to two estates.
- Host hk.centanet.com: shared with the daily Centanet listings (08:00).
