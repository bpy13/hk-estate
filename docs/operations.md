# Operations

How the scheduled jobs run on the laptop, where to look when something goes wrong, and how
to run things by hand. Commands assume the repo folder and the `hk-estate` conda env.

## Scheduled jobs (Windows Task Scheduler)

Registered for the current user by `scripts\register_task.ps1` (rerun it to update them):

| Task (folder `\hk-estate\`) | When | Runs |
|---|---|---|
| `daily listings` | every day 08:00 | `scripts\daily.ps1` → `hkestate daily` (may run up to 23 h: every due Centanet page is read) |
| `watchdog` | every 15 minutes | `scripts\watchdog.ps1` → `hkestate watchdog` (restarts jobs that died; see Watchdog) |
| `weekly deals` | Sundays 07:00 | `scripts\daily.ps1 -Command update` → `hkestate update` |
| `weekly ricacorp` | Sundays 10:00 | `scripts\daily.ps1 -Command ricacorp` → `hkestate ricacorp` (a pass is about 7 hours; the task may run 14) |
| `daily rank` | every day 14:00 | `scripts\daily.ps1 -Command rank` → `hkestate rank` (after the 08:00 listings, ~4.5 h; about 1-3 minutes, of which the database read is about 1 minute) |
| `weekly train` | Sundays 12:00 | `scripts\daily.ps1 -Command train` → `hkestate train` (after the deal update; about 5 minutes) |
| `monthly blocked-check` | 1st of each month 09:00 | `scripts\daily.ps1 -Command check-blocked` → `hkestate check-blocked` (see below; a few seconds) |

`daily.ps1` starts Python in its own hidden console (`Start-Process`) and waits for it, so a
Ctrl+C or a closed console window elsewhere can't stop the job (the daily job died that way
on 2026-10-20, exit 0xC000013A); the exit code is still the job's. Python's output lands in
the task log when the job ends.

The wrapper runs `<env>\python.exe -m hkestate <command>`, not `hkestate.exe`: Windows
Application Control (Device Guard) blocked the generated `hkestate.exe` launcher on
2026-10-17 (exit code 4551), while `python.exe` is allowed. `scripts\register_task.ps1` finds
the env's `python.exe` and passes it to `daily.ps1 -Python`; rerun it after moving or
rebuilding the env. By hand, `python -m hkestate <command>` is the same as `hkestate <command>`.

If the laptop was off at 08:00, the listings start late and the 14:00 rank uses the previous
day's listings; the next day's rank catches up. A long Centanet pass (the first one is about
14 hours) is still running at 14:00: rank then uses what is stored so far. The task may run
23 hours and never twice at once, so a run still going at 08:00 the next day makes that
day's start be skipped. The growth study (`hkestate report`) is not
scheduled: run `hkestate train` then `hkestate report` when
the territory backfill finishes, or whenever a fresh findings report is wanted.

All are set to **start as soon as possible after a missed start** (laptop off or asleep at
the time), never to run twice at once, and to run only while you are logged on (so no
password is stored). Check them with:

```powershell
Get-ScheduledTask -TaskPath '\hk-estate\' | Get-ScheduledTaskInfo
```

Each task runs `conhost.exe --headless powershell.exe ...`, so no window appears. Started
directly, `powershell.exe -WindowStyle Hidden` flashes a window and takes the focus on
Windows 11, where new consoles open in Windows Terminal (since 2026-10-20). Because of this,
the task's "Last Run Result" is always 0 (conhost's own exit code). A job's real exit code is
the last line of its `data\logs\task_<command>_<time>.log`.

Remove one with `Unregister-ScheduledTask -TaskPath '\hk-estate\' -TaskName 'daily listings'`.

## What the daily job does (`hkestate daily`)

For each pilot estate group (`listing_run.PILOT`). The three sources are different hosts and
run **at the same time, each in its own thread**, so 28Hse and Spacious don't wait behind a
long Centanet pass; steps 4-5 run once all three are done. Database writes from the threads
take turns (`db.session` holds a per-process lock: DuckDB lets threads open the same file
but two writing the same row at once fail with "Conflict on update"). `--sequential` runs
them one after another.

1. **Centanet**: reads the detail sitemap (every live listing), then detail pages for new
   listings plus a rotating seventh of known ones (the sitemap's dates are useless, so this
   is how price changes are caught: every listing is re-read within 7 days). 2 s between
   requests (slower when the site pushes back, see Pacing), holding the `centanet` lock as a
   marker (see Locks). Reads are recorded every 25 pages, so a run stopped midway doesn't
   read those pages again.
2. **28Hse**: the estate's search pages (sale and rent), and a detail page only for a new
   listing or a changed price. 4 s between requests (28Hse rate-limits hard).
3. **Spacious**: the neighbourhood search pages (sale), cards only.

When a site pushes back (429/503, a run of 5xx, timeouts) a source cools down 10, 20, then
30 minutes and carries on from the same page; after the third cool-down it gives up for the
day (noted in `run_log`, status `partial`). Other errors (e.g. HTTP 400) five pages in a row
still stop that source for the run; 404 means the listing is gone.

4. Stores changed listings in `listing_raw`, updates `listing`, writes one
   `listing_snapshot` row per listing per HK day, marks listings missing from 3 run days in
   a row as delisted, matches every listing to a building and floor band, groups cross-site
   duplicates (`dup_group`) and links relistings (`relist_of`).
5. Writes keyword flags to `risk_flag` (rules in `src\hkestate\keywords.yml`).
6. Deletes cached listing pages 7 or more days old (`data\cache\<site>\<date>\`): they show
   agents' names and phones, which are never stored in the database.

A first run reads every pilot listing's detail page (about 2,500 on Centanet: ~1.5 hours at 2 s).
Later runs read only new and rotating pages (minutes).

## Territory listings (from 2026-10-16)

`hkestate daily` now runs `--estates all`: the pilot groups plus a `territory` group. Per run
Centanet reads the whole sitemap and **every due detail page** (new listings first, then the
oldest read) at 2 s: the first pass is about 26,000 pages (~14 h), after that about 6,000 a
day (new plus a seventh of known, ~3.5 h). Spacious reads every building sale page at 1 s.
There is no cap by default since 2026-10-20 (owner's decision; the earlier caps of 30,000 and
6,000 were a run-length choice); `--centanet-budget N` / `--spacious-budget N` set one for a
manual run. 28Hse is pilot only. Territory listings are matched to master (`cd-`) buildings from the
Centanet block code, else by estate then block name; the rest stay unmatched until the master
list covers their estate. Use `--estates pilot` for the quick pilot-only run.

## Locks: jobs that must not overlap

Lock files live in `data\locks\` and hold the process id of the job that owns them; a lock
whose process has ended is ignored.

- `hse28.lock`: held by `hkestate backfill` and `hkestate update`. While it is held, the
  daily job **skips 28Hse** for that day (it would double the request rate and trigger 429s)
  and still runs Centanet and Spacious. The skipped source is noted in the run's message and
  the day doesn't count towards delisting 28Hse listings.
- `listings.lock`: held by `hkestate daily` / `hkestate listings`; a second listing run
  started meanwhile records `skipped` and exits.
- `centanet.lock`: held by the listing job only while it reads Centanet; a second Centanet
  listing run skips Centanet while it is held. Since 2026-10-20 it is only a marker for the
  other hk.centanet.com jobs: `hkestate cd-history` (6 s) and `hkestate master` (4 s) no
  longer pause for it and run alongside the listings (2 s), together about 0.67 requests a
  second (0.92 with the rare master pass), below the ~1 a second that ran for days.
- `cd-history.lock`: held by `hkestate cd-history`, so two copies cannot run.
- `ricacorp.lock`: held by `hkestate ricacorp` (from 2026-10-20), likewise.

The watchdog uses these locks to tell a live job from a dead one (a job sleeping through a
cool-down holds its lock, so it counts as running).

The database itself is shared: every job opens it only for short reads and writes, and a job
that finds it busy retries (`db.connect`: 5 s between tries, doubling to 60 s; the first 10
waits are logged as "database busy" in the task log, then one line every 10 minutes).
Short and interactive commands give up after 30 minutes. The long jobs (`backfill`,
`backfill-territory`, `cd-history`, `ricacorp`, `master`) **wait for as long as it takes**:
on 2026-10-19 all three long jobs died after 30 minutes of another process holding the
file. Only the 'file in use' error is retried; any other error stops the job. `train`, `rank` and `report` read their data
into memory in one session (a few minutes), close the file, then fit or score without it.

## Centadata sale history (`hkestate cd-history`)

Loads every recorded sale of each flat (back to 1995) from Centadata, for the growth study.
Not scheduled: it is a one-off territory run (about 29 hours, see docs/sources.md "Centadata
history"), then it can be repeated now and then to pick up buildings added by the master pass.

```powershell
conda run -n hk-estate hkestate cd-history                       # whole territory, largest estates first
conda run -n hk-estate hkestate cd-history --estates taikoo-shing --limit 20   # a quick trial
conda run -n hk-estate hkestate cd-history --max-hours 8         # stop after 8 hours; rerun to continue
```

- Flat codes come from the latest deals already stored (`txn_raw`, source `centadata`), so the
  master pass (`hkestate master`) must have covered an estate first. A flat whose latest deal
  matched 28Hse in the pilot cross-check was not stored as a Centadata row, so it has no
  code yet (open item).
- One call carries up to 40 flats and returns 100 rows a page; 6 s between calls (4 s drew
  429s on 2026-10-20; the master pass and the Centanet listings share this host and run at
  the same time). Every answer is cached.
- Progress is kept per building (`run_progress`, command `cd-history`): a restart skips
  finished buildings, and a rerun adds nothing that is already stored. A building whose batch
  failed is tried again on the next run.
- `--limit N` counts buildings. When the site pushes back (429/503, 5xx, timeouts) the job
  **waits and asks for the same batch again, for as long as it takes** (see Pacing; on
  2026-10-20 the Transaction/Search API still answered 429 eleven hours after the first
  ones). A batch that fails with a 5xx three times on its own is skipped (its buildings are
  retried on the next run). Five other errors in a row (404, bad answers) stop the run; a
  403 or challenge page stops it for good. It
  holds the `cd-history` lock so two copies cannot run. It writes a `run_log` row
  (command `cd-history`) and `data\logs\cd-history_<time>.log`.
- After a load, run `hkestate train` and `hkestate report` to use the dated pairs.

## Pacing and throttling (from 2026-10-20)

Each job sets a base gap per host (never below 1 s or the robots.txt Crawl-delay): Centanet
listings 2 s, cd-history 2 s, master pass 4 s, 28Hse 4 s, Ricacorp 3 s, Spacious 1 s. The
fetcher adapts it per host, within the process (`fetch.py`):

- **Slow down**: HTTP 429 or 503, a 5xx still failing after its 3 retries, or a timeout
  multiplies the host's gap by 1.5 (at most 15 s). Logged once: `hk.centanet.com: slowing to
  3.0 s after HTTP 429`.
- **Cool down** (429/503 only): the host gets a cool-down of 15 min, then 30, 60, then 2 h
  each time (Retry-After is honoured if longer, up to 2 h). The next request waits it out
  and is a single probe. Logged: `... HTTP 429; cooling down 15 min (until 19:15), then one
  probe request`. The daily listing run uses 10/20/30 min instead and gives up a source for
  the day after the third.
- **Speed up**: after 100 good answers in a row the gap steps 25% back towards the base,
  never below it (`speeding up to 2.7 s`). After 100 good answers the cool-downs start again
  at the first.

The long jobs (`cd-history`, `backfill-territory`, `backfill`, `ricacorp`, `master`) never
exit on throttling: they wait and retry the same item (`patience.py`). Only one item failing
with a 5xx three times on its own is skipped, for the next run. These messages are in the
job's own log.

## Watchdog (`hkestate watchdog`, every 15 minutes)

Restarts a job that died instead of waiting for someone to notice. It never opens the
database: it reads lock files, the jobs' logs and `data\watchdog_state.json`.

- Watched: `backfill-territory` (lock `hse28`), `cd-history` (lock `cd-history`), `ricacorp`
  (lock `ricacorp`), and today's `daily`.
- **Running** = a live process holds the job's lock (a job asleep in a cool-down holds it),
  or its newest log was written in the last 45 minutes. **Dead** = not running and the newest
  log has no normal ending: `done: {...}` (cd-history), `ok:`/`partial: {...}`
  (backfill-territory, ricacorp), `daily: ok|partial|skipped` (daily). A log of a run that
  never started ("not started", "daily: skipped") is passed over. A source block (403) counts
  as an ending: blocked jobs are not restarted.
- A dead job is started again with the same arguments as the dead run (each log now has a
  `command line:` line; older logs get the defaults), detached from any console:
  `python.exe -m hkestate ...`, output in `data\logs\<job>_watchdog_<time>.out/.err`.
- `ricacorp` is only resumed within the ISO week of its last pass (the Sunday task starts a
  new pass). `daily` is only rerun for a run started today (HK date) and only between 08:00
  and 20:00; a rerun doesn't read again the pages already read today.
- At most 3 watchdog restarts per job per day. A job that dies within 10 minutes of a
  restart 3 times in a row is left alone: the log says `GIVING UP` with the job's name. Fix
  the cause, then delete that job's entry from `data\watchdog_state.json` (or start the job
  yourself; once it is seen running or finished the watchdog resumes).
- Every decision is one line in `data\logs\watchdog_<YYYYmmdd>.log`; a crash of the watchdog
  itself goes to `data\logs\watchdog_<YYYYmmdd>.err`.

Register it (once; `MultipleInstances IgnoreNew`, only while logged on, 10-minute limit):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\watchdog.ps1 -Register
# same as: scripts\register_task.ps1 -Only watchdog
```

Try it without starting anything: `conda run -n hk-estate hkestate watchdog --dry-run`, then
read today's `watchdog_<date>.log`.

## Monthly blocked-site check (`hkestate check-blocked`)

Midland, Squarefoot, house730 and HKP (www.hkp.com.hk) answer 403 even for robots.txt, so
we don't use them (docs/sources.md). Once a month the task requests each one's robots.txt a
single time, with our normal User-Agent and the normal polite fetcher; nothing is done to get
round a block. Per site it logs the status to `data\logs\check-blocked_<time>.log` and writes
one `run_log` row (command `check-blocked`, `args` = the site, `message` = host and status).
A site that now answers 200 gets the log line "now reachable, review docs/sources.md": read
that site's robots.txt by hand and update docs/sources.md before using it. Anything else (403,
5xx, network error) means nothing changed. Register just this task with
`scripts\register_task.ps1 -Only 'monthly blocked-check'`.

## Redirects

The fetcher follows redirects itself, at most 5 hops. Every hop's host gets its robots.txt
check and delay like a first request; a redirect into a disallowed path is not requested
(`RobotsDisallowed`, logged as a warning). The cache is keyed by the original URL.
## Logs and checks

- Task output: `data\logs\task_<command>_<time>.log` (written by `scripts\daily.ps1`).
- The job's own log: `data\logs\daily_<time>.log`, `update_<time>.log`, ...
- `run_log` table: one row per run with status `ok`, `partial` (a source skipped or
  incomplete; the reason is in `message`), `skipped`, `failed` or `running`.
- `hkestate status` prints the last runs.

## Manual commands

```powershell
conda run -n hk-estate hkestate daily                    # the daily job now
conda run -n hk-estate hkestate listings --sources centanet,spacious --estates taikoo-shing
conda run -n hk-estate hkestate update                   # newest 28Hse deals
conda run -n hk-estate hkestate prune-cache --days 7
conda run -n hk-estate hkestate status
conda run -n hk-estate hkestate datacheck                # data checks on the snapshot (below)
```

## Data checks (`hkestate datacheck`)

Writes `data\reports\datacheck_<date>.md`: sales per year against the Land Registry's count
of residential sale and purchase agreements (2005 on, open data; docs/sources.md "Land
Registry statistics"), duplicate sales left after `excl_duplicate`, odd values (dates,
prices, areas, price per sq ft, two sales of one unit on one day) and coverage by decade
and source, each with a verdict ("ok" / "look at this"). It reads `data\model\snapshot`
(written by the last `train`, `rank` or `report` that read the database) and **never opens
the database** unless you pass `--read-db`; it writes a log file but no `run_log` row.
`--no-fetch` skips the Land Registry download (22 small files, cached 30 days), leaving the
yearly table without a verdict. About a minute on 2.3 million deals. Nothing is changed or
deleted: odd rows are listed for a decision.

## Model and growth study by hand

`rank` took 2+ hours on 2026-10-21 and blocked the scrapers; fixed the same day (the duplicate
check no longer uses an OR join, `extract` reads the sales once): the database read is now
about 1 minute, the whole `rank` 1-3 minutes.

```powershell
conda run -n hk-estate hkestate train --from-snapshot              # ~5 min; --since YEAR, --compare-since YEAR, --previous DIR
conda run -n hk-estate hkestate rank --from-snapshot
conda run -n hk-estate hkestate report --settings standard --from-snapshot   # 5-15 min, up to ~9 GB
conda run -n hk-estate hkestate report --start 2008-10-01 --end 2021-09-30   # one window
```

- `train` writes `data\reports\model_eval.md`. The model from 2026-10-18 is saved in
  `data\model\backup_20261018\`; to restore it, stop the dashboard and run
  `Copy-Item data\model\backup_20261018\* data\model\ -Recurse -Force` (from the repo folder).
- `report --settings standard` runs `all`, `since-2000`, the phases `crash-1997-2003`,
  `recovery-2003-2008`, `boom-2008-2021`, `fall-2021-2024`, `recent-2024-now`,
  `bought-after-crash` (from Jul 2003) and the 1-year-hold sensitivities `fall-2021-2024-1y`
  and `recent-2024-now-1y`. A pair counts when both sales fall inside the window. It is not
  scheduled; run it after `train`. Results: `data\reports\growth\<date>\<setting>\`
  (`effects.csv`, `districts.csv`, `index.csv`, `returns.csv`, `settings.json`) and
  `data\reports\findings_<date>.md`.
- The dashboard page "Growth study" (`?page=growth&setting=<name>`) reads the newest
  `growth\<date>\` folder and needs no database.

## Manual building map

Listings whose building can't be matched automatically can be mapped by hand in
`data\manual\building_map.csv` (columns `source,building_text,building_id`; source `*`
means any site). `building_text` is the text in the `listing.building_text` column.
