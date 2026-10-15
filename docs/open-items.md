# Open items

As of 2026-10-16. Tick off, or move to `decisions.md` when settled. A bug that will be fixed goes
here; a limit or trap we live with goes in `known-issues.md`. Each item in one place only.

## Blockers

## Waiting on the user
1. **History from 2000 can't be met with free sources.** Free full deal histories start in
   2018 (28Hse). Before that we only have each flat's latest deal (Centadata, back to 1995).
   This matters most for the growth study: repeat-sales pairs held 3+ years would start
   2018–2023 and cover one cycle (2021 peak, 2021–24 fall, recovery). Options: (a) accept
   2018 onward, and use Centadata's older latest deals for the fair-price model only;
   (b) approve a paid source (IRIS or similar); (c) probe other agency sites (e.g. Ricacorp,
   Hong Kong Property) for older history. (d) Claude's suggestion, combinable with (a): pair
   each flat's pre-2018 latest deal (Centadata) with the fair-price model's estimate of its
   value today, giving long-horizon "estimated pairs" back to 1995. Model error averages out
   across an attribute group, and these pairs are reported separately from real pairs.
   **Default: (a) + (d)**, noted as a limit in the report. Phase 5 only; earlier phases don't
   depend on it.
2. **Raw listing pages in the cache contain agent names and phones** (the pages show them;
   parsers will skip them and the database never stores them). Default: delete cached listing
   pages after 7 days with a prune step in the daily job.
3. **CSDI files by hand?** CSDI's API is off-limits (robots.txt), so MTR distance will be to
   the station centre (geocoded with ALS), not the nearest exit. Default: station centres.
   The alternative is for you to download the MTR-exits file once in a browser and drop it in
   `data/`.

## Pending work
- Verify current (Feb 2026) stamp duty rules on IRD pages before modelling round-trip costs.
- SRPE (Phase 3, Task 3.4): the site is now a JavaScript app with a disclaimer and session-token
  API and a 30 s crawl-delay. Decide how to drive it (Playwright for the disclaimer, then the
  API at 30 s per request) before Task 3.4. A full backfill is about 30 hours.
- Centanet listings: the sitemap's `lastmod` is always today, so price changes on existing
  listings need a rotating re-read of detail pages (e.g. 1/7 of live listings a day). Design in
  Task 2.1/2.3.
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

## Later
