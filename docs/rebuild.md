# Rebuild from scratch

How to build the whole database again from an empty data folder. This page is for someone who
has just cloned the repo. All commands are Windows PowerShell, run from the repo folder.

## 1. Setup from a fresh clone

```powershell
conda env create -f environment.yml
conda run -n hk-estate python -m pip install -e ".[dev]"
conda run -n hk-estate python -m playwright install chromium
```

The last line downloads a headless browser (a browser with no window). The SRPE step needs it
to tick the site's disclaimer. Without it, that step fails.

The `data\` folder is created on first use. It is git-ignored. To keep it elsewhere, set
`HKESTATE_DATA_DIR`.

## 2. Run it

```powershell
conda run -n hk-estate hkestate rebuild
```

The command runs steps 1 to 16 below, one at a time, in order. Each step calls the same
command you would type by hand, so each step also writes its own log and its own `run_log`
row.

- **Resume.** The rebuild records each finished step in the database (`run_progress`, command
  `rebuild`). If you stop it and start it again, it skips the finished steps. A step that was
  half done carries on from its own saved progress.
- **Done means nothing is left to do.** A step with a to-do count runs again until the count is
  0, at most 3 tries. After 3 tries the step counts as failed. It and the steps that need it
  wait for a rerun; the other steps go on. The log names what is left. A step without a
  to-do count is done when it exits normally.
- **Log.** `data\logs\rebuild_<date>_<time>.log`. The same lines print to the console.
- **Lock.** The rebuild holds `data\locks\rebuild.lock`. A second rebuild cannot start. The
  watchdog restarts nothing while a live rebuild holds this lock.
- **Held host locks.** A step that uses a host lock (`hse28`, `cd-history`, `ricacorp`,
  `listings`) is deferred when another live process holds that lock. So is every step that
  depends on it. The other steps carry on. The log lists the deferred steps at the end. Run
  `hkestate rebuild` again when the locks are free.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every step is done. |
| 1 | Not started: another rebuild holds the lock. |
| 2 | A step still had work left after 3 tries. The steps that don't need it ran. The log names it. |
| 3 | Stopped: the request cap was reached (`--max-requests`). |
| 4 | Every step that could run is done. Some steps were deferred. |

### Test-only options

- `--estates a,b` runs a small subset. Give estate names, Centadata `2-` or `3-` codes, or
  pilot group names. The rebuild passes it to every step that takes a subset. The backfill,
  the cross-check and the listing run take only pilot estates, and the rebuild skips them when
  the subset has none. A subset has its own progress keys, so it does not mark the real steps
  as done.
- `--max-requests N` stops the whole process cleanly after N web requests (exit code 3). Pages
  read from the cache do not count.

Two more settings help a test run. `HKESTATE_CACHE_DIR` and `HKESTATE_LOCK_DIR` set the page
cache folder and the lock folder. A test can then write its own database but share the live
cache and see the live jobs' locks (docs/operations.md).

## 3. The steps

"Lane" is the website a step uses. Steps in different lanes could run at the same time, but the
rebuild runs them one at a time. Run times come from the 2026-10 logs. "Est." means estimated
from other logs, not measured. "Done when" is how `rebuild.py` decides a step is finished.

| # | Step | What it does | Lane | Run time | Done when |
|---|---|---|---|---|---|
| 0 | Setup | The three setup commands above. | none | minutes | You ran them. The rebuild does not check. |
| 1 | `rvd` | Loads the government's price and rent indices. | rvd.gov.hk | 56 s | The command exits normally. |
| 2 | `backfill --estates pilot` | Reads every 28Hse deal for the pilot estates. Holds the `hse28` lock. | 28hse.com | 16.7 h | Every pilot estate has its last step recorded. |
| 3 | `crosscheck --estates pilot` | Compares the pilot estates with Centadata and stores the deals 28Hse lacks. Then fills `gain_pct` (percent gain) on the Centadata rows. | hk.centanet.com | about 8 min live | The command exits normally. Needs step 2 done. |
| 4 | `master` | Builds the master list of estates, buildings and each flat's latest deal from Centadata. | hk.centanet.com | about 2.5 to 3 days (est.) | The newest `master` run ended `ok` and reports 0 estates left. |
| 5 | `backfill-territory` | Reads 28Hse deals for every estate in Hong Kong, largest first. The first run on an empty database uses `--discover` to find the estates from the sitemap. Holds the `hse28` lock. | 28hse.com | about 11 to 12 days (est.) | The to-do count of 28Hse estates is 0. Needs steps 2 and 4. |
| 6 | `link-ids` | Links 28Hse estates and buildings to the master ones. | none | short (est.) | The command exits normally. Needs steps 4 and 5. |
| 7 | `cd-history` | Loads every recorded sale of each flat, back to 1995. Holds the `cd-history` lock. | hk.centanet.com | about 19 to 20 h (est.) | The count of buildings still to do is 0. Needs steps 3 and 4. |
| 8 | `python -m hkestate.sources.srpe` | Loads first-hand sales from the SRPE site, using the headless browser. | srpe.gov.hk | 7.6 h | The newest `srpe` run ended `ok` and reports 0 developments left. Needs step 4. |
| 9 | `ricacorp` | Reads Ricacorp estate pages, then places the deals in buildings. Holds the `ricacorp` lock. | ricacorp.com | about 30 h | The newest `ricacorp` run ended `ok` and reports 0 pages left. Needs step 4. |
| 10 | `python -m hkestate.geo` | Looks up each building's coordinates, then the MTR distances, building ages and districts. | als.gov.hk | 17.1 h, tail 3 s | The count of buildings not yet tried is 0. Needs steps 2 and 4. |
| 11 | `python -m hkestate.sources.schools` | Gives each building a primary school net. | edb.gov.hk | 23 s | The command exits normally. Needs step 10. |
| 12 | `daily` | One listing run. A subset uses `listings --estates` instead, without the cache prune. Holds the `listings` and `hse28` locks. | hk.centanet.com, 28hse.com, spacious.hk | 4.5 to 12.1 h per run; the first pass is about 14 h | The newest listing run ended `ok` or `partial`. Needs steps 4 and 5. |
| 13 | `train` | Fits the fair-price model. | none | 5.7 to 9.5 min | The command exits normally. Needs steps 1 to 12. |
| 14 | `rank --from-snapshot` | Ranks today's listings against the model. | none | 1.8 min | The command exits normally. Needs step 13. |
| 15 | `report --settings standard --from-snapshot` | Runs the growth study for every window. Uses up to about 9 GB of memory. | none | 6.6 to 21 min | The command exits normally. Needs step 13. |
| 16 | `datacheck` | Checks the loaded data. Downloads 22 Land Registry files. | Land Registry open data | about 1 min | The command exits normally. Needs step 13. |
| 17 | Register tasks | See section 5. The rebuild does not do this. | none | seconds | You ran the scripts. |

Notes on the order:

- The master list (step 4) comes before the territory backfill (step 5). The backfill needs it
  to order estates, skip subsidised estates and link buildings.
- Geocoding, `cd-history` and the school nets run after the whole master list. Then they cover
  every building.
- `train`, `rank` and `report` run last, so they see all the data.

## 4. How long a full run takes

- **About 19 days** when the steps run one at a time, as `hkestate rebuild` runs them.
- **About 13 days** if the lanes ran in parallel. The 28Hse lane (steps 2 and 5) sets the
  length. Every other lane finishes inside it.

Both figures are estimates. They assume the sites keep today's pace limits.

## 5. After the rebuild: register the scheduled tasks

The rebuild loads data. It does not create the Task Scheduler jobs. Register them once:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\watchdog.ps1 -Register
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_dashboard_task.ps1
```

Do this after the rebuild ends. The daily listing job and the watchdog would otherwise compete
with it for the same websites. See docs/operations.md for what each task does.

## 6. Limits: what a rebuild cannot recover

- **Listing history.** The database keeps one price per listing per day and the listings that
  have since gone. A rebuild sees only today's listings. The old asking prices cannot be
  rebuilt.
- **Ricacorp deals.** The site shows only about the last 90 days of deals. The code deletes
  each page after parsing. Older deals that only this database holds cannot be fetched again.
- **A different day gives a slightly different model.** Facing, sea view, rooms, rent yields
  and the asking gap all come from the listings of that day. Listings differ from day to day.
  The model itself is deterministic, so the same data gives the same model.
- **No replay from the cache.** The daily prune deletes saved pages after 7 days on three hosts
  (hk.centanet.com, www.28hse.com, www.spacious.hk). The fetcher also ignores a cached
  transaction page older than 7 days. So a second rebuild makes live requests again.
- **Other differences.** Errors leave an item undone until a rerun. ALS returns its closest
  guess. Wikipedia coordinates can be edited. Report folders carry the run date.

## Test run (2026-10-28)

Two estates, 爾巒 (`2-BEPPWPPJPG`, house-flagged, 26 tower blocks) and 凱帆軒 (`2-EGPPWPPEPW`,
3 blocks), rebuilt into an empty test data folder that shared the live page cache and locks:

```powershell
$env:HKESTATE_DATA_DIR = "<repo>\data\rebuild_test"
$env:HKESTATE_CACHE_DIR = "<repo>\data\cache"
$env:HKESTATE_LOCK_DIR = "<repo>\data\locks"
conda run -n hk-estate hkestate rebuild --estates 2-BEPPWPPJPG,2-EGPPWPPEPW --max-requests 1500
```

| Step | Result | Time |
|---|---|---|
| rvd | done | 8 s |
| backfill, crosscheck | skipped: no pilot estate in the subset | - |
| master | done: 29 blocks, 1,603 sales, 48 house blocks skipped (pages mostly cached) | 29 s |
| backfill-territory, link-ids | deferred: the live territory backfill held the `hse28` lock | - |
| cd-history | done: every building | 5.6 min |
| srpe | failed in the first 3 runs (the site was very slow, then sent empty answers); done in the 4th: 534 developments listed, none in these two estates | 5.2 min |
| ricacorp | done: 2 estate pages, 18 deals, 8 placed | 9 s |
| geo | done: 29 of 29 buildings geocoded, MTR distance set | 11 s |
| schools | done: school net for 29 of 29 buildings | 12 s |
| daily | skipped: a subset listing run takes only pilot estates | - |
| train, rank, report, datacheck | deferred: they wait for the 28Hse backfill and link-ids | - |

Compared with today's database (row counts for the two estates):

| | Test rebuild | Today's database | Why they differ |
|---|---|---|---|
| Buildings | 26 + 3 | 26 + 3 | same |
| Centadata sales, 爾巒 | 1,083 | 724 | today's database never ran cd-history on the tower blocks the house recheck added (audit, section 2.2) |
| Centadata sales, 凱帆軒 | 2,524 | 2,523 | one newer sale (2026-10-06) |
| Buildings with coordinates, MTR distance, school net | 26 + 3 | 0 + 3 | today's database geocoded before the house recheck added 爾巒's towers (audit, section 2.2) |
| Ricacorp sales | 0 + 3 | 0 + 9 | Ricacorp shows only ~90 days of deals; today's database kept older weeks (a limit above) |
| Flats (`unit`) | 724 + 879 | 727 + 880 | not checked; probably flats known only from 28Hse or listings, steps the test didn't run |

Resuming works: each rerun skipped the steps already done. The test found and fixed three
faults: a step left out by the subset held up its dependants (cd-history waited for the pilot
cross-check); the SRPE page limit of 90 s was too short; and one failed step stopped every
later step. Now a failed step waits like a locked one and the others go on.

Not tested: the 28Hse steps (backfill, territory backfill, link-ids) and the model steps,
because the live territory backfill held the 28Hse lock. Their times come from the logs.

Time estimate: the test confirms the audit's per-step times where it ran; the full run stays at
about 19 days run one step at a time, set by the 28Hse backfill (~12 days).
