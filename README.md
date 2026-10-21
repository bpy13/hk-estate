# HK Estate

A personal research tool for Hong Kong private residential flats. It collects transaction
history, current listings and flat attributes from free public sources, stores them in a local
DuckDB database, and analyses them to:

1. rank current listings by their discount to a modelled fair price, with risk flags, and
2. measure which flat attributes are linked to better long-run price growth and total return.

Single user, non-commercial. Scraping is slow and polite (robots.txt obeyed, at most one request
per second per site, every page cached). No personal data is stored.

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

**Growth study windows.** `report --start YYYY-MM-DD --end YYYY-MM-DD` keeps a pair only when
both its sales fall inside the window (default: all data). `report --settings standard` runs
these windows from one snapshot (or `--settings name,name`; not together with
`--start/--end`, the presets carry their own dates); a pair needs a 3-year hold, so a
window too short for enough pairs says so instead of giving a verdict:

| Setting | Pairs counted |
|---|---|
| `all` | every pair, 1995 on |
| `since-2000` | bought from 2000 |
| `crash-1997-2003` | bought and sold Oct 1997 - Jul 2003 |
| `recovery-2003-2008` | bought and sold Aug 2003 - Sep 2008 |
| `boom-2008-2021` | bought and sold Oct 2008 - Sep 2021 |
| `fall-2021-2024` | bought and sold Oct 2021 - Oct 2024 |
| `recent-2024-now` | bought and sold since Nov 2024 |
| `bought-after-crash` | bought from Jul 2003 (does the crash drive `all`?) |
| `fall-2021-2024-1y`, `recent-2024-now-1y` | the last two phases again with a 1-year minimum hold, because the 3-year rule leaves almost no pairs; labelled sensitivities, never mixed into the main verdicts |

The standard run takes 5-15 minutes (less when the scrapers are idle) and up to about 9 GB of
memory (close other big programs).
Results go to `data/reports/growth/<date>/<setting>/` (CSV files and `settings.json`);
the findings text is `data/reports/findings_<date>.md`.

The dashboard's **Growth study** page (sidebar, or `?page=growth&setting=<name>`) shows the
newest saved results: a setting selector, which attributes hold in which phases, the effect
per attribute, and the districts with their price index.

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
- `docs/plans/` – build plan
