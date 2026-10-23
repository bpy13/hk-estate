# Plan 2026-10-23: estates wrongly skipped as "house"

**Goal.** Bring the tower blocks of estates that Centadata flags as "house" into the master
building list, with their deal history, so their listings can be matched and ranked.

**Why.** The investigation of 2026-10-23 found 11,098 active sale listings without a building
after the link fix. About 3,850 of them sit in estates whose master entry says
`exclude_reason = 'house'`: `exclusion_reason()` in `src/hkestate/sources/cd_master.py` drops
the whole estate when the `2-` page's `BuildingInfo.isHouse` is true, so none of its blocks or
deals were loaded. Many are mainly towers: 愉景灣 (301 listings), 爾巒 (102), 逸瓏灣 (84),
Monterey (83), 天賦海灣 (77), Savannah (74), 昇薈 (67), 嘉峯匯 (59), 尚城 (58), 貝沙灣 (51).
1,735 estates carry the flag; most are real house estates (錦繡花園, 匡湖居).

**Approach.**
1. Probe a few estates to learn what tells a house block from a tower block on Centadata.
2. Decide per block, not per estate: keep the estate, load its tower blocks and their deals,
   skip its house blocks.
3. Re-read only the house-flagged estates (a new `--recheck-house` option), at the master
   pass's pace, after tonight's geocoding chain.
4. Rematch territory listings and check the result.

**Constraints (verbatim from `docs/decisions.md`).**
- "**Coverage filter.** HOS, TPS (Tenants Purchase Scheme) and other subsidised flats are
  excluded, including ex-HOS with premium paid (different buyer pool). Villas and village
  houses are excluded; duplexes and penthouses are kept but flagged."
- Project rules (CLAUDE.md): scrape politely, obey robots.txt, at least 1 s between requests per
  site, never fetch a page twice (the fetcher's cache); never call paths robots.txt disallows.
- Database: `data/hkestate.duckdb` is shared with running jobs (geocoding, territory backfill,
  SRPE, the scheduled daily job). Short sessions only; no long-held connections.

**Acceptance criteria.**
- A1. A house-flagged estate with tower blocks gets `master_building` rows for its towers and
  `txn_raw` Centadata deals for them; its house blocks get none.
- A2. A pure house estate (e.g. 錦繡花園) still loads no buildings.
- A3. After the recheck and a territory rematch, the count of active sale listings with a
  building rises; a random sample of 20 newly matched listings names the right building.
- A4. Tests and lint pass; the recheck is resumable and polite (same host pace as the master
  pass, cached pages not fetched again).

## Phase 1 – Probe and code (sonnet)

- [x] **Task 1: probe the house flag**
  - Files: none in the repo; findings go into this plan's Notes below.
  - Do: for 愉景灣, 爾巒, 昇薈, Monterey, 錦繡花園 and 匡湖居, read the `2-` page (already
    cached by the fetcher, so no request) and up to 4 block pages each (at most ~24 requests
    through `hkestate.fetch.Fetcher`, which keeps the pace). For each block record
    `BuildingInfo.isHouse`, `estateType`, `noOfFloors` / floors, unit count and block name.
  - Done when: a table in Notes shows, per block, those fields, and states the rule that tells
    a house block from a tower block (e.g. block-level `isHouse`, or floors <= 3).
  - Notes: does the `2-` page's `isHouse` describe the estate or only its first block?

- [x] **Task 2: decide per block, add `--recheck-house`** (after Task 1)
  - Files: modify `src/hkestate/sources/cd_master.py`, `src/hkestate/cli.py`; test
    `tests/test_cd_master.py` (or the existing master tests file).
  - Do (failing tests first, with fixture pages):
    - `exclusion_reason()` no longer returns `"house"` for the estate-level flag alone.
      Subsidised, villa and village rules stay as they are.
    - `process_estate()` loads each block but skips a block the Task 1 rule calls a house;
      it returns the count of skipped house blocks. An estate whose blocks are all houses is
      saved with `exclude_reason = 'house'` as today.
    - `hkestate master --recheck-house` processes only estates with
      `exclude_reason = 'house'`, resumable through `run_progress` under its own step keys
      (`house:<code>`), so the normal master steps are untouched.
  - Done when: `conda run -n hk-estate python -m pytest -q` and
    `conda run -n hk-estate ruff check .` pass, with new tests for: a mixed estate loads its
    tower block and skips its house block (A1); an all-house estate loads nothing (A2); the
    recheck selects only house-excluded estates and skips done steps (A4).

## Phase 2 – Live run (main session)

- [ ] **Task 3: run the recheck** (after tonight's chain: `geo --stations-only`, schools,
  `train`, `rank`, `report`)
  - Do: `conda run -n hk-estate hkestate master --recheck-house` in the background; log in
    `data/logs/master_<date>.log`.
  - Done when: the log ends with `done:` and its counts (estates, blocks, deals, house blocks
    skipped); 錦繡花園 still has 0 `master_building` rows (A2); 愉景灣 has tower blocks (A1).

- [ ] **Task 4: rematch and check**
  - Do: run `listing_run.match_territory` (the daily job's step), then count active sale
    listings with a building before and after; open 20 random newly matched ones.
  - Done when: the count rises and the 20 sampled listings name the right building (A3).
    Record numbers in `docs/open-items.md`.

## Notes
**Task 1 findings (2026-10-23, 23 new requests, cached `2-` pages reused).**

Does the `2-` page's `isHouse` describe the estate or the first block? Neither reliably. It
is true when the estate holds any house (it is true for 爾巒, 昇薈 and Monterey, whose first
block is a 20-31 floor tower). The block page of the *entry* block repeats that estate flag, so
the entry block's own `isHouse` says nothing. On every *other* block page (those with a
`buildingTypeCode`) `isHouse` is the block's own: False for towers, True for 洋房 / villas.
Exception: 錦繡花園 2座 and 3座 (2 floors, 4 units) read False, so the flag alone misses
low-rise houses.

| Estate (code) | Block | Entry block? | block isHouse | estateType | floors | units | Is it a house? |
|---|---|---|---|---|---|---|---|
| 愉景灣 7期璧如臺 (2-LIDHTHXXDT) | 1座 | yes | True | Phase | 1 | 1 | house |
| | 2座, 31座, 32座 | no | True | Phase | 1 | 1 | house |
| 愉景灣 5期頤峰 (2-LIDHTHXXZT) | 靖山閣 (1座) | yes | False | Phase | 24 | 142 | tower |
| | 韶山閣, 蔚山閣, 濤山閣 | no | False | Phase | 24 | 142-192 | tower |
| 愉景灣 12期 海澄湖畔二段 (2-WDPPWPPJES) | 悠澄閣 | yes | True (estate flag) | Phase | 24 | 111 | tower |
| | 閒澄閣 | no | False | Phase | 24 | 133 | tower |
| | 洋房15, 洋房17 | no | True | Phase | 1 | 1 | house |
| 爾巒 (2-BEPPWPPJPG) | 茵羅洛斯大道 1座 | yes | True (estate flag) | Normal | 20 | 56 | tower |
| | 茵羅洛斯大道 2座 | no | False | Normal | 23 | 63 | tower |
| | 迪維尼亞大道 洋房17, 洋房18 | no | True | Normal | 1 | 1 | house |
| 昇薈 (2-WBPPWPPYPP) | 1座 | yes | True (estate flag) | Normal | 31 | 152 | tower |
| | 2座 | no | False | Normal | 31 | 152 | tower |
| | 洋房7, 洋房8 | no | True | Normal | 1 | 1 | house |
| Monterey (2-YASPWPPHPY) | 1座 | yes | True (estate flag) | Normal | 13 | 90 | tower |
| | 2A座 | no | False | Normal | 13 | 51 | tower |
| | 洋房G, 洋房H | no | True | Normal | 1 | 1 | house |
| 錦繡花園 (2-LMDNZHNSHH) | 1座 | yes | True (estate flag) | Normal | 2 | 4 | house |
| | 2座, 3座 | no | False | Normal | 2 | 4 | house (flag misses it) |
| | 4座 | no | True | Normal | 1 | 1 | house |
| 匡湖居 1期 (2-DDDOQRUYOO) | 1, 2, 101, 102座 ("House A..K, Block n") | mixed | True | Phase | 1 | 1 | house |

**Rule.** A block is a house block when any of these holds:
1. `floors` is 1-3 (all real houses seen have 1-2; all towers 13+);
2. its name contains 洋房, 別墅 or "House" (the existing villa words);
3. it is not the entry block and its own page says `isHouse` true.
The entry block's `isHouse` is never used, because it repeats the estate flag. A block with
no floors figure falls back to rule 2 and 3 only. Pure house estates still end up with every
block skipped, so they are saved `exclude_reason = 'house'` as before. Estates often have
many blocks (爾巒 74, 匡湖居 1期 102). A block whose menu name already has a house word
(rule 2) is skipped without a request; the others need their page to read the floors.
