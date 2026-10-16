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
```

A stopped backfill carries on where it left off when run again. Logs go to `data/logs/`,
reports to `data/reports/`.

## Data folder

Everything scraped or generated (raw-page cache, database, reports) lives in `data/`, which is
never committed. To keep it elsewhere, set `HKESTATE_DATA_DIR` to another folder.

## Docs

- `docs/architecture.md` – modules, data flow, database tables
- `docs/decisions.md` – what was decided and why
- `docs/sources.md` – each data source: what it allows, what it holds, keep or drop
- `docs/known-issues.md` – limits of the data and traps
- `docs/plans/` – build plan
