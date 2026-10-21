# HK Estate – notes for Claude

A personal research tool for Hong Kong residential property. It collects transaction
history, current listings and flat attributes (size, rooms, floor, facing, location), stores
them in a structured local database, and analyses them to find (a) listings priced below
market and (b) flat attributes linked to better long-run price growth. Single user (the
owner); not a public service. Scope and details are still in discovery: see docs.

Read these before substantial changes:

- `docs/decisions.md` – what was decided and why (don't re-litigate without a reason)
- `docs/open-items.md` – pending work and questions waiting on the user
- `docs/plans/` – the latest plan
- `docs/architecture.md` – modules, data flow, where things live (once it exists)
- `docs/known-issues.md` – limits and traps found so far (once it exists)
- `README.md` – user-facing setup and commands

## Environment
- Install: `conda env create -f environment.yml`, then
  `conda run -n hk-estate python -m pip install -e ".[dev]"` (conda env `hk-estate`, Python 3.12)
- Run: `conda run -n hk-estate hkestate <command>`: `backfill --estates pilot` (resumable),
  `rvd`, `crosscheck --estates pilot`, `cd-history` (every sale per flat from Centadata; resumable;
  `--estates`, `--limit`, `--max-hours`), `listings`, `daily`, `update`, `prune-cache`,
  `status`, `datacheck` (checks on the snapshot; `--read-db`, `--no-fetch`),
  `train` (`--since`, `--compare-since`, `--previous`; the 2026-10-18 model is in
  `data/model/backup_20261018/`), `rank` (fair price; `--from-snapshot` reuses
  `data/model/snapshot`), `report` (growth study, `data/reports/findings_<date>.md`;
  `--start/--end`; `--settings standard` runs all windows, 5-15 min, up to ~9 GB, saved in
  `data/reports/growth/<date>/<setting>/`). Notebook: `notebooks/explore.ipynb`.
  Dashboard: `conda run -n hk-estate streamlit run app/dashboard.py` (page "Growth study"). Scheduled jobs and
  locks: `docs/operations.md`. Spot-check:
  `conda run -n hk-estate python scripts/spot_check.py`. Logs in `data/logs/`, reports in
  `data/reports/`.
- Tests: `conda run -n hk-estate python -m pytest -q`
- Lint: `conda run -n hk-estate ruff check .`
- Data folder: `data/` (git-ignored), override with env var `HKESTATE_DATA_DIR`.

## Project rules
- Scrape politely: obey robots.txt, at least 1 request/second per site, identify a cache so
  pages are never fetched twice. Never call paths robots.txt disallows (e.g. centanet
  `/centanetapi/api/`).
- Never store personal data: no owner/buyer names, no agent names or phone numbers (keep
  only the listing URL). Hong Kong's privacy law (PDPO) applies.
- Raw scraped data and the database stay out of git (data/ is ignored).

## Conventions
<tbd>
