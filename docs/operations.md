# Operations

How the scheduled jobs run on the laptop, where to look when something goes wrong, and how
to run things by hand. Commands assume the repo folder and the `hk-estate` conda env.

## Scheduled jobs (Windows Task Scheduler)

Registered for the current user by `scripts\register_task.ps1` (rerun it to update them):

| Task (folder `\hk-estate\`) | When | Runs |
|---|---|---|
| `daily listings` | every day 08:00 | `scripts\daily.ps1` → `hkestate daily` |
| `weekly deals` | Sundays 07:00 | `scripts\daily.ps1 -Command update` → `hkestate update` |
| `weekly ricacorp` | Sundays 10:00 | `scripts\daily.ps1 -Command ricacorp` → `hkestate ricacorp` (a pass is about 7 hours; the task may run 14) |
| `daily rank` | every day 14:00 | `scripts\daily.ps1 -Command rank` → `hkestate rank` (after the 08:00 listings, ~4.5 h; about 1 minute) |
| `weekly train` | Sundays 12:00 | `scripts\daily.ps1 -Command train` → `hkestate train` (after the deal update; about 1 minute) |
| `monthly blocked-check` | 1st of each month 09:00 | `scripts\daily.ps1 -Command check-blocked` → `hkestate check-blocked` (see below; a few seconds) |

The wrapper runs `<env>\python.exe -m hkestate <command>`, not `hkestate.exe`: Windows
Application Control (Device Guard) blocked the generated `hkestate.exe` launcher on
2026-10-17 (exit code 4551), while `python.exe` is allowed. `scripts\register_task.ps1` finds
the env's `python.exe` and passes it to `daily.ps1 -Python`; rerun it after moving or
rebuilding the env. By hand, `python -m hkestate <command>` is the same as `hkestate <command>`.

If the laptop was off at 08:00, the listings start late and the 14:00 rank uses the previous
day's listings; the next day's rank catches up. The growth study (`hkestate report`) is not
scheduled: run `hkestate train` then `hkestate report` when
the territory backfill finishes, or whenever a fresh findings report is wanted.

All are set to **start as soon as possible after a missed start** (laptop off or asleep at
the time), never to run twice at once, and to run only while you are logged on (so no
password is stored). Check them with:

```powershell
Get-ScheduledTask -TaskPath '\hk-estate\' | Get-ScheduledTaskInfo
```

Remove one with `Unregister-ScheduledTask -TaskPath '\hk-estate\' -TaskName 'daily listings'`.

## What the daily job does (`hkestate daily`)

For each pilot estate group (`listing_run.PILOT`):

1. **Centanet**: reads the detail sitemap (every live listing), then detail pages for new
   listings plus a rotating seventh of known ones (the sitemap's dates are useless, so this
   is how price changes are caught: every listing is re-read within 7 days). 1 s between
   requests, holding the `centanet` lock so the Centadata jobs on hk.centanet.com pause
   (see Locks). Reads are recorded every 25 pages, so a run stopped midway doesn't read those
   pages again.
2. **28Hse**: the estate's search pages (sale and rent), and a detail page only for a new
   listing or a changed price. 4 s between requests (28Hse rate-limits hard).
3. **Spacious**: the neighbourhood search pages (sale), cards only.
4. Stores changed listings in `listing_raw`, updates `listing`, writes one
   `listing_snapshot` row per listing per HK day, marks listings missing from 3 run days in
   a row as delisted, matches every listing to a building and floor band, groups cross-site
   duplicates (`dup_group`) and links relistings (`relist_of`).
5. Writes keyword flags to `risk_flag` (rules in `src\hkestate\keywords.yml`).
6. Deletes cached listing pages 7 or more days old (`data\cache\<site>\<date>\`): they show
   agents' names and phones, which are never stored in the database.

A first run reads every pilot listing's detail page (about 2,500 on Centanet: ~1.5 hours).
Later runs read only new and rotating pages (minutes).

## Territory listings (from 2026-10-16)

`hkestate daily` now runs `--estates all`: the pilot groups plus a `territory` group. Per run:
Centanet reads the whole sitemap but at most 30,000 detail pages (`--centanet-budget`; new
listings first, then the oldest read) at 1 s. Raised from 8,000 on 2026-10-19 so the rest of
the first pass (~26,000 pages, ~10 h) fits in one run; after that a run is about 6,000 pages
(new plus a seventh of known), ~2.5 h. Spacious reads up to 6,000 building sale pages (`--spacious-budget`) at 1 s: about 2 h.
28Hse is pilot only. Territory listings are matched to master (`cd-`) buildings from the
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
- `centanet.lock`: held by the listing job only while it reads Centanet (1 request a second,
  the host's whole allowance), not during 28Hse, Spacious or matching. While it is held,
  `hkestate cd-history` and `hkestate master` pause (checked every 60 s, logged once per
  pause) and carry on when it is released. A stale `centanet.lock` (its process crashed)
  makes nobody wait.
- `cd-history.lock`: held by `hkestate cd-history`, so two copies cannot run.

The database itself is shared: every job opens it only for short reads and writes, and a job
that finds it busy retries for up to 30 minutes (`db.connect`: 5 s between tries, doubling
to 60 s, each wait logged as "database busy" in the task log). Only the 'file in use' error
is retried; any other error stops the job. `train`, `rank` and `report` read their data
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
- One call carries up to 40 flats and returns 100 rows a page; 4 s between calls (the master
  pass and the Centanet listings share this host). Every answer is cached.
- Progress is kept per building (`run_progress`, command `cd-history`): a restart skips
  finished buildings, and a rerun adds nothing that is already stored. A building whose batch
  failed is tried again on the next run.
- `--limit N` counts buildings. The job **pauses while the `centanet` lock is held** (the
  listing job reading Centanet; its 28Hse and Spacious parts don't pause it), stops after 5
  errors in a row, stops for good on a 403 or challenge page, and
  holds the `cd-history` lock so two copies cannot run. It writes a `run_log` row
  (command `cd-history`) and `data\logs\cd-history_<time>.log`.
- After a load, run `hkestate train` and `hkestate report` to use the dated pairs.

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
```

## Manual building map

Listings whose building can't be matched automatically can be mapped by hand in
`data\manual\building_map.csv` (columns `source,building_text,building_id`; source `*`
means any site). `building_text` is the text in the `listing.building_text` column.
