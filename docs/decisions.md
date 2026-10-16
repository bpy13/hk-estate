# Decisions

What was decided, why, and when. Change these only with a reason, and note the change here
(mark reversed decisions as reversed rather than deleting them).

## Scope
- **Purpose: the owner's own investment research** (2026-10-16). Not a product, no clients,
  single user. Keeps scraping in personal-use territory and polish modest.
- **Coverage: whole territory, private residential flats only** (2026-10-16). Excludes HOS /
  subsidised flats (resale restrictions distort prices), houses/villas, commercial. Build is
  proven on a few pilot estates first, then scaled to the whole territory within the plan.
- **History from 2000** (2026-10-16). Covers several cycles (2003 trough, 2008, 2021 peak,
  2021–24 fall, 2025–26 recovery).
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
- **Request pacing:** Centanet 2 s (hk.centanet.com is shared with the Phase 3 Centadata
  crawl, which also uses 2 s, so the host sees ~1 request/s), 28Hse 4 s, Spacious 1 s.
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

## After Phase 2 (2026-10-17)
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
    filtered read-only by fetch time (`db.QUARANTINE_SQL`; 8,954 are still the newest copy)
    before the duplicate/bulk/outlier tests, so they can't distort them. Deleting them still
    waits for the owner (open-items).
- **Facing and sea view are flat-level**: a listing stating them lends them to every deal on
  the same building + flat letter. Listings without a floor are valued at their band's middle
  (1/6, 1/2, 5/6 of the building) or, with no band, at mid-height (marked `imputed`).
- **SHAP values come from LightGBM's own TreeSHAP** (`pred_contrib=True`), the same algorithm
  as the `shap` package: importing `shap` fails on this laptop because Windows Application
  Control blocks a scikit-learn DLL it loads.

## Repository
- **Public GitHub repo `hk-estate` on account `bpy13`** (2026-10-16). Because it is public:
  scraped data, the database and raw-page cache are never committed (`data/` ignored), and no
  credentials or personal data appear in code or docs.
- **Phase 1 acceptance** (2026-10-16): complete transaction histories for the pilot estates
  loaded and spot-checked against the website; RVD indices loaded; scraper can stop and rerun
  without duplicating rows; schema documented; tests pass.
