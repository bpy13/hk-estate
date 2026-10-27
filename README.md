# HK Estate

A personal research tool for Hong Kong private residential flats. It collects transaction
history, current listings and flat attributes from free public sources, stores them in a local
DuckDB database, and analyses them to:

1. rank current listings by their discount to a modelled fair price, with risk flags,
2. measure which flat attributes are linked to better long-run price growth and total return, and
3. help one person think through a move (buy, rent, sell or let) with a questionnaire and a
   "market today" page.

Single user, non-commercial. Scraping is slow and polite (robots.txt obeyed, at most one request
per second per site, every page cached). No personal data is stored.

**Notice.** This repository holds code only: no scraped data, database or model is included.
It is a personal research project, not investment advice and not an estate agency service.
Each data source (Centadata, Centanet, 28Hse, Spacious, Ricacorp, the RVD, SRPE and others)
has its own terms of use; anyone running this code is responsible for following them. The
saved pages in `tests/fixtures/` are kept only to test the parsers. Licence: MIT
(see `LICENSE`).

## Setup (Windows PowerShell, conda)

```powershell
conda env create -f environment.yml
conda run -n hk-estate python -m pip install -e ".[dev]"
```

## Commands

```powershell
conda run -n hk-estate python -m pytest -q   # tests (offline, fixture pages only)
conda run -n hk-estate ruff check .          # lint

conda run -n hk-estate hkestate backfill --estates pilot   # 28Hse deal history (hours; resumable)
conda run -n hk-estate hkestate rvd                         # RVD price/rent indices and stock
conda run -n hk-estate hkestate crosscheck --estates pilot  # compare with Centadata
conda run -n hk-estate hkestate status                      # recent runs and row counts
conda run -n hk-estate python scripts/spot_check.py         # 50 deals vs the live site

conda run -n hk-estate hkestate datacheck # checks on the loaded sales: data/reports/datacheck_<date>.md (~1 min)
conda run -n hk-estate hkestate train     # fair-price model + data/reports/model_eval.md (~5 min)
conda run -n hk-estate hkestate rank      # data/reports/ranked_listings_<date>.csv (1-3 min)
conda run -n hk-estate hkestate report    # growth study, all data: data/reports/findings_<date>.md
conda run -n hk-estate hkestate report --settings standard   # all settings below (5-15 min, up to ~9 GB)
conda run -n hk-estate streamlit run app/dashboard.py   # dashboard at http://localhost:8501
# or start it at every logon, headless, restarted if it stops (once, no admin needed):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_dashboard_task.ps1
#   (task \hk-estate\dashboard, fixed port 8501; stock_advisor's dashboard uses 8502)

conda run -n hk-estate python -m hkestate.geo                 # coordinates for every building (ALS; ~19 h, resumable)
conda run -n hk-estate python -m hkestate.geo --stations-only # MTR stations, distances, district fill (minutes)
conda run -n hk-estate python -m hkestate.sources.schools     # primary school nets (EDB files), then assigns buildings
```

`datacheck` reads the saved snapshot and does not open the database (`--read-db` takes a fresh
snapshot first; `--no-fetch` skips the Land Registry download).

`train` options: `--since YEAR` trains only on sales from that year, `--compare-since YEAR`
also trains from that year and reports both, `--previous DIR` compares with an earlier model
folder. The model from 2026-10-18 is kept in `data\model\backup_20261018\`; to go back to it,
close the dashboard and copy its files over `data\model\`:

```powershell
Copy-Item data\model\backup_20261018\* data\model\ -Recurse -Force   # run from the repo folder
```

**Growth study windows.** `report --start YYYY-MM-DD --end YYYY-MM-DD` keeps a flat only when
both its sales fall inside the window (default: all data). `report --settings standard` runs
these windows from one snapshot (or `--settings name,name`; not together with
`--start/--end`, the presets carry their own dates); a flat sold twice needs a 3-year hold, so a
window with too few of them says so instead of giving a verdict:

| Setting | Flats sold twice counted |
|---|---|
| `all` | every one, 1995 on |
| `since-2000` | bought from 2000 |
| `crash-1997-2003` | bought and sold Oct 1997 - Jul 2003 |
| `recovery-2003-2008` | bought and sold Aug 2003 - Sep 2008 |
| `boom-2008-2021` | bought and sold Oct 2008 - Sep 2021 |
| `fall-2021-2024` | bought and sold Oct 2021 - Oct 2024 |
| `recent-2024-now` | bought and sold since Nov 2024 |
| `bought-after-crash` | bought from Jul 2003 (does the crash drive `all`?) |
| `fall-2021-2024-1y`, `recent-2024-now-1y` | the last two phases again with a 1-year minimum hold, because the 3-year rule leaves almost no flats sold twice; labelled sensitivities, never mixed into the main verdicts |

The report and the dashboard use three terms: **flats sold twice** (two recorded sales; the
code calls them "pairs"), **model-valued flats** (a sale before 2018 against the model's
value today) and **Ricacorp reported gains** (the previous sale taken from Ricacorp's days
held and % gain). The model-valued check tends to understate real differences, because the
model learned partly from the same old sale; trust a "no effect" there less than the result
from flats sold twice.

The standard run takes 5-15 minutes (less when the scrapers are idle) and up to about 9 GB of
memory (close other big programs).
Results go to `data/reports/growth/<date>/<setting>/` (CSV files and `settings.json`);
the findings text is `data/reports/findings_<date>.md`.

The dashboard's **Growth study** page (sidebar, or `?page=growth&setting=<name>`) shows the
newest saved results: a setting selector, which attributes hold in which phases, the effect
per attribute, and the districts with their price index.

## Shortlist, location data and the move-planning pages

**Shortlist.** `rank` also writes `data/reports/shortlist_<date>.md`: the 50 best-discount
listings that pass every check and have normal (not low) confidence, one block each with estate, flat, asking price, fair-price
range, discount, the top 3 reasons, flags and the listing URL. Open a few URLs before
believing any of them. A check never changes a price. A listing that fails one gets an
`exclude_reason`, goes to the end of the ranked list (the dashboard hides it unless you tick
the box) and stays in the CSV:

| `exclude_reason` | Meaning |
|---|---|
| `subsidised` | the estate is on the subsidised-estate list (`src/hkestate/subsidised_estates.csv`); outside this project's scope |
| `check_subsidised` | name ends 苑 or 邨 and prices run far below its district: probably subsidised |
| `check_area` | area under 150 or over 5,000 sq ft, or far above the building's recorded areas: probably a typo |
| `check_area_basis` | area 12-45% above the flat's recorded saleable area: probably gross area |
| `check_combined` | the text says two adjacent flats are sold as one |
| `check_price` | asking per sq ft under 0.6x or over 2.5x the building's usual level |
| `check_model` | the model's fair price is over 1.25x what 5+ recent deals show |

`below_all_recent` is only a note: asking per sq ft is under every recent deal in the
building. The same flat listed twice (any site) is shown once, at the lowest price, with the
other in `also_listed`.

**Dashboard pages** (sidebar, or `?page=...`). Both are research aids, not investment or
mortgage advice.

- **Plan a move** (`?page=plan`). Say whether you want to buy, rent, sell or let; what matters
  (investment, being near some MTR stations, a primary school net, space for the money);
  your budget, smallest size, rooms, longest trip, and how many years ahead. You get cards for
  the best estates and then listings, each with a score and a plain reason per goal. For
  buyers there is a line comparing buying with renting a similar flat over your years. For
  owners, type your building, size and floor to see sell now against let and sell later.
  Commute and school choices appear only once station and school data are loaded.
- **Market today** (`?page=market`). Pick a district and flat size: median price per sq ft,
  change over 3 and 12 months (official index and our own sales), number of sales, listings
  for sale and rent, and how far asking prices sit above recent deals. A district table and a
  36-month trend chart. Rows with few sales are greyed.
- **Ranked listings** and **Growth study** are as before.

**Order after geocoding.** Coordinates feed MTR distance, school nets and the commute estimate,
so rerun in this order (each step in its own process, one after another):

1. `python -m hkestate.geo` until every building has been tried (resumable), then
   `python -m hkestate.geo --stations-only`: loads MTR stations, marks geocodes in the wrong
   district as `mismatch` (their coordinates are then ignored), sets MTR distances and fills
   missing districts
2. `python -m hkestate.sources.schools` (add `--assign-only` to re-assign nets without
   fetching)
3. `hkestate train`, then `hkestate rank --from-snapshot`, then
   `hkestate report --settings standard --from-snapshot`

A stopped backfill carries on where it left off when run again. Logs go to `data/logs/`,
reports to `data/reports/`.

`train`, `rank` and `report` read the database once (about 1 minute) and save a snapshot in
`data/model/`; add `--from-snapshot` to reuse it. Retrain as data grows (the territory
backfill, geocoding); rerun `rank` after each day's listings and `report` after `train`
(the findings report is preliminary until the territory backfill finishes). The scheduled
tasks run `rank` daily and `train` weekly (docs/operations.md). The dashboard reads only
`data/model/`, so it can stay open while other jobs run.

## Data folder

Everything scraped or generated (raw-page cache, database, reports) lives in `data/`, which is
never committed. To keep it elsewhere, set `HKESTATE_DATA_DIR` to another folder.

## Docs

- `docs/architecture.md` – modules, data flow, database tables
- `docs/decisions.md` – what was decided and why
- `docs/sources.md` – each data source: what it allows, what it holds, keep or drop
- `docs/known-issues.md` – limits of the data and traps
- `docs/operations.md` – scheduled jobs and locks
- `docs/plans/` – build plan
