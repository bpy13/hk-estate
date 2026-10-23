# Open items

As of 2026-10-23. Tick off, or move to `decisions.md` when settled. A bug that will be fixed goes
here; a limit or trap we live with goes in `known-issues.md`. Each item in one place only.

## Blockers

## Waiting on the user
- [x] *(Settled 2026-10-23, decisions.md: a box on the page, empty = each district's rise
  since 1997.)* **Which price growth should buy-vs-rent assume? (2026-10-23)** After the review fixes the
  accounting is fair: both paths are cash at the same date, and the renter invests the
  monthly gap. Even so, buying wins for every listing in the middle case. The cause is the
  growth assumption.
  - The middle case uses each district's long-run median from the growth study, 2.9-5.8% a
    year since 1995.
  - The typical listing breaks even at about 0.7% a year.
  - In the low case, buying wins for only 3.9% of listings.

  Options:
  - (a) Keep long-run history (today's default).
  - (b) Use growth since 2003 only, or since the 2021 peak.
  - (c) Let the user type the growth rate on the page, with history as the hint.

  Proposal: (c), defaulting to the district's since-2003 median.

- [x] *(Done: the watchdog has logged every 15 minutes, and the daily job started on
  schedule at 08:00 on 2026-10-23.)* **Register the new tasks (2026-10-20).**
- **Spacious uncapped run length is unmeasured (2026-10-20).** Without the 6,000 cap the
  Spacious territory pass reads every building page in `spacious_building` at 1 s each run;
  it runs in parallel with Centanet, but if the building list is large (tens of thousands)
  the daily job's length is set by Spacious. Check the first uncapped run's log.
*(Settled 2026-10-16 and moved to decisions.md: pre-2018 history (every free source, plus
estimated pairs, Ricacorp pairs as Task 3.6, Centadata % change); MTR station centres; listing
cache pruning.)*

- [x] *(Done 2026-10-18: the owner ran the delete, 14,562 rows, 0 left; `hkestate crosscheck
  --estates lohas-park` rerun from cached pages added 164 Centadata-only rows, match rate
  82.4% as before. `QUARANTINE_SQL` stays as a guard and now matches nothing.)*
  **Delete the wrong-building Centadata rows from the first LOHAS Park cross-check.** The
  19:28-19:31 run on 2026-10-16 (before the matching fix) stored about 14,500 Centadata-only
  rows in `txn_raw` against the wrong LOHAS Park buildings (e.g. Montara deals on LP6). The
  fixed run (19:44 on) adds correct versions of the unmatched ones, but rows that now match a
  28Hse deal, and 海璇灣 rows, keep the wrong building. Since Phase 5 they are flagged in
  `txn_all` and left out of `txn`, the model and the growth study, so nothing reads them; they
  are only filtered, not deleted. Fix: delete `txn_raw` rows with `source='centadata'`, LOHAS
  Park estates and `fetched_at` before 2026-10-16 19:44, then rerun `hkestate crosscheck
  --estates lohas-park` (stored pages, no new requests). The delete was blocked in this session
  and needs your OK.
- **Ricacorp answered 403 (2026-10-16 18:42)** after 24 estate pages at 1.5 s per request; the
  fetcher stopped and nothing was retried. The gap is now 3 s and the Sunday task will try once
  more. If it is still 403, Ricacorp is dropped like Midland and Squarefoot and Task 3.6 closes
  with the pairs already stored (Centadata `gain_pct` stays the fallback). Your call whether to
  contact them or accept the loss.
- **Redirects are followed without a robots.txt check** (pre-release review). *(On hold:
  the owner said wait, 2026-10-22, decisions.md.)* `fetch.py`
  follows redirects, so a redirect to another host or a disallowed path would be fetched
  unchecked and without that host's delay. None seen so far. Options: turn redirects off (a
  redirected page then counts as a failure), or check the final address against robots.txt and
  drop the page if disallowed. Proposal: the second.

## Pending work
- **"Check a price" page (2026-10-23, plan check-a-price).** Built and checked on real data.
  Left: the 1.25 correction for sales the model learned from was measured on recent sales
  only (untested for 1990s-2010s); the page has no facing or sea-view inputs (`fair_value`
  supports them); odd floor labels from recorded sales are passed as they are.
- **House blocks are judged one by one (2026-10-23, plan house-flag-estates, 2134192).** A
  block counts as a house when its name has 洋房/別墅/House, it has 1-3 floors, or (not the
  entry block) its own Centadata `isHouse` is true. Side effects for future full master
  passes: every block of a house-flagged estate is now fetched (爾巒: 74 blocks, was 1
  page); a 3-storey walk-up of ordinary flats, or a tower named "... House", would be
  skipped. Estates already loaded are not affected.
- **Live steps of the 2026-10-23 run, after geocoding ends (~17:00 on 10-23).**
  - `python -m hkestate.geo --stations-only`: district fill and MTR distances.
  - `python -m hkestate.sources.schools`.
  - `hkestate train`, then `rank --from-snapshot`, then `report --settings standard
    --from-snapshot`.
  - The manual check of 20 shortlist URLs. (The phone-width check is dropped: no phone app,
    owner 2026-10-23.)

  The plan file's Tasks 5, 6, 7, 11 and 17 track them.
- **SRPE multi-flat rows (pre-release review 2026-10-23, judgment call).** The parking cell
  is copied to every flat in a multi-flat row. A one-flat row whose block cell wraps onto two
  lines now raises and is skipped. Compare the skipped count at the next SRPE run with the
  last one.
- **The step after geocoding is untimed on live data (pre-release review 2026-10-23).**
  `geo.main`'s tail (`mark_mismatches`, `assign_mtr`, `set_building_ages`,
  `fill_districts`) runs in one database session with per-row updates over ~40k buildings.
  The running process (PID 16964) uses the older tail: `assign_mtr` and
  `set_building_ages` only. Time it from its log when it ends.
- **Asking-vs-deal gap uses the assumed 5% for every listing** (`gap_pairs: 0`): no listing
  has yet been matched to its later sale. Revisit once listings collected since 2026-10-16
  start selling.
- **9.5% of the model's test sales have no district** (their building has none in the
  building table), so they get the weakest fallback; fill districts from `master_building`.
- **Data check leftovers (2026-10-21, after Task 4b).** Small, not blocking the analysis:
  ~5k Centadata floor labels still unread *(2026-10-23: `parse_floor` now reads repeated
  floors `3樓 3`, two-storey flats `2-3`/`G-1`/`20F & R` as the lower floor, and `高層地下`;
  unread stored sales fell from 10,793 to 4,522, mostly houses `13號`/`H`/`3座`, mezzanine
  `閣` and the unclear `G1`/`G2`; takes effect at the next `train`)*; 309 clean same-day sales of one unit at different prices (likely
  confirmor sales, no rule yet); the first-hand rule still decides 263k unlabelled rows and
  could be tuned against Centadata's own label (agrees on 217,503 of 222,951 labelled
  first-hand sales). `test_connect_gives_up_after_the_wait_limit` failed once under heavy CPU
  load and passed on rerun (timing-sensitive test).
- [x] *(Removed 2026-10-23, Task 10, 2fbbb24, with the `excl_quarantine` flag.)*
  **`QUARANTINE_SQL` matches nothing now.** The 14,562 wrong LOHAS Park rows were deleted
  on 2026-10-18, so the guard in `db.py` is a no-op (data check 2026-10-21). Harmless; remove
  it when convenient.
- **Two SRPE rows carry dates from the developer's register that look wrong:** one sale
  dated 2055-09-26 (the register's own typo; the parser now uses the PASP date in such cases,
  but the stored row keeps 2055 until that register is re-read) and one dated 2026-10-26 (in
  the future). Since the pre-release review fixes (2026-10-22) a deal dated after today is
  flagged `excl_bad_date` in `txn_all` and gets `in_future` in `features.deal_features`, so
  both are out of the analysis from the next snapshot on (they were first-hand, so never in
  the clean data; the harm was the 2055 date emptying the rental-yield anchor's 12-month
  window, now capped at the snapshot date). Re-read those registers when SRPE runs next.
- **SRPE glued-price parse (pre-release review 2026-10-21).** Some SRPE register rows glue
  several flats' prices into one number, e.g. row `3085:3 3:2 5:A A:2016-12-03` stored at
  HK$728,100,014,452,000; ~50 SRPE rows are over HK$500m (datacheck 2026-10-21, "price over
  HK$500m"). Harmless today because those rows have no building (so they are `no_building`
  and never clean), but they would enter the analysis once SRPE buildings are matched. Fix
  the parser (one row per flat, one price each) before the next SRPE building pass.
  *(Parser fixed 2026-10-23, e4fe623: a row listing several flats line by line becomes one
  deal per flat. Still to do, needs the owner's OK because it deletes rows: re-parse the
  cached register PDFs (no web requests) and delete the old glued rows (owner's OK
  2026-10-23). **The first `--reparse-cached` run (13:20-~14:00, stopped by a low-memory
  clean-up after 202 of 377 files) deleted 1,285 stored SRPE rows, many of them real sales:**
  the multi-flat parser read two-language block cells (`第2座\nTower 2`) as two flats and
  rejected nearly every row. Fixed (several flats = several prices); a 20-file sample now
  reads 3,964 sales and rejects 2. Rerunning `--reparse-cached` restores the deleted rows
  from the saved files; needs the owner's go; their `source_id`
  (`3085:3 3:2 5:A A:...`) differs from the new per-flat ids, so they would otherwise stay
  beside the corrected ones.)*
- *(Mostly settled 2026-10-23, Task 8, fd6b00f: on the model's clean data the count is
  1,558; a sample of 50 found 47 real 1950s-60s tenement prices and 3 suspected part shares.
  New `low_psf` rule removes 485 clean sales in all. Left: part shares in buildings with few
  deals and no repeat sale of the flat, e.g. 嘉湖山莊 8座 30/H 2002. A building-year check at
  0.6× with 3+ deals would catch them, if wanted.)*
  **2,134 clean rows under HK$1,000 per sq ft (pre-release review 2026-10-21).** The data
  check's "price per sq ft under 1,000" rule hits 8,181 stored rows (centadata 6,100,
  ricacorp 1,866, hse28 215) and 2,134 of them are still clean: sales a tenth of today's
  level or less (1990s village or Tier-3 prices, part-share transfers the price check misses
  in buildings under 5 deals, or area errors). Decide a rule (e.g. index-adjusted floor per
  sq ft) and check a sample; until then they sit in the pairs as extreme starts or ends.
- [x] **`rank` holds the database too long and blocks the scrapers.** *(Fixed 2026-10-21,
  commits 02b6e7c, ed1fad4: the duplicate test's OR join split into two joins; rank's
  database read is now about 1 minute.)* Its extract
  (`features.extract`, built on the `txn_latest` views) took 6 min on 10-17, 25 min on 10-20,
  and on 2026-10-21 was still running after 2 h 10 min once cd-history added ~970k sales. During
  that time the daily run died on the lock at 14:30 and backfill-territory sat waiting. I killed
  it at 16:12 and restarted the daily run. The same will happen at 14:00 on 10-22 unless the
  extract gets faster (e.g. read base tables instead of `txn_latest`) or stops holding the lock
  for the whole read.
- [x] Verify current (Feb 2026) stamp duty rules on IRD pages before modelling round-trip costs.
  *(Done in Phase 5, `src/hkestate/costs.py`: the 2026-27 Budget raised residential over
  HK$100m from 4.25% to 6.5% for agreements from 26 Feb 2026 (IRD AVD FAQ, government press
  release 25 Feb 2026); the HK$100 band up to HK$4m dates from 26 Feb 2025. One detail not
  confirmed: IRD pages give two gazettal dates for the 2026 amendment ordinance (6 March and
  29 May 2026). It doesn't change any figure here (the rate is backdated to 26 Feb 2026 and
  no pair in the study is above HK$100m in 2026), so the IRD FAQ table is used.)*
- [x] SRPE: how to drive the JavaScript app. *(Done in Phase 3: Playwright for the disclaimer,
  then the API at 30 s per request.)*
- [x] Centanet listings: the sitemap's `lastmod` is always today, so price changes on existing
  listings need a rotating re-read of detail pages. *(Done in Phase 2: new listings plus the
  oldest-read seventh of known ones each day, so every price is re-read within 7 days.)*
- **28Hse live listing run waits for the backfill.** The 28Hse listing parser is built and
  tested on fixtures only; while the territory backfill (PID 38336) runs, the daily job skips
  28Hse (lock `data/locks/hse28.lock`) so the two don't share 28Hse's rate limit. The first
  daily run after the backfill ends does the first live 28Hse listing pass (~100 search pages
  plus ~1,000 detail pages at 4 s, about 75 minutes). The estate search URLs
  (`/buy/apartment/a1/dg9/c1140` style) were taken from 28Hse's own links but not yet fetched;
  check that run's log for 404s.
- **28Hse territory listings (Task 3.5) are not built.** The daily job reads 28Hse for the pilot
  estates only; add the territory after the 28Hse backfill (same lock, same 4 s pace, estate
  search pages from the `hse28_estate` list).
- **LOHAS Park listings: 157 of 1,385 unmatched** because 凱柏峰 I–III and the three Seasons
  developments are not in the building table yet. They will match on the first daily run after
  the load; rerun `hkestate listings --sources spacious` to rematch sooner.
- **Spacious finds no City One or Goldwin Heights listings.** Their neighbourhood pages (Sha
  Tin, Mid-Levels West) stop after 3 pages; deeper pages or the building pages
  (`/n/77-.../b/93017--city-one-shatin/...`) are needed. Secondary source, low priority.
- **75 Taikoo Shing Centanet listings name only the phase** (e.g. "太古城 海天花園", no block),
  so they can't be placed in a building. Left unmatched; they could be valued at phase level
  later.
- [x] Fetcher: robots.txt retries. *(Done in Phase 1: 2 retries with 2 s / 4 s back-off;
  401/403/404 are not retried.)*
- [x] **Deals that include a parking space (連車位).** *(Done in Phase 4 and, for the stored
  views, Phase 5: flagged in `txn_all`, left out of `txn`.)*
- [x] **First-hand (developer) sales flagged.** *(Done in Phase 4; decisions.md "Phase 4
  build".)*
- [x] **Agreement dates for 28Hse deals.** *(Decided in Phase 4: shift back 14 days.)*
- [x] **Part-share transfers in the stored views.** *(Done in Phase 5: `excl_price_check` in
  `txn_all`, left out of `txn`.)*
- [x] **SRPE houses out of scope.** *(Done in Phase 5: houses are left out through the shared
  exclusion list used by the model and the growth study.)*
- **28Hse rate limit and the backfill length.** At ~900 requests an hour (what 28Hse
  tolerates), the territory-wide 28Hse pass (~195k pages) takes ~9 days, not 1–3.
  **Decided (Claude's call, 2026-10-16, given the user's priority on history):** run
  continuously in the background with the largest estates first, keeping the 2018–2021
  per-flat pass.
- [x] **MTR station coordinates (Task 3.3).** *(Done 2026-10-16: all 97 stations from
  Wikipedia title coordinates, about 50 m accuracy. Rerun `python -m hkestate.geo` after the
  building geocoding finishes to cover the rest.)*
- **SRPE gaps (Task 3.4).** (a) Price-list PDFs (flat facing and size) not parsed. (b)
  Developments whose sales ended over 18 months ago are in a second index
  (`disclaimer_index_for_all_residential_t18m`) that is not read, so older first-hand sales
  are missing. (c) SRPE deals dedupe only against rows under the same building id (cd-
  buildings), not pilot 28Hse ids. (d) **Only 20.8% of the ~88,200 SRPE rows have a
  building** (datacheck 2026-10-21; 0 of ~8,600 on 2026-10-16): rerun
  `python -m hkestate.sources.srpe` after the master pass and geocoding; until then the
  first-hand rule covers those buildings.
- [x] **Master list vs 28Hse ids.** *(Done 2026-10-16: `hkestate link-ids`, `source_map`.)*
- **28Hse territory estate names vs Centadata:** check the `hse28_estate.match_status` counts
  once the backfill finishes; name matching is untested at territory scale.
- **Subsidised estates with no current listing** carry no HOS/TPS flag on Centadata and pass
  the coverage filter; use `data/manual/master_overrides.csv`.
- **Ricacorp: place deals after the master pass.** Run `hkestate ricacorp --place-only` once
  the Centadata master pass has finished, then check how many `ricacorp_deal` rows are still
  unplaced (`building_id IS NULL`). Ricacorp's ~16,400 per-building pages are not read; decide
  whether the extra ~7 hours per pass is worth it (moot if the 403 persists).
- **Territory listing quality to check after the first full pass:** the match rate by method
  (`listing.match_method` for `group_name = 'territory'`), Spacious buildings with more listings
  than one page shows (only ~10-14 listings per building are seen), and the Centanet run length
  (about 3 h a day at 1 s since 2026-10-19; `cd-history` and the master pass pause while it
  reads Centanet, so a long run delays them; lower `--centanet-budget` if that matters).
- **Jobs started before 2026-10-19 still use the old locks.** A `cd-history` started earlier
  pauses on the `listings` lock (the whole daily job), and an older listing run reads Centanet
  at 2 s without holding the `centanet` lock. Both stay at or under ~1 request/s on
  hk.centanet.com; restart them (when convenient) to get the new behaviour.
- **海璇灣 I and II (Centadata phases 13A/13B) are not in the 28Hse LOHAS Park set.** 8 blocks,
  ~900 deals. The territory backfill picks them up if 28Hse has them as a separate estate.
- [x] **Retrain, re-rank and rerun the findings report when the backfill finishes.** *(Done
  2026-10-21, plan 2026-10-21 Tasks 5-8: retrained on all 1.5M clean sales, re-ranked, and
  `findings_20261021.md` rests on 663,400 real pairs across the territory (the pilot estates
  are 4% of clean sales), not the 2,989 pairs, 93% pilot, of `findings_20261016.md`.)*
- **Long-running jobs may put back the old `txn`/`txn_all` views.** Jobs started before Phase 5
  (territory backfill PID 38336, master pass, SRPE, daily listings) still run the old code; if
  one calls `init_db` again it recreates the old views until any new command runs. The growth
  study and model are not affected (they build the query from the current code).
- [x] **First-hand result in recorded pairs is LOHAS-Park-heavy.** *(Rechecked 2026-10-21:
  the territory report has 176,516 recorded pairs starting with a developer sale, from every
  district and 1993 on, so the result no longer rests on the 2018-2020 LOHAS Park launches;
  see "First-hand vs second-hand" in the latest findings file.)* On 2026-10-16, 1,652 of
  2,989 recorded pairs started with a developer sale, most of them LOHAS Park launches.
- **Jupyter is not in the conda env.** `notebooks/explore.ipynb` was checked by running its
  cells as code; to open it interactively, `conda install -n hk-estate ipykernel notebook`.

- **`cd-history` skips pilot flats whose latest deal matched 28Hse.** Its flat codes come from
  stored Centadata rows, and the pilot cross-check stores only the Centadata deals 28Hse did
  not have (99% of 2018+ latest deals matched), so those flats have no stored code and get no
  pre-2018 history. Taikoo Shing: 6,001 codes for about 12,000 flats. Fix when wanted: have
  the cross-check also keep the flat code of matched deals (a small table), then rerun
  `cd-history --estates pilot`. Territory estates are complete (the master pass stores every
  latest deal). *(Phase 1, Task 3.)*
- **Page size 200-400 was not tried.** 100 rows a page works and 500 gives HTTP 500; to keep
  to the probe allowance only those two were tested. A larger page would cut calls by up to
  a third on big buildings. *(Task 2.)*

- *(2026-10-23: the list now holds 312 Housing Authority and Housing Society estates, matched
  by name and district; 82 ranked listings marked, 40 of them 浩景台. Still open: wire in
  `master_overrides`.)* **Subsidised-estate list is a seed only (2026-10-23, Tasks 2-3).**
  `src/hkestate/subsidised_estates.csv` holds 銀蔚苑 only. The `check_subsidised` price rule
  catches 5 more listings. Add the Housing Authority's HOS / PSPS / TPS court list. Also wire
  in the master list's exclusion and `master_overrides`; today `building_table` drops those
  estates as "house or subsidised estate", so their listings never reach the ranking anyway.
- **Territory rematch, first run (2026-10-23, Task 2c).** The first daily run after 878d84d
  re-checks every `contains`/`single` territory listing in the usual 1,000-row sessions. It
  wasn't timed on the live database. Check that day's daily log for its length. Rows that lose
  their building keep the old `dup_group` until that run's dedupe step recomputes it (untested
  path).
- **峰景大廈 vs 峯景 (ranking diagnosis 2.4).** Only partly fixed by Task 2b. The diagnosis's
  district check (rule c) is not built.

- **Mortgage rules (Task 16, 2026-10-23, 35733ba).** HKMA rules are entered from
  2024-02-28 (current: 70% LTV for all, DSR 50%, stress test suspended, since 2024-10-16).
  Mortgage-insurance limits confirmed from the HKMC pages on 2026-10-23 and entered as
  `mortgage.mip_max_loan` (not yet used by the advisor). Still to do: decide on the Dec 2024 scheme for uncompleted first-hand flats
  (reported 80% LTV, 60% DSR, not entered), and add older rules if past purchases need them.
- **First-hand window counts (Task 8b, 384a649).** The 72-month window removes 5,012 clean
  deals; the diagnosis counted 4,153 with a different filter, not reconciled. Price ratio to
  resales is 1.05 overall (1.24 for buildings completed 2010-13), below the diagnosis's
  1.12. Check the shortlist after the Task 11 retrain.

- **Rent estimates (Task 15, 20135bc).** Of 44,830 active sale listings, the rent estimate
  comes from the building's own rent listings for 9.4%, the estate 25.3%, the district 32.8%
  and the RVD average 32.5%; median gross yield 3.3%. Much of the RVD share is the 2,315
  buildings with no district; recheck after `fill_districts` runs (Task 7). District medians
  mix small and large flats (no size band yet). Estimates are at asking-rent level;
  `rent.achieved_from_asking()` converts, and the caller passes the `costs.py` anchor.
- **Commute is an estimate (Tasks 14/14b).** No MTR timetable between stations is
  published; trips are walk + 1.6 min per stop + distance at 1.7 km/min + 5 min per change,
  fitted to five known journeys (within about 2 min). Buses, minibuses and ferries are not
  modelled.

## Later
- **Dashboard map** (decisions.md: "a map in a later phase"): not built in Phase 4.
