# Open items

As of 2026-10-16. Tick off, or move to `decisions.md` when settled. A bug that will be fixed goes
here; a limit or trap we live with goes in `known-issues.md`. Each item in one place only.

## Blockers

## Waiting on the user
- **Register the new tasks (2026-10-20).** `scripts\register_task.ps1` (all) or
  `-Only 'daily listings'` (now 23 h limit) and `scripts\watchdog.ps1 -Register` (watchdog
  every 15 min). Then check `data\logs\watchdog_<date>.log` after a day. Running jobs started
  before this change keep the old code (no adaptive pacing, 30-min DB wait) until restarted.
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
- **Redirects are followed without a robots.txt check** (pre-release review). `fetch.py`
  follows redirects, so a redirect to another host or a disallowed path would be fetched
  unchecked and without that host's delay. None seen so far. Options: turn redirects off (a
  redirected page then counts as a failure), or check the final address against robots.txt and
  drop the page if disallowed. Proposal: the second.
- **Estimated pairs lean towards "no effect"** (pre-release review). The model that values a
  flat today was trained partly on that flat's own pre-2018 sale, so the estimated growth
  partly echoes the start price and attribute effects shrink towards zero. The report should
  say so in one sentence; a cleaner version would value each flat with a model that leaves
  that flat out (slower). Your call whether that is worth it.

## Pending work
- **Your call: keep the retrained price model (2026-10-21)?** Retrained on all 1.5M clean
  sales since 1995. On recent sales it is a touch worse than the 2026-10-18 model on the same
  deals (6.25% vs 6.10% typical error) but it knows 99% of listings' buildings (old: 91%, the
  rest fell back to a rough estate/district guess) and is far better on older sales (2000s
  5.5% vs 7.8%), which the growth study uses. Kept the new one; the old one is in
  `data\model\backup_20261018\` (copy back to restore). Training on 2010+ only would gain
  0.2 pp on recent sales (`hkestate train --since 2010`); kept all years as decided.
- **Asking-vs-deal gap uses the assumed 5% for every listing** (`gap_pairs: 0`): no listing
  has yet been matched to its later sale. Revisit once listings collected since 2026-10-16
  start selling.
- **9.5% of the model's test sales have no district** (their building has none in the
  building table), so they get the weakest fallback; fill districts from `master_building`.
- **Data check leftovers (2026-10-21, after Task 4b).** Small, not blocking the analysis:
  ~5k Centadata floor labels still unread (`G-1`, `閣` mezzanine, duplex ranges `2-3`,
  `高層地下`, house numbers); 309 clean same-day sales of one unit at different prices (likely
  confirmor sales, no rule yet); the first-hand rule still decides 263k unlabelled rows and
  could be tuned against Centadata's own label (agrees on 217,503 of 222,951 labelled
  first-hand sales). `test_connect_gives_up_after_the_wait_limit` failed once under heavy CPU
  load and passed on rerun (timing-sensitive test).
- **`QUARANTINE_SQL` matches nothing now.** The 14,562 wrong LOHAS Park rows were deleted
  on 2026-10-18, so the guard in `db.py` is a no-op (data check 2026-10-21). Harmless; remove
  it when convenient.
- **Two SRPE rows carry dates from the developer's register that look wrong:** one sale
  dated 2055-09-26 (the register's own typo; the parser now uses the PASP date in such cases,
  but the stored row keeps 2055 until that register is re-read) and one dated 2026-10-26 (in
  the future). Both are excluded from analysis as odd dates only if Task 4b's rule catches
  them; re-read those registers when SRPE runs next.
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
  buildings), not pilot 28Hse ids. (d) **0 of ~8,600 SRPE deals have a building yet**: rerun
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
- **Retrain, re-rank and rerun the findings report when the backfill finishes.** `hkestate
  train`, then `hkestate rank` and `hkestate report` (rank runs daily at 14:00, train weekly on
  Sundays at 12:00). Today's `data/reports/findings_20261016.md` is preliminary: recorded
  pairs are 93% pilot, and verdicts near the line moved between runs while data was loading.
  Compare pilot vs territory error in `data/reports/model_eval.md` (today 5.2% vs 5.0%).
- **Long-running jobs may put back the old `txn`/`txn_all` views.** Jobs started before Phase 5
  (territory backfill PID 38336, master pass, SRPE, daily listings) still run the old code; if
  one calls `init_db` again it recreates the old views until any new command runs. The growth
  study and model are not affected (they build the query from the current code).
- **First-hand result in recorded pairs is LOHAS-Park-heavy.** 1,652 of 2,989 recorded pairs
  start with a developer sale, most of them LOHAS Park launches 2018-2020; their +1.7 pp a
  year may reflect those launches' pricing. Recheck with territory pairs.
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

## Later
- **Dashboard map** (decisions.md: "a map in a later phase"): not built in Phase 4.
