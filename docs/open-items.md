# Open items

As of 2026-10-16. Tick off, or move to `decisions.md` when settled. A bug that will be fixed goes
here; a limit or trap we live with goes in `known-issues.md`. Each item in one place only.

## Blockers

## Waiting on the user
*(Settled 2026-10-17 and moved to decisions.md: pre-2018 history (every free source, plus
estimated pairs, Ricacorp pairs as Task 3.6, Centadata % change); MTR station centres; listing
cache pruning.)*

## Pending work
- Verify current (Feb 2026) stamp duty rules on IRD pages before modelling round-trip costs.
- SRPE (Phase 3, Task 3.4): the site is now a JavaScript app with a disclaimer and session-token
  API and a 30 s crawl-delay. Decide how to drive it (Playwright for the disclaimer, then the
  API at 30 s per request) before Task 3.4. A full backfill is about 30 hours.
- [x] Centanet listings: the sitemap's `lastmod` is always today, so price changes on existing
  listings need a rotating re-read of detail pages. *(Done in Phase 2: new listings plus the
  oldest-read seventh of known ones each day, so every price is re-read within 7 days.)*
- **28Hse live listing run waits for the LOHAS Park backfill.** The 28Hse listing parser is
  built and tested on fixtures only; while the backfill (PID 5004) runs, the daily job skips
  28Hse (lock `data/locks/hse28.lock`) so the two don't share 28Hse's rate limit. The first
  daily run after the backfill ends does the first live 28Hse listing pass (~100 search pages
  plus ~1,000 detail pages at 4 s, about 75 minutes). The estate search URLs
  (`/buy/apartment/a1/dg9/c1140` style) were taken from 28Hse's own links but not yet fetched;
  check that run's log for 404s.
- **LOHAS Park listings: 157 of 1,385 unmatched** because 凱柏峰 I–III and the three Seasons
  developments are not in the building table yet (the backfill is still loading LOHAS Park).
  They will match on the first daily run after the load; rerun `hkestate listings
  --sources spacious` to rematch sooner.
- **Spacious finds no City One or Goldwin Heights listings.** Their neighbourhood pages (Sha
  Tin, Mid-Levels West) stop after 3 pages; deeper pages or the building pages
  (`/n/77-.../b/93017--city-one-shatin/...`) are needed. Secondary source, low priority;
  proposal: switch these two to building pages in Phase 3 (Task 3.5).
- **75 Taikoo Shing Centanet listings name only the phase** (e.g. "太古城 海天花園", no block),
  so they can't be placed in a building. Left unmatched; they could be valued at phase level
  later.
- [x] Fetcher: a robots.txt fetch has no retry. One run saw Centanet's robots.txt fail once,
  which (correctly, but needlessly) blocked the host for that run. Add 1–2 retries for
  robots.txt on 5xx or network errors in Phase 1. *(Done in Phase 1: 2 retries with 2 s / 4 s
  back-off; 401/403/404 are not retried.)*

- **Deals that include a parking space (連車位).** 28Hse marks some deals as including a car
  space; their price per sq ft is inflated. They are stored (flag in `raw_json`) but not yet
  excluded or adjusted. Proposal: exclude them from fair-price comparables and repeat-sales
  pairs in Phase 4/5.
- **First-hand (developer) sales are not flagged yet.** 28Hse's deal lists don't label them,
  and LOHAS Park's newer phases are full of them. Until SRPE (Task 3.4) supplies the list,
  proposed rule: a flat's first recorded deal within 24 months of the building's first deal
  is treated as first-hand. Needed before Phase 4.
- **Agreement dates for 28Hse deals.** 28Hse shows registration dates; the agreement date is
  2–6 weeks earlier and known only where Centadata has the deal. Fine for yearly figures;
  decide in Phase 4 whether to shift 28Hse dates by the typical gap.

- **28Hse rate limit changes the Phase 3 backfill estimate.** At ~900 requests an hour
  (what 28Hse tolerates), the territory-wide 28Hse pass (~195k pages) takes ~9 days, not
  ~3 (the agreed limit was 1–3 days). Options for Phase 3: (a) accept ~9 days of background
  running in chunks; (b) skip the 2018–2021 per-flat pass outside the pilot (~150k of the
  pages; history then starts March 2021 except via Centadata's latest deal per flat).
  **Decided (Claude's call, 2026-10-17, given the user's priority on history):** (a), run
  continuously in the background with the largest estates first. Keep the 2018–2021 per-flat
  pass.

- **MTR station coordinates (blocks MTR distance, Task 3.3).** ALS resolves only 16 of 98
  stations by name; Overpass and CSDI are disallowed by robots.txt. Default: you drop a
  `data/manual/mtr_stations.csv` (code,lat,lon; codes as in table `mtr_station`), then
  `python -m hkestate.geo` fills `master_building.mtr_station/mtr_m`.
- **SRPE gaps (Task 3.4).** (a) Price-list PDFs (flat facing and size) not parsed. (b)
  Developments whose sales ended over 18 months ago are in a second index
  (`disclaimer_index_for_all_residential_t18m`) that is not read, so older first-hand sales
  are missing. (c) SRPE deals dedupe only against rows under the same building id (cd-
  buildings), not pilot 28Hse ids. (d) Rerun `python -m hkestate.sources.srpe` after the
  master pass and geocoding so unmatched developments get buildings.
- **Master list vs 28Hse ids (for Task 3.2).** Territory buildings carry `cd-` ids, the 28Hse
  backfill mints `h28-` ids; link them (name/address, via `source_map`) so the duplicate rule
  can pair their deals.
- **Subsidised estates with no current listing** carry no HOS/TPS flag on Centadata and pass
  the coverage filter; use `data/manual/master_overrides.csv`.

## Later
