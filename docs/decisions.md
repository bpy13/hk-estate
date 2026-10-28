# Decisions

What was decided, why, and when. Change these only with a reason, and note the change here
(mark reversed decisions as reversed rather than deleting them).

## Scope
- **Purpose: the owner's own investment research** (2026-10-16). Not a product, no clients,
  single user. Keeps scraping in personal-use territory and polish modest.
- **Coverage: whole territory, private residential flats only** (2026-10-16). Excludes HOS /
  subsidised flats (resale restrictions distort prices), houses/villas, commercial. Build is
  proven on a few pilot estates first, then scaled to the whole territory within the plan.
- ~~**History from 2000** (2026-10-16). Covers several cycles (2003 trough, 2008, 2021 peak,
  2021–24 fall, 2025–26 recovery).~~ *Reversed 2026-10-21, see next line.*
- **History: all data, adjustable window** (2026-10-21, user): use every year available
  (Centadata goes back to 1995) in everything, unless the 1997–2003 crash skews a result to
  a meaningless degree. The study takes an adjustable start and end date and is run in
  several meaningful settings (all data, each market phase, bought after the crash).
- **Recent phases get a short-hold sensitivity** (2026-10-21, Claude's call): the
  2021-2024 fall and 2024-now windows hold almost no 3-year pairs (1 and 0), so each also
  runs as a clearly labelled sensitivity with a 1-year minimum hold. The 3-year hold stays
  the main rule; the short-hold results are never mixed into the main verdicts.
  Built as settings `fall-2021-2024-1y` and `recent-2024-now-1y` (`sensitivity: true` in
  settings.json). Their after-cost figures ignore Special Stamp Duty (holds under 36 months
  paid 10-20% until 28 Feb 2024), so the report says to read their gross growth and
  verdicts only. (Claude's call, 2026-10-21.)
- **Findings report reading order** (2026-10-21, Claude's call): recorded pairs are the
  headline; the attribute x setting table shows each effect in bold when robust, and a
  plain line per attribute says in which phases it holds. "Holds in rising and falling
  markets" needs robust and the same sign in every phase with enough pairs. Estimated
  pairs moved to a cross-check section. The "preliminary / pilot-heavy" wording was
  dropped: recorded pairs are now 3% pilot and span 1993-2026.
- **Ricacorp pairs are checked against every dated sale** (2026-10-21, Claude's call): a
  derived Ricacorp pair ending within 90 days of any stored consecutive-sale pair (not only
  3-year ones) is the same sale twice and is dropped; 107 -> 99 pairs.
- **Don't wait for the 28Hse territory backfill before analysing** (2026-10-21, user).
- **Dashboard gets a growth-study page** (2026-10-21, user).
- **Two goals:**
  1. Under-market listings: score every current listing by its discount to a modelled fair
     price (after allowing for the usual asking-vs-deal gap) and **rank them; no fixed cut-off**
     (2026-10-16, user: "all is relative"). Each listing shows risk flags (haunted, illegal
     works, tenanted, short lease, poor view…).
  2. Attribute growth study: which flat attributes are linked to better growth, measured as
     **both price growth and total return (growth + rental yield)**, over holding periods of
     3+ years, with risk (drawdown) shown and out-of-sample checks (2026-10-16).
- **Transaction costs** (stamp duty, agent fees, legal) are modelled from the IRD's own pages,
  since secondary sources disagree on 2026 rules. (Claude's call.)

## Platform and hosting
- **Runs on the owner's Windows laptop** (2026-10-16). Listings refreshed daily by Windows
  Task Scheduler from the listings phase on.
- Python in a conda env (python-env skill). (Claude's call.)

## Data and secrets
- **Collection method: slow, polite scraping** (2026-10-16): obey robots.txt, ≤1 request/s per
  site, raw-page cache so nothing is fetched twice, resumable runs. Never call disallowed
  paths (e.g. centanet `/centanetapi/api/`).
- **Data budget: HK$0** (2026-10-16). No paid APIs (e.g. Anysite) and no IRIS searches unless
  the owner approves a specific spend.
- **Storage: DuckDB file on the laptop + raw-page cache in `data/`, nothing in the cloud**
  (2026-10-16). Shape: estate → building → unit → transactions, plus dated listing snapshots.
- No personal data of owners/buyers stored (PDPO). (Claude's call.)

## Behaviour and rules
- **AI: keyword rules first** for reading listing text (sea view, renovated, haunted…). A
  Claude model only later, with a cost quote before any run (2026-10-16).

## UI
- **Deliverables: analysis notebooks, a local Streamlit dashboard listing ranked deals, and a
  written findings report** (2026-10-16).
- **Language: English reports, Chinese estate/building names kept alongside** (2026-10-16).

## Claude's calls from the builder check (2026-10-16)
Minor gaps decided by Claude; change any with a reason.

**Phases.** Phase order lives in the plan file. Phase 0 (source spike) gates the rest: a source
that blocks scrapers, or whose robots.txt forbids what we need, is dropped and recorded here,
with no need to ask. Each phase gets acceptance criteria in the plan.

**Pilot estates.** Taikoo Shing (Island, large, 1970s–80s), Mei Foo Sun Chuen (Kowloon, oldest),
City One Sha Tin (NT, large), LOHAS Park (new, NT East), plus one single-block building chosen in
Phase 0. Spot-check: 50 random rows against the source website, all must match.

**Coverage filter.** HOS, TPS (Tenants Purchase Scheme) and other subsidised flats are excluded,
including ex-HOS with premium paid (different buyer pool). Villas and village houses are
excluded; duplexes and penthouses are kept but flagged. First-hand (developer) sales are
stored and flagged: they are left out of fair-price comparables and reported as a separate
group in the growth study (developer incentives distort prices).

**Source roles.**
- Transactions: Centadata (master estate/building list). 28Hse transactions are used to
  cross-check and fill gaps. *(Changed 2026-10-16 by the Phase 0 spike: 28Hse is the
  transaction master; see "Phase 0 source spike" below.)* History starts at whatever the free sources hold, aiming for 2000.
  Coverage is reported per estate rather than assumed.
- Listings: 28Hse, Centanet, Squarefoot, Spacious, house730 (Midland only if it stops
  blocking). *(Changed 2026-10-16: Squarefoot and house730 dropped as blocked.)*
- First-hand sales: SRPE.
- Indices: RVD monthly price and rent indices by class, popular developments, completions and
  stock.
- Location: the government Address Lookup Service (free API) for coordinates; CSDI for MTR
  stations and building footprints, with 3D view scoring in a later phase.
- Rents: RVD rent indices for history, rental listings for current yields.

**Risk flags.**
- From listing text: haunted, tenanted, renovated, sea view.
- Squarefoot's haunted-flat list, if its robots.txt allows.
- Illegal works (Buildings Department orders) and land-lease expiry are deferred. Most leases
  were extended, so lease expiry adds little.

**Personal data.** No agent names or phone numbers are stored, only the listing URL.

**Scraping.**
- At most 1 request per second per site, with random jitter and back-off on 429 and 5xx
  errors. On 403 or a CAPTCHA, that source stops for the run and the failure is logged.
- Plain HTTP first; a Playwright headless browser only for pages that need JavaScript, and
  never on paths robots.txt disallows.
- The cache is keyed by URL and fetch date, with a lifetime per page type: listings 1 day,
  transaction pages 7 days, static pages 30 days. Cached pages are compressed.

**Schema.**
- Tables run estate → phase → building → unit, plus transactions, listing snapshots, rents,
  index series, attributes and risk flags. Every row records its source and fetch time.
  Raw records are append-only, and clean views are built on top.
- A unit is identified by building + floor label + flat. Floor labels are kept as text and
  also mapped to a number, so skipped floors (4, 13, 14, 24) and ground/podium labels are
  handled.
- Saleable area is the standard. Gross-only rows are converted using the building's own
  saleable-to-gross ratio where known, and otherwise flagged.
- Deal dates use the agreement (instrument) date, with the registration date kept as well.
- Excluded from analysis but kept in storage: deals under HK$500k, price per sq ft more than
  5 median absolute deviations from the same building's yearly median, bulk or same-day
  multi-unit deals, and non-market transfers.
- Listings get one snapshot per day (HK time). A listing is treated as delisted after 3
  missed days. A relisting at a new price becomes a new snapshot of the same listing.
- Growth is shown in nominal HK$, with CPI-adjusted figures alongside.

**Matching.**
- Centadata's estate/building list is the master. Other sources are mapped to it by Chinese
  name plus address, with fuzzy matching and a manual mapping CSV for leftovers.
- Chinese name is the primary key text; English is optional.
- Listings without a flat letter are matched to the building and a floor band (low, mid or
  high), and their fair price is estimated for that band.
- Cross-site duplicate listings are merged on building + floor band + saleable area (±2%).

**Fair price (goal 1).**
- Model: LightGBM (a tree model) on attributes + location + time, with SHAP (a method that
  shows which inputs drove each estimate) to explain each value. A check against recent
  deals in the same building runs alongside.
- Uncertainty uses conformal intervals (a statistical way to put a range around each
  estimate).
- The asking-vs-deal gap is estimated per district from listings that disappear and match a
  recorded deal on the same unit within 90 days. Until enough pairs exist, a default of 5%
  is used and marked as an assumption.
- Sort key: percentage discount to fair price. Confidence and flags are shown but don't
  change the rank. Listings with few comparable deals are marked low-confidence, not hidden.

**Growth study (goal 2).**
- Uses repeat-sales pairs of the same unit, all pairs held 3 years or more. Annualised
  growth is regressed on attributes, controlling for district, building age, start period
  and starting price.
- Drawdown: peak-to-trough of a monthly repeat-sales index per attribute group.
- Out-of-sample checks:
  - fit on pairs ending by 2018 and test on later pairs
  - a by-district holdout
  - an attribute counts only if its effect holds in both and is statistically significant.
- Total return is net of round-trip costs and gross of holding costs, with no leverage.
- Costs:
  - buyer is an HK permanent resident, not a first-time buyer
  - agent fee 1% on each side, legal fees 0.3%
  - stamp duty follows the rules in force at each deal date (a dated table of schedules).

**Operations.**
- Listings refresh daily at 08:00 via Task Scheduler, with "run as soon as possible after a
  missed start".
- Transactions refresh weekly.
- Failures go to a log file and a banner on the dashboard; no email.

**Tooling.**
- Python 3.12 conda env `hk-estate`, pytest, ruff.
- Tests run offline against trimmed or synthetic fixture pages committed to the repo. Real
  pages stay in the local cache.
- Schema is documented in `docs/architecture.md`.
- Notebooks are committed with outputs stripped (nbstripout).
- No hooks for a future AI listing reader until one is approved.
- Dashboard: ranked list with filters, a listing detail page (comparables, price history,
  SHAP reasons), and a map in a later phase.

## Answers to builder check (2026-10-16)
- **Data folder stays in the repo (`data/`, git-ignored).** OneDrive isn't syncing on this
  machine (user). The path is still a single config setting, so it can move if sync is
  turned back on.
- **Findings report and ranked deals are written to the data folder, never committed** (repo
  is public).
- **First territory-wide backfill of roughly 1–3 days of resumable run time is acceptable.**
  The daily listing refresh fetches only new or changed listings.

## Phase 0 source spike (2026-10-16)
Details and measurements in `docs/sources.md`. Supersedes parts of "Source roles" above.
- **Transaction master: 28Hse, not Centadata.** Centadata's allowed pages show only the latest
  deal per flat; full flat histories and paged deal lists come from
  `/centanetapi/api/`, which is disallowed, so we never use it. 28Hse gives every deal
  per flat, but free data starts in **2018** (its estate deal lists reach back to March 2021;
  2018–2021 needs per-flat pages).
- **Centadata kept as the building master list** (estate → block → flat, completion date,
  floors) and for the one latest deal per flat, which reaches back to 1995. Its pages are
  served from `hk.centanet.com/findproperty/centadata-details/...`.
- **Dropped, blocked:** Squarefoot (and its haunted-flat list), house730 and Midland all answer
  HTTP 403 to our user agent, even for robots.txt. We do not try to get round it.
- **Dropped, robots.txt:** CSDI's data API (`api.csdi.gov.hk` disallows everything). MTR
  station locations come instead from MTR open data (station list) plus ALS geocoding of
  station names. Distance is to the station, not the nearest exit. Building footprints and
  3D view scoring are deferred.
- **Kept:** Centanet listings (all live listings are in its sitemap), 28Hse listings and rents,
  Spacious (secondary: no floor/flat), RVD, ALS, MTR open data.
- **SRPE kept but deferred to Phase 3:** it is now a JavaScript app with a disclaimer and
  session-token API and a 30 s crawl-delay. The way to drive it is decided at Phase 3.
- **Pilot single-block building: 高雲臺 Goldwin Heights** (Mid-Levels West, 1994, 328 flats,
  one tower; 281 flats with a recorded deal on Centadata).
- **Fetcher:** robots.txt parsed with `protego` (wildcards), robots `Crawl-delay` honoured,
  robots.txt answering 401/403/5xx means stay away.

## Phase 1 build (2026-10-16, Claude's calls)
- **28Hse dates Land Registry deals by registration date**, not agreement date (Centadata
  cross-check: all 70 matched Goldwin Heights deals). `deal_date` for 28Hse registry deals is
  therefore the registration date (copied into `registration_date` in the views); the
  agreement date is known only where Centadata gives it. Agreements are typically 2–6 weeks
  earlier.
- **Outlier rule made robust:** still 5 MADs from the building's yearly median price per
  sq ft, but the MAD is scaled by 1.4826 (the usual factor that makes it comparable to a
  standard deviation) and never taken below 5% of the median. With the raw MAD, a building
  of near-identical flats flagged 17 of ~290 Goldwin Heights deals, including ordinary
  high-floor premiums; now 3, all clearly abnormal.
- **Bulk deals:** 3 or more flats in one building on one day (no buyer names are stored, so
  same-buyer can't be checked).
- **Same sale reported twice** (an agency report later registered, or Centadata and 28Hse
  both holding it): same flat, price within 1%, dates within 90 days; the 28Hse Land
  Registry copy wins.

## Phase 2 build: listings (2026-10-16, Claude's calls)
- **Where each site's pilot listings come from:** Centanet from its detail sitemap (slug
  starts with the estate name); 28Hse from its estate-filtered search pages
  (`/buy|rent/apartment/<area>/<district>/c<estate id>`); Spacious from the Chinese
  neighbourhood search pages (sale only), filtered by building name.
- **Detail pages:** Centanet: new listings plus a rotating seventh of known ones a day.
  28Hse: only new listings or a changed price (search cards carry the rest). Spacious: none
  (cards have everything it offers; no floor or flat on Spacious anyway).
- **Request pacing:** Centanet 1 s, 28Hse 4 s, Spacious 1 s. hk.centanet.com (robots
  crawl-delay 1) is shared with the Centadata jobs (`cd-history` 4 s, the master pass 2 s);
  they pause while the listing job holds the `centanet` lock (only while it reads Centanet),
  so the host sees at most ~1 request/s. (Until 2026-10-19 Centanet listings ran at 2 s
  alongside the master pass at 2 s; owner approved 1 s once the master pass finished.)
  *Superseded 2026-10-20: see "Pacing and recovery" below.*
- **Rent listings** are collected too (Centanet and 28Hse), for current yields in Phase 5.
- **Mei Foo address ranges:** Centanet names a block "百老匯街19-21號"; 28Hse lists one
  building per street number. A range listing is matched to the lowest number's building
  (`match_method = 'range'`): it is one physical block.
- **Delisting counts run days, not calendar days:** a listing is delisted after 3 runs of its
  source that covered its estate and didn't see it; days the laptop was off, or a run that
  was blocked or skipped a source, don't count.
- **Relisting:** the same id seen again after delisting gets a snapshot with status
  `relisted`; a new id on the same site for the same building, floor band, flat and area
  (±2%) within 180 days of a delisting gets `relist_of` = the earlier listing.
- **Flags** come from titles, blurbs and feature tags with building/estate names removed
  (so 太古城's 海景花園 blocks don't read as "sea view"); `rooms = 0` also sets `studio`.
  Only the flag and the matched term are stored.
- **28Hse is skipped by the daily job while a backfill or the weekly update holds the
  `hse28` lock**, rather than the whole job waiting: Centanet and Spacious still run, and the
  database is shared through short sessions.
- **Weekly transaction refresh** (`hkestate update`) reads each estate's deal list from page 1
  and stops at the first page that adds nothing.

## After Phase 2 (2026-10-16)
- **Pre-2018 history is a priority, not optional** (user). Long-term growth depends on it.
  Use every free pre-2018 data point:
  - Centadata latest deals back to 1995, in the price model and as one end of growth pairs.
  - RVD indices: popular-development indices since 1992, class indices since 1979.
  - Estimated pairs (pre-2018 latest deal → model value today), reported apart from real
    pairs.
  - A probe of further free sources for older per-flat history.
  Paid sources stay out (HK$0 budget) unless the user approves a spend.
- **MTR distance to station centres** (user). Not exits.
- **Pre-2018 probe results** (Claude's call, following the user's "pre-2018 is a priority";
  details in `docs/sources.md`):
  - **Ricacorp: used** (Task 3.6). Its robots-allowed estate pages give real repeat-sale
    pairs reaching back to about 1997 for recent resales. A weekly re-crawl accumulates
    them.
  - **Centadata's "% change vs previous deal": captured** as a lower-confidence pair, since
    the previous date is unknown.
  - **Not used:**
    - Wayback Machine (no deal rows)
    - HKP, Midland, Squarefoot, house730 (403)
    - Land Registry (counts only)

    Paid sources stay out.

## Phase 4 build: fair price (2026-10-16, Claude's calls)
- **Agreement dates for 28Hse deals: shifted back by the measured lag.** 28Hse Land Registry
  deals show the registration date; for the model (month index, train/test split) they move
  back by the median Centadata registration-minus-agreement gap, measured on every `hkestate
  train` (14 days on 72,732 Centadata deals since 2018; middle half 11-20 days). Centadata and
  SRPE give the agreement date themselves; 28Hse agency reports keep the date shown. Stored
  dates are unchanged; only the model's `agreement_date` is shifted.
- **Left out of fair-price training and comparables** (kept in storage; `features.py`
  `exclude_reason`):
  - deals including a parking space (28Hse 連車位, SRPE parking column)
  - first-hand sales: SRPE's flag, and where SRPE doesn't cover the building, a rule: a flat's
    first recorded deal within 24 months of the building's completion. Without a completion
    date, the building's first deal is the anchor, but only for a launch (25%+ of the
    building's flats first sold in those 24 months, first deal 1998 or later). A first try
    without the 25%-of-all-flats test would have treated old buildings whose 28Hse history
    starts in 2018 as launches. On the live data the rule marks 65,208 deals, a median of
    one year before completion: Centadata's "latest deal per flat" is the developer sale for
    every flat never resold.
  - houses, villas and subsidised estates (master-list coverage filter; SRPE house registers)
  - Ricacorp previous deals derived from days held and % gain
  - the ~14,500 wrong-building LOHAS Park Centadata rows from the first cross-check run,
    filtered read-only by fetch time (`db.QUARANTINE_SQL`, removed 2026-10-23 after the rows
    were deleted on 2026-10-18; 8,954 were then still the newest copy)
    before the duplicate/bulk/outlier tests, so they can't distort them. Deleting them still
    waits for the owner (open-items).
- **Price check** (added after the first ranking): deals under half or over double their
  building's median index-adjusted price per sq ft (5+ deals) are left out. One Mei Foo block
  had several 2026 "sales" at HK$1-4m for 908 sq ft flats (likely part-share transfers;
  28Hse doesn't mark them); so many that the yearly 5-MAD rule widened and kept them, and the
  model learnt that block as 30%+ cheap. The stored `txn_all` rules are unchanged.
- **Target divided by the RVD index; listings valued at the last well-populated month.** Trees
  can't extrapolate in time, so the model learns prices on a common market level (the RVD
  price index of the flat's size class) and the out-of-time test freezes the index at the
  cut-off. Listings are valued at the newest month with 200+ deals (the current month has a
  handful, which would otherwise set the date effect for every listing).
- **Facing and sea view are flat-level**: a listing stating them lends them to every deal on
  the same building + flat letter. Listings without a floor are valued at their band's middle
  (1/6, 1/2, 5/6 of the building) or, with no band, at mid-height (marked `imputed`).
- **SHAP values come from LightGBM's own TreeSHAP** (`pred_contrib=True`), the same algorithm
  as the `shap` package: importing `shap` fails on this laptop because Windows Application
  Control blocks a scikit-learn DLL it loads.

## Phase 5 build: growth study (2026-10-16, Claude's calls)
- **Shared clean view.** `txn_all` now flags `excl_price_check` (the Phase 4 price check, same
  thresholds, index-adjusted in SQL), `excl_parking` and `excl_quarantine` (the wrong LOHAS
  Park rows, also ignored by the duplicate/bulk/outlier tests of other rows); `txn` leaves
  them out. The first-hand rule and the house filter need building-level logic and the
  master tables, so they stay in `features.deal_features`; its `exclude_reason` is the clean
  definition both the model and the growth study use. Nothing is deleted.
- **Pairs are consecutive clean sales of a unit, held 3+ years**; a shorter hold is dropped,
  not merged with an earlier sale. A developer sale may start a pair (tagged
  `first_hand_start`) if it passes every check except the bulk rule (a launch sells many
  flats a day). Two sales whose areas differ by over 10% are not treated as the same flat.
- **Four pair types, never pooled with estimated pairs:** real; Ricacorp (derived previous
  sale; pooled with real as "recorded" since both are actual prices); Centadata/28Hse
  "% change" pairs (no previous date, so used only to check the previous price, 96% within
  2%, and as a weak sign check); estimated (pre-2018 last sale vs the Phase 4 model's value).
- **Time split adapted.** The plan's "pairs ending by 2018 vs later" leaves almost no recorded
  pairs before 2019 (28Hse's free data starts 2018) and estimated pairs all end today. When
  either side has under 200 pairs, the split is by purchase year at the median (recorded:
  bought by 2018 vs later; estimated: by 2009 vs later). The district check deals districts
  into two halves of similar size. Robust = p < 0.05 and the same sign in both halves of
  both splits. An attribute that never varies within a district (e.g. "large estate" in the
  pilot estates) is not tested, since district is a control.
- **Regression:** annual log growth on attribute dummies (size bands, floor thirds, MTR
  bands, large estate, special unit, south-facing among known facings, sea view, first-hand
  start) with district, purchase-year and sale-year dummies, building age at purchase (and
  squared) and log purchase price per sq ft; errors clustered by building.
- **Model error for estimated pairs:** in the residuals (clustered by building) plus a tilt
  check: the model's out-of-time error by attribute, divided by the median holding years; an
  estimated effect no bigger than that tilt is not called robust.
- **Drawdown index:** monthly repeat-sales (BMN) index with a light month-to-month smoothness
  penalty; recorded pairs judged from 2018, estimated pairs over 1995-2017.
- **Costs:** legal 0.3% charged once, on the purchase (decisions above list it without "each
  side"); agent 1% each side; stamp duty for an HKPR who owns another flat (Scale 1 from
  23 Feb 2013, 15% from 5 Nov 2016, 7.5% from 25 Oct 2023, Scale 2 from 28 Feb 2024), with a
  first-home buyer shown as a sensitivity. No SSD (holds are 3+ years) or BSD (HKPR).
- **Rental yield:** RVD average rent / average price per m2 by size class and region (1999 on;
  carried back to 1993 by the rent and price indices), averaged over each pair's holding
  period, times the median ratio of current listing yields (asking rent / recent sale price
  per sq ft, by building) to RVD's; 1 if under 20 buildings; limited to 0.5-2. Total return
  = after-cost growth + that yield (no reinvestment, no leverage, gross of holding costs).
- **Charts are hand-written SVG** (`charts.py`): matplotlib is not installed and Windows
  Application Control already blocks one compiled package here.

## Pacing and recovery (2026-10-20)
What happened: Centanet listings at 1 s got five HTTP 500s in a row after ~800 pages and the
circuit breaker stopped Centanet for the day; cd-history (4 s) then got 429s and exited, and
still got 429 on its first requests when restarted 11 hours later. The daily job died with
exit 0xC000013A (a console Ctrl+C/close); the day before, all three long jobs died when
`db.connect` gave up after 30 minutes.
- **Adaptive pacing per host** (owner: "pick a pace, monitor, adjust when the site pushes
  back"). Base gaps: Centanet listings 2 s (2 s had run 8,000 pages without trouble),
  cd-history 2 s (owner, 2026-10-20: two jobs at 2 s is ~1 request a second, as listings + master ran for days), master pass 4 s; others unchanged. A throttle signal (429/503, 3 timeouts in a row, 5xx after
  retries, timeout) multiplies the gap by 1.5 (cap 15 s); 429/503 also start a cool-down
  (15 min, 30, 60, then 2 h each time, because the API throttle lasted hours), followed by a
  single probe. After 100 good answers the gap steps 25% back towards the base, never below.
  Process-local state; nothing is shared between jobs.
- **Long jobs wait instead of dying** on throttling (cd-history, the backfills, Ricacorp,
  the master pass): indefinitely, retrying the same item. An item failing with a plain 5xx
  three times on its own is skipped for the next run (it may be that item, not the site).
  The daily listing run cools down 10/20/30 min and gives up a source for the day after the
  third. Non-throttle errors (404, bad answers) keep their old handling.
- **Concurrent on hk.centanet.com, not exclusive pauses** (owner, replaces the `centanet`
  lock pauses): listings 2 s + cd-history 2 s ≈ 1 request a second (0.92 with the rare
  master pass at 4 s), below the ~1 a second that ran for days. Host priority: the listings'
  pace is the fastest; the slow-down handles any pushback. The `centanet` lock stays as a
  marker around the Centanet part of the listing run.
- **No Centanet or Spacious page cap** (owner). The 30,000 / 6,000 caps were a run-length
  choice in the 2026-10-16 build plan, not a politeness limit; each daily run now reads every
  due page however long it takes (first Centanet pass ~26,000 pages ≈ 14 h at 2 s, then ~6,000
  a day). `--centanet-budget` / `--spacious-budget` remain as optional manual caps. The three
  listing sources run in parallel threads (different hosts) so 28Hse and Spacious don't wait
  behind Centanet; the finishing steps run after all three. The `daily listings` task may run
  23 h. A time box was considered and dropped with the caps.
- **DB lock waits**: the long jobs wait for the database file for ever (logging the first 10
  waits, then every 10 minutes); short commands keep the 30-minute limit. Threads of one
  process take turns in `db.session` (DuckDB allows it, but concurrent writes to a row fail
  with "Conflict on update", checked 2026-10-20).
- **Watchdog** every 15 minutes restarts a long job or today's daily run that died (lock
  files + logs, never the database), with at most 3 restarts per job per day and a
  crash-loop guard. The scheduled daily job's Python runs in its own console so a Ctrl+C
  elsewhere can't reach it.

## Shortlist and decision helper (2026-10-23)
- **Keep the retrained price model of 2026-10-21** (user). The 2026-10-18 model stays as a
  backup in `data\model\backup_20261018\`.
- **Decision helper** (user): help a person decide to buy, rent, sell or let by their goals
  (investment, near a place, primary school, space and budget), with recommended listings.
  Defaults accepted with the plan (user, "ok"):
  - designed as if for the public, running privately in the Streamlit dashboard; no public
    app; monetisation on hold
  - all four decisions, buy in most detail
  - primary school nets only, from Education Bureau open data
  - a free commute estimate (walk + MTR stops), no paid routing
  - HKMA loan-to-value caps and stress test, monthly payment at a user-set rate
  - rents from the rent listings already collected
  - a "market today" view
- **Report wording** (user): "pairs" becomes "flats sold twice", "model-valued flats" and
  "Ricacorp reported gains" in text people read; the report says model-valued flats
  understate differences. Code names stay.
- **Licence: MIT; README notice** that the repo holds code only and each source's terms
  apply (user, 2026-10-22). Fixtures, history and commit email stay as they are (user).
- **Redirect robots check and the scheduled-task registration wait** (user, 2026-10-22).
- **Sandwich Class estates count as subsidised** (user, 2026-10-23), like HOS courts: a flat
  with its premium unpaid would otherwise look like a bargain. 82-85 ranked listings marked,
  most of them Sandwich Class (浩景台 40).
- **No mortgage insurance or cash check on "Plan a move"** (user, 2026-10-23, "skip"). The
  page keeps the normal 70% loan and a 30% deposit for everyone and does not ask for the
  buyer's cash. `mortgage.mip_max_loan` stays in the code, unused.
- **No phone app** (user, 2026-10-23). The phone app idea is given up; pages need no
  phone-width check. Supersedes "phone-sized screens" in the plan of 2026-10-23.
- **Price growth for buy-vs-rent and sell-vs-let** (user, 2026-10-23). The "Plan a move" page
  has a box for the yearly price rise. Left empty, each district's index rise since 1997 is
  used (Hong Kong as a whole 2.4%/yr). Why: the long-run median made buying win for 100% of
  listings; the answer flips between about 0% (buying ahead for 6%) and 2% (96%). 1997 was
  a peak, so the figure spans a full rise and fall; 2003 was the low (6.3%/yr from there)
  and was rejected. Low and high cases keep the district's 10-year spread.

## Listing checks (2026-10-23, Claude's call)
From `docs/ranking-diagnosis.md` (rank.py `listing_checks`). A check never changes a value;
it puts a reason in `exclude_reason` and the row goes to the end of the ranking, never
deleted. One reason per row, first that applies: `subsidised`, `check_area`,
`check_combined`, `check_area_basis`, `check_price`, `check_model`, `check_subsidised`.
- `subsidised`: estate named in `src/hkestate/subsidised_estates.csv` (seed: 銀蔚苑; the
  Housing Authority's HOS/PSPS/TPS list still to be added). The master list's own flag finds
  nothing (diagnosis 2.7). 景翠苑 and 豐盛苑 are not in it: gross area and wrong building.
- `check_subsidised` (stop-gap): name ends 苑 or 邨, and the estate's 24-month clean median
  price per sq ft is under 0.7x its district's, with 3+ deals.
- `check_area`: area under 150 or over 5,000 sq ft; or at least 1.5x the nearest recorded
  saleable area when the building has 3+ recorded areas, unless the text says 複式, 天台 or 相鄰.
- `check_area_basis`: listed area 1.12-1.45x the flat's recorded saleable area (nearest, when
  the flat letter has one; else the nearest of the building's 3+ areas). Within 3% passes.
  Not for 複式/天台. `check_combined`: the text says 相鄰 (two flats in one listing).
- `check_price`: asking per sq ft under 0.6x or over 2.5x the building's index-adjusted
  median (all clean deals). Lower bound 0.6 not the plan's 0.4: nothing real is below it.
  A typo guard, not a filter.
- `check_model`: 5+ comps in 24 months and fair price over 1.25x their median.
- `check_match` (matcher flag for a block the estate doesn't have) is not a ranking reason: the
  matcher returns no building for it, so the listing is "no building match" and is not ranked.
  Keeping its building id would make the gap, facing/age and dashboard code treat it as a
  confirmed match (pre-release review, 2026-10-23).
- `below_all_recent`: information only. Asking per sq ft under the building's lowest clean
  deal of the last 24 months, with 3+ such deals.
- Duplicates: same building, area within 2%, price within 2%, floor band and flat equal or
  blank on either side, on any site. Kept: the clean row, then the lowest asking price.
- On the 2026-10-22 snapshot: 24,392 clean rows, 591 with a reason (check_model 247,
  check_combined 115, check_area 89, check_price 81, check_area_basis 52, check_subsidised 5,
  subsidised 2); 5,233 duplicates merged.

## Cheap-sale rule (2026-10-23, Claude's call)
New `exclude_reason` value `low_psf` in `features.deal_features` (Task 8), applied after the
building price check to the deals it left. A deal is out when its index-adjusted price per
sq ft is (a) under 0.15x its district's median over clean deals (the median of every clean
deal where the district is unknown), or (b) under half the median of the same flat's other
clean sales within 5 years. Why so low a district floor: a 50-row sample of the 1,558 clean
sales under HK$1,000/sq ft on the 2026-10-22 snapshot was 47 real old prices (1950s-60s
tenement flats in Sham Shui Po, Yau Tsim Mong and Kowloon City sold 1995-2006 for
HK$0.5-0.8m, consistent with the flat's later sales and the building's other deals), 3
suspected part shares, 0 area errors. Real tenement prices sit at 0.2-0.35 of the district's
level, so the plan's 0.25 floor would have removed about half of them; 0.15 catches only
prices no whole flat fetched. The flat-level check (b) is the price check's 0.5 applied to one
flat, for the small buildings (under 5 clean deals) the price check never judges; of 25 random
hits, about 20 looked like part shares (the flat sold at 2-4x within a few years with the
index flat or falling). On the snapshot the rule removes 485 clean sales (1990s 125, 2000s
193, 2010s 100, 2020s 67; 45 of them under HK$1,000/sq ft). The shared `txn` view is
unchanged; the rule lives in the model's feature table only.

- **First-hand window for newer buildings (2026-10-23, Claude's call).** In buildings with a
  completion date in 2010 or later, a flat's first-ever sale counts as a developer sale up to
  72 months after completion (was 24). Older buildings, and launches anchored on the
  building's first deal (no completion date), keep 24 months. Why: developers of newer
  buildings (e.g. 御海灣) release flats slowly, and those sales sit well above resales
  (diagnosis section 2.5). On the snapshot the change removes 5,012 clean sales (by completion
  year: 2010-13 491, 2014-17 1,079, 2018-21 1,674, 2022+ 1,768; by sale year mostly 2024-26,
  2,773). Against other clean sales of the same building and year they are priced 1.05x at the
  median (2,793 matched; 1.24x for 2010-13, 1.01x for 2014-17, 1.07x for 2018-21, 1.05x for
  2022+), less than the diagnosis's 1.12x. Takes effect at the Task 11 retrain.

## Decision helper scoring (2026-10-23, Claude's call)
`advisor.recommend` (Task 18). Hard limits first, then four goal scores of 0-100, then a
weighted average by the profile's goal weights.
- **Candidates.** Buy: the ranked sale listings with no `exclude_reason`. Rent: the active
  rent listings `rent.clean_rent_listings` keeps. Sell and let have no listing search.
- **Hard limits, in this order** (a row counts against the first it fails): budget (buy:
  asking at or under the budget, and when a monthly income is given the HKMA test
  `mortgage.affordable` must pass; rent: asking rent at or under the limit); minimum size;
  minimum rooms; longest commute; school net. A limit on a value the data lacks (no
  coordinates, no net, no room count) counts the row out, unless `Profile.allow_unknown`.
- **Candidates, same flat twice** (Task 18b): rank.py merges the same flat listed twice only
  when the prices are within 2%; the helper also merges two listings of one flat (same
  building, floor and flat, area within 2%) at prices further apart, keeping the cheaper and
  naming the other in `also_listed` (809 of 13,091 investor candidates on the 10-22
  snapshot). A blank floor or flat never matches. Space score unchanged.
  (changed 2026-10-23 after the pre-release review) The ranked table's floor is a band or
  "not given", never exact, so matching on that label merged different flats (the same flat
  letter on other floors). Now two listings merge only when they are truly one flat: same
  building, same non-blank flat letter, floors that do not contradict (two exact floors or
  two bands must be equal; "not given" does not object), area within 2% and asking within 5%
  of the cheaper. Anything else is kept as its own listing. On the 10-22 ranked table: 420
  of 24,385 clean listings merged (809 under the old rule).
- **Investment (buy)** = mean of four parts: district growth from the growth study's
  `districts.csv` (setting `all`, flats sold twice): 0 at 0%/yr, 100 at 8%/yr; attribute
  effects from `effects.csv`, robust verdicts only, summed for the attributes the flat has:
  50 + 25 points per percentage point a year; rent yield (rent.py estimate x 12 / asking):
  100 at 6%; listing discount: 50 + 500 x discount (10% below fair price = 100), only for
  normal-confidence rows. **Investment (rent)** = 50 + 500 x (1 - asking / reference rent).
  (changed 2026-10-23 after the pre-release review; it used the raw estimate whatever its
  basis) The reference compares like with like by size: at the estimate's level (building,
  estate or district), the median rent per sq ft of the other active rent listings within
  +/-30% of the flat's area when 5 or more exist, else the level's median. Same trust rule
  as for buying: a district reference is capped at the district's median rent per sq ft (all
  sizes); an RVD-average estimate scores unknown ("no rent listings near this flat").
- **Rent-yield trust** (Task 18b): the yield part counts in full only when the rent estimate
  rests on the building's or estate's own rent listings. A district-level estimate is pulled
  halfway to the district's median yield (the study's `rental_yield`; counts half when the
  study has none). An RVD-average estimate (no listings near the flat) scores the yield part
  as unknown (0). The reason names the basis: "rent yield 3.4% (estate rents)". On the
  10-22 snapshot 5,996 of 13,091 investor candidates had district-level rents.
  Capped (Task 21 helper): a district-level estimate's scored yield never exceeds the
  district's median yield, so cheap old flats with district rent guesses cannot max the yield
  part. The investor top 5 on the 10-22 snapshot is unchanged (all estate or building rents).
- **Near a place** = 100 at 15 minutes or less, 0 at 60 or more, linear; minutes are the
  average over the chosen stations of `commute.minutes`.
- **School** = 100 when the building's net is the profile's, else 0.
- **Space** = 0.75 x size + 0.25 x age, where size = 100 x area / (2 x the minimum size;
  400 sq ft when none is given), capped at 100, and age = 100 - 2 x building age in years.
- **Unknown** (no coordinates, no net, no rent estimate, no district in the study, a
  low-confidence discount): the part or goal scores 0 and the reason says "unknown". It is
  never scored as average or good. The overall score counts an unknown goal as 0.
- **Estates** rank by the mean overall score of their listings; listings follow in estate
  order. Weights that are all zero mean equal weights.
- Why these numbers: linear scales with round anchors so a reader can check a score by hand
  (a reason line names every input). Robust effects only, because the study calls the others
  hints. Rent intent reuses the investment slot for value-for-money.
- **Data** (Task 18b): the snapshot carries `master_building.school_net` in
  `buildings.parquet` (NULL on a database without the column) and the `mtr_station` table
  as `stations.parquet`; `advisor.load_data` reads both, with a CSV / Series override.

## Buy vs rent, sell vs let (2026-10-23, Claude's call)
`advisor.buy_vs_rent` and `advisor.sell_vs_let` (Task 19), each returning three cases.
- **Growth**: mid = the district's long-run median from the growth study (`districts.csv`,
  setting `all`, flats sold twice); low / high = mid shifted by the 25th and 75th percentile
  spread of N-year outcomes in the district's repeat-sales index (`index.csv`; the territory
  index when the district has none), N = the horizon. The quartiles are measured on the
  index, not on individual pairs, because the saved study has no per-pair percentiles; the
  shift keeps low <= mid <= high. Without the study: 3%/yr +/- 3 pp, marked a guess. On the
  10-22 study, 10 years: 灣仔區 4.8% (-0.6 to 9.1), 沙田區 3.9% (-1.2 to 8.6).
- **Rent growth** = price growth unless given (rents and prices move together long run).
- **Which rent** (changed 2026-10-23 after the pre-release review; it used the raw estimate,
  so district and RVD guesses fed every listing's figure): `advisor.comparison_rent`, the
  same trust rule as the score's yield part. Building or estate rents as they are; a
  district-level rent capped at the district's median yield x price (used as it is when the
  study has no median for the district); an RVD-average rent gives no buy-vs-rent figures,
  and the listing and its dashboard card say "no reliable rent figure for this building".
- **Buying** counts: stamp duty (costs.py, the profile's buyer type, agreement date = today);
  agent 1% on the purchase and 1% on the sale; legal 0.3% once (costs.py's convention);
  mortgage interest at the profile's rate on the largest loan HKMA rules allow
  (mortgage.max_loan; an `ltv` argument caps it lower), equal instalments over
  `mortgage_years`, closed form; rates 5% of the annual rent (taken as the rateable value;
  RVD; the progressive 8% / 12% steps above HK$550,000 rateable value from 2024-25 are not
  modelled); government rent 3% of the annual rent (Lands Department, Cap. 515; nominal-rent
  leases not modelled); management fee 10% of rent (a guess: HK$3-5 a sq ft a month is
  usual); the sale at the end at the assumed growth. Net cost = all of these less the gain.
- **Renting** counts the rent, less what the buyer's day-one cash (deposit, duty, fees) would
  earn at 3% a year (a guess: HKD time deposits and bonds paid 2-3.5% in 2025-26).
- **Same spending each month** (changed 2026-10-23 after the pre-release review; the renter
  used to invest only the day-one cash, so the owner's higher monthly outgoings were set
  against nothing): both sides are compared as wealth at the end of the horizon. Each month
  the renter invests the owner's outgoings (instalment + rates + government rent +
  management) less the rent, at 3% (the owner invests the gap when renting costs more),
  paid at the start of the month. Owner: sale less agent 1% less the loan left, plus what
  the owner invested. Renter: the day-one cash grown at 3%, plus what the renter invested.
  Worked example (tests): 8m flat, HK$20,000 rent, 10 years, 3% growth: buying 1.40m ahead
  (1.57m under the old count). On the 10-22 snapshot, investor profile (budget 8m, 12,914
  listings): median mid case +HK$3.2m (was +3.3m), buying ahead in 100% of listings in the
  mid case (was 100%) but only 3.9% in the low case. The mid case is driven by growth, not
  by this accounting: district medians are 2.9-5.8%/yr over 10 years (rent grows with
  them), and the median listing breaks even at about 0.7%/yr.
- **Selling now**: value less agent 1%, grown at the same 3% to the end of the horizon so the
  two sums are at one date. (changed 2026-10-23 after the pre-release review; the rents
  were summed as paid, at different dates) Each year's net rent counts as cash in
  mid-year, grown at 3% to the end: x (1.03)^(years - t - 0.5). Worked example (tests):
  8m flat, HK$20,000 rent, 4 years: letting 679,487 ahead (640,160 before).
  **Letting**: rent less a month empty and half a month's rent to
  the agent at each new tenancy (tenancies of 2 years; common practice, a guess), rates and
  government rent as above, management 10%, property tax 15% of rent received less rates
  after the 20% statutory allowance (IRD), plus the sale at the end less agent 1%. No
  mortgage on the flat, no special stamp duty (a resale within 2 years) and no legal fee on
  the sale are assumed.
- **Value for an owner** (`advise_owner`): the fair-price model's value for the building,
  area and floor (rooms, facing and sea view unknown), moved to today's RVD index level as
  rank.py does; rent from rent.py. The dashboard (Task 21) collects the inputs.
- Why: every line is a named constant with its source or "a guess" in advisor.py and in the
  `assumptions` dict the page can print, so a reader can change one number and see the
  effect. Tests work each function by hand (tests/test_advisor.py).

## Rebuild from scratch (2026-10-28, owner's answers)
- **Same steps, same result for everyone**: `hkestate rebuild` has no choices about what to
  scrape; `--estates` and `--max-requests` are for tests only. No data snapshot is shared.
- **Accepted limit**: listing history and Ricacorp deals can't be rebuilt (docs/rebuild.md).
- **Test small and extrapolate**: a 2-estate test, not a full-territory run.
- **A failed step waits, the rest go on**: after 3 tries a step and its dependants are deferred
  like a held lock; the run ends with exit code 2.
- **Yoho fixes built** (`check_peer_ask`, the shortlist flag, `repeat_price`).
- Claude's call: `repeat_price` uses 0.1%, not the 0.5% first proposed (the Grand Yoho repeats
  differ by under 0.01%).
- **28Hse 403 on 2026-10-28 21:57** (owner's OK): one restart of `backfill-territory` at the same
  4 s pace, by the one-off task `\hk-estate\one-off 28hse restart` (2026-10-29 13:00, after the
  daily job's 28Hse pass). A new 403 stops it for good and is discussed, not worked around.
- **Catch-up run** (owner's OK): after the 28Hse backfill finishes, run `geo`, `cd-history`,
  `srpe`, `link-ids`, `rvd`, then `train`, `rank`, `report`, so today's database matches what a
  fresh rebuild would hold.
- **Page-cache prune unchanged** (owner): the daily prune keeps deleting all pages on the three
  listing hosts after 7 days; rebuilds fetch again.

## Repository
- **Public GitHub repo `hk-estate` on account `bpy13`** (2026-10-16). Because it is public:
  scraped data, the database and raw-page cache are never committed (`data/` ignored), and no
  credentials or personal data appear in code or docs.
- **Phase 1 acceptance** (2026-10-16): complete transaction histories for the pilot estates
  loaded and spot-checked against the website; RVD indices loaded; scraper can stop and rerun
  without duplicating rows; schema documented; tests pass.
