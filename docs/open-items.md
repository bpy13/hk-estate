# Open items

As of 2026-10-16. Tick off, or move to `decisions.md` when settled. A bug that will be fixed goes
here; a limit or trap we live with goes in `known-issues.md`. Each item in one place only.

## Blockers

## Waiting on the user
*(Settled 2026-10-17 and moved to decisions.md: pre-2018 history (every free source, plus
estimated pairs, Ricacorp pairs as Task 3.6, Centadata % change); MTR station centres; listing
cache pruning.)*

## Pending work
- [x] Verify current (Feb 2026) stamp duty rules on IRD pages before modelling round-trip costs.
  *(Done in Phase 5, `src/hkestate/costs.py`: the 2026-27 Budget raised residential over
  HK$100m from 4.25% to 6.5% for agreements from 26 Feb 2026 (IRD AVD FAQ, government press
  release 25 Feb 2026); the HK$100 band up to HK$4m dates from 26 Feb 2025. One detail not
  confirmed: IRD pages give two gazettal dates for the 2026 amendment ordinance (6 March and
  29 May 2026). It doesn't change any figure here (the rate is backdated to 26 Feb 2026 and
  no pair in the study is above HK$100m in 2026), so the IRD FAQ table is used.)*
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

- [x] **Deals that include a parking space (連車位).** *(Done in Phase 4: left out of
  fair-price training and comparables, `features.py`; Phase 5 pairs should use the same
  `exclude_reason`.)*
- [x] **First-hand (developer) sales are not flagged yet.** *(Done in Phase 4: SRPE's flag plus
  a rule for buildings SRPE doesn't cover, refined so old buildings with 2018-on histories
  aren't mistaken for launches; decisions.md "Phase 4 build".)*
- [x] **Agreement dates for 28Hse deals.** *(Decided in Phase 4: shift 28Hse registry dates back
  by the measured Centadata lag, 14 days; decisions.md.)*

- **28Hse rate limit changes the Phase 3 backfill estimate.** At ~900 requests an hour
  (what 28Hse tolerates), the territory-wide 28Hse pass (~195k pages) takes ~9 days, not
  ~3 (the agreed limit was 1–3 days). Options for Phase 3: (a) accept ~9 days of background
  running in chunks; (b) skip the 2018–2021 per-flat pass outside the pilot (~150k of the
  pages; history then starts March 2021 except via Centadata's latest deal per flat).
  **Decided (Claude's call, 2026-10-17, given the user's priority on history):** (a), run
  continuously in the background with the largest estates first. Keep the 2018–2021 per-flat
  pass.

- [x] **MTR station coordinates (Task 3.3).** *(Done 2026-10-16: ALS names only 14 of 97 stations
  and returns street addresses for the rest; CSDI, Overpass, api.data.gov.hk and static.data.gov.hk
  are disallowed by robots.txt or answer 403; MTR open data has no coordinates; Wikipedia's
  station articles are ordinary pages that robots.txt allows (its API is not), so
  `python -m hkestate.geo --stations-only` reads their title coordinates for the other 83. All
  97 stations now have coordinates; distance to the nearest station is set for the 1,313
  geocoded buildings (median 541 m). Accuracy is about 50 m, enough for station-level distance.
  Rerun `python -m hkestate.geo` after the building geocoding finishes to cover the rest.)*
- **SRPE gaps (Task 3.4).** (a) Price-list PDFs (flat facing and size) not parsed. (b)
  Developments whose sales ended over 18 months ago are in a second index
  (`disclaimer_index_for_all_residential_t18m`) that is not read, so older first-hand sales
  are missing. (c) SRPE deals dedupe only against rows under the same building id (cd-
  buildings), not pilot 28Hse ids. (d) Rerun `python -m hkestate.sources.srpe` after the
  master pass and geocoding so unmatched developments get buildings.
- [x] **Master list vs 28Hse ids (for Task 3.2).** *(Done 2026-10-16: `src/hkestate/link.py`,
  `hkestate link-ids`. `source_map` links each 28Hse building to its master building and
  `txn_all` uses it, so both sources' deals share a unit. Pilot: 17,917 Centadata deals sit on
  known pilot units, 399 on flats that also have 28Hse deals.)*
- **28Hse estate list for the territory is not fetched yet (Task 3.2).** `hkestate
  backfill-territory --discover` reads 28Hse's sitemap index (named in its robots.txt) and
  follows only child sitemaps with "estate" in the address; that layout is unverified because no
  request to www.28hse.com may be made while the LOHAS Park backfill runs. If it finds nothing,
  the log lists the child sitemaps; fall back to `data/manual/hse28_estates.csv` (`id,name`) or
  the estates named on 28Hse listing cards. Name matching to Centadata (exact, or a whole-estate
  name against phases) is untested on real territory data until both lists exist: check the
  `hse28_estate.match_status` counts after the first `--discover`.
- **Subsidised estates with no current listing** carry no HOS/TPS flag on Centadata and pass
  the coverage filter; use `data/manual/master_overrides.csv`.

- **Ricacorp: place deals after the master pass.** Run `hkestate ricacorp --place-only` once
  the Centadata master pass has finished (it also runs at the end of every `hkestate
  ricacorp`), then check how many `ricacorp_deal` rows are still unplaced
  (`building_id IS NULL`): estate-name or block-name mismatches go in a new alias list.
  Ricacorp also has ~16,400 per-building pages (`/transaction/building/...`, in the
  `transaction-detail` sitemap) that probably list more recent deals per building than the
  estate page's ~10; not read, since the plan names estate pages only. Decide after the first
  pass whether the extra ~7 hours per pass is worth it.

- **Ricacorp answered 403 (2026-10-16 18:42)** after 24 estate pages at 1.5 s per request; the
  fetcher stopped and nothing was retried. The gap is now 3 s and the Sunday task will try once
  more. If it is still 403, Ricacorp is dropped like Midland and Squarefoot and Task 3.6 closes
  with the pairs already stored (Centadata `gain_pct` stays the fallback). Your call whether to
  contact them or accept the loss.
- **28Hse territory listings (Task 3.5) are not built.** The daily job reads 28Hse for the pilot
  estates only; add the territory after the 28Hse backfill (same lock, same 4 s pace, estate
  search pages from the `hse28_estate` list).
- **Territory listing quality to check after the first full pass:** the match rate by method
  (`listing.match_method` for `group_name = 'territory'`), Spacious buildings with more listings
  than one page shows (its paging repeats page 1, so only ~10-14 listings per building are
  seen), and the Centanet run length (about 4.5 h a day at 2 s; lower `--centanet-budget`
  if it clashes with the master pass).
- **SRPE includes houses (out of scope).** E.g. development 7045 (37 Shouson Hill Rd, a
  single-house tender sale at HK$2.566bn) is stored as a first-hand deal. Houses and villas
  are excluded by decision. Before Phase 4, apply the coverage filter to SRPE developments
  (9-column house registers, or the master list's house flag) so they never enter comparables
  or growth pairs.
- **Wrong-building Centadata rows from the first LOHAS Park cross-check.** The 19:28-19:31 run (before the matching fix) stored about 14,500 Centadata-only rows in 	xn_raw against the wrong LOHAS Park buildings (e.g. Montara deals on LP6). The fixed run (19:44 on) adds correct versions of the unmatched ones, which supersede them in 	xn_latest, but rows that now match a 28Hse deal, and 海璇灣 rows, keep the wrong building. Fix: delete 	xn_raw rows with source='centadata', LOHAS Park estates and etched_at before 2026-10-16 19:44, then rerun hkestate crosscheck --estates lohas-park (stored pages, no new requests). The delete was blocked in this session and needs your OK.
- **海璇灣 I and II (Centadata phases 13A/13B) are not in the 28Hse LOHAS Park set.** 8 blocks, ~900 deals. Territory backfill picks them up if 28Hse has them as a separate estate.

- **Retrain and re-rank as Phase 3 data lands (Phase 4 follow-up).** `hkestate train` then
  `hkestate rank` after the territory 28Hse backfill, the master pass and the geocoding run
  finish, and compare pilot vs territory error in `data/reports/model_eval.md` (today 5.2% vs
  5.0%). *(Scheduled in Phase 5: `\hk-estate\daily rank` 14:00 and `\hk-estate\weekly train`
  Sundays 12:00; both started by hand on 2026-10-16, exit code 0.)*
- **Rerun the findings report when the territory backfill finishes (Phase 5).** `hkestate
  train`, then `hkestate report`. Today's `data/reports/findings_20261016.md` is preliminary:
  recorded pairs are 93% pilot, and verdicts near the line moved between runs an hour apart
  while data was loading.
- **Long-running jobs may put back the old `txn`/`txn_all` views.** The Phase 5 view (price
  check, parking, quarantine flags) is in the live database now, but jobs started before it
  (territory backfill PID 38336, master pass, SRPE, daily listings) still run the old code; if
  one calls `init_db` again it recreates the old views until any new command runs. The growth
  study and model are not affected (they run the query from the current code). Nothing to do
  except restart those jobs normally when they finish.
- **First-hand result in recorded pairs is LOHAS-Park-heavy.** 1,652 of 2,989 recorded pairs
  start with a developer sale, most of them LOHAS Park launches 2018-2020; their +1.7 pp a
  year may reflect those launches' pricing. Recheck with territory pairs.
- **Jupyter is not in the conda env.** `notebooks/explore.ipynb` was checked by running its
  cells as code; to open it interactively, `conda install -n hk-estate ipykernel notebook`
  (not done: it adds compiled packages, and Windows Application Control already blocks one
  here).
- **SRPE first-hand deals reach no building yet** (0 of ~8,600 have a `building_id`), so their
  first-hand flag doesn't feed the model; the first-hand rule covers those buildings for now.
  Same root cause as SRPE gap (d) above: rerun the SRPE matching after the master pass.
- **Part-share transfers in the stored views.** The model's price check drops deals under half
  / over double their building's median price per sq ft (decisions.md), but `txn_all`/`txn`
  still keep them. Proposal: add the same rule to `txn_all` as `excl_price_check` before
  Phase 5 so the growth pairs leave them out too. Changes a shared view, so not done
  unasked.
- **The quarantined LOHAS Park rows are only filtered, not deleted** (see "Wrong-building
  Centadata rows" above): the model and rank read around them; the stored `txn_all`/`txn`
  views still include them until you OK the delete.

## Later
- **Dashboard map** (decisions.md: "a map in a later phase"): not built in Phase 4.
