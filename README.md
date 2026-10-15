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
```

## Data folder

Everything scraped or generated (raw-page cache, database, reports) lives in `data/`, which is
never committed. To keep it elsewhere, set `HKESTATE_DATA_DIR` to another folder.

## Docs

- `docs/decisions.md` – what was decided and why
- `docs/sources.md` – each data source: what it allows, what it holds, keep or drop
- `docs/plans/` – build plan
