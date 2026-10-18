# Plan: Centadata per-flat sale history (2026-10-18)

**Goal.** Load every recorded sale per flat from Centadata, back to 1995, with dates, so that
the growth study uses real dated pairs instead of estimated ones before 2018.

**Approach.** Centadata's own block pages make one call per click to
`POST https://hk.centanet.com/findproperty/api/Transaction/Search` with body
`{"cuntcodes": [<flat code>, ...]}`. robots.txt allows this path; it forbids only
`/centanetapi/api/`. No login is needed. Probed on 2026-10-18 (4 requests):
- One call for 太古城 洞庭閣 27H returned both sales (1996-09-20 HK$3.39m, 2011-03-04 HK$5.86m).
- Each row carries `id`, `insDate`, `regDate`, `transactionPrice`, `gainPercent`, `nArea`,
  `gArea`, `prevInsDate`, `prevRegDate`, `prevTransactionPrice`, `heldDay`, `cuntcode`, `yAxis`,
  `xAxis`, `buildingName`, `estateName`, `bigEstateName`, `firstOrSecondHand` and more.
- 50 codes in one call worked, but the result is paged: count 82, 10 rows per page.

**Constraints** (from decisions.md and CLAUDE.md):
- Obey robots.txt.
- At most 1 request a second per site in total. The master pass also reads hk.centanet.com
  at 2 s, so this job uses 4 s.
- Cache every response.
- No personal data: keep only the sale fields listed above, no agent or owner fields.
- Never evade blocks: our normal User-Agent, no login.
- Data stays out of git; fixtures are trimmed or synthetic.

**Acceptance.**
- `hkestate cd-history` loads full sale histories for the territory, largest estates first.
- It is resumable.
- Pilot check: 太古城 洞庭閣 27H has its 1996 sale.
- The growth study's recorded pairs include dated Centadata pairs before 2018.

## Phase 1 – Centadata history (sonnet)

- [ ] **Task 1: POST support in the fetcher**
  - Files: `src/hkestate/fetch.py`, `tests/test_fetch.py`
  - Done when: tests show that a JSON POST is checked against robots.txt, paced like a GET,
    retried on 429/5xx, cached under a key made of the URL plus the body, and sent with our
    User-Agent. pytest is green.
- [ ] **Task 2: paging and batch size**
  - Find the paging fields in the site's own script (`/findproperty/_nuxt/64295af.js` and
    `fb20c4e.js` are already in the cache). Pick the largest page size the server honours,
    testing 20, 50 and 100 with one request each. Record the result in `docs/sources.md`.
  - Done when: `docs/sources.md` has a "Centadata history" section with the call, the paging
    fields, the page size used, the robots.txt line, and the estimated run length.
- [ ] **Task 3: `hkestate cd-history` loader**
  - Files: `src/hkestate/sources/centadata_history.py`, `src/hkestate/cli.py`, `tests/`
  - Flat codes: take `cuntcode` from stored Centadata deals (`txn_raw.raw_json`), grouped
    by building. Estates go in order of flat count, largest first.
  - Batches: send one batch of codes per call, then read every page of the result.
  - Storing: each sale goes into `txn_raw` with `source='centadata'` and
    `source_id=<id>`. It is placed on the same building and unit as the master pass would
    place it (reuse its placement code).
    - Fields: `deal_date=insDate`, `registration_date=regDate`, `price_hkd`, `gain_pct`, the
      areas, and `raw_json` limited to the listed fields.
    - Rows already stored are left alone.
  - Progress: progress is kept per building so a restart resumes. Options: `--estates`,
    `--limit`, `--max-hours`. Pace is 4 s.
  - Coexistence: the job pauses while the `listings` lock is held. It uses the circuit
    breaker (stop after 5 consecutive errors) and writes a run_log row.
  - Done when: tests pass on trimmed fixtures, and
    `hkestate cd-history --estates taikoo-shing --limit 20` stores the 1996 sale of
    洞庭閣 27H. Check with a read-only query.
- [ ] **Task 4: dated Centadata pairs in the growth study**
  - Files: `src/hkestate/pairs.py`, tests
  - Consecutive dated sales of the same unit become `real` pairs, whatever the source.
    Duplicates of 28Hse deals are already removed by `txn_all`.
  - A `gain` pair whose start date is now known moves to `real`.
  - Done when: a test with a two-sale Centadata history makes one real pair. After the pilot
    load, `hkestate report` shows more recorded pairs that start before 2018.
- [ ] **Task 5: run it and document it**
  - Update `docs/operations.md`, `README.md` and `CLAUDE.md` (the command list).
  - The territory run is started in the background by the main session, not by the helper.
  - Done when: docs are updated; pytest and ruff are green.
