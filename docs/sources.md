# Data sources

Phase 0 source spike, 2026-10-16. Every probe went through the project fetcher
(`hk-estate-research/0.1` user agent, robots.txt obeyed, ≥1 s per host, cached). Pages fetched
per host: hk.centanet.com 28 (Centadata + Centanet together), 28hse 13, rvd 2, spacious 2,
srpe 2, csdi 2, als 1, mtr 1, hk.centadata.com 1, plus one robots.txt per host. The probe
scripts are in `scripts/spike/` (throwaway).

Timings below assume about 1.4 s per page (1 s minimum gap + 0–0.5 s jitter + response
time), one host at a time. Different hosts can run in parallel. All counts are rough.

## Summary

| Source | Role | robots.txt | Page structure | History depth | Pages / time | Verdict |
|---|---|---|---|---|---|---|
| Centadata | building master, latest deal per flat | no robots.txt on hk.centadata.com (404); pages on hk.centanet.com (allowed) | server-rendered block pages (Nuxt) | latest deal per flat only, back to 1995 | ~27k blocks / ~10.5 h | **Keep**, not as transaction master |
| Centanet listings | sale + rent listings | allowed except `/centanetapi/api/`, `/home`, `/icms`, `/*Test` …; crawl-delay 1 | listing sitemap + server-rendered detail pages | current only | 43.7k first pass / ~17 h; daily new only ~0.5–0.8 h | **Keep** |
| 28Hse transactions | **transaction master** | allowed except `*search_words_thing=`, `*sortBy_by_text=` | server-rendered lists, block grids, per-flat pages | **2018 on** (lists to 2021-03) | ~195k / ~3 days | **Keep** |
| 28Hse listings and rents | sale + rent listings, rental deals | as above | server-rendered, 15 per page | current; rental deals as above | ~3,800 search pages / ~1.5 h a day | **Keep** |
| Squarefoot (+ haunted list) | listings, haunted list | robots.txt answers **403** to our user agent | not fetched | – | – | **Drop** (blocked) |
| Spacious | sale + rent listings | allowed; territory paging capped at page 100, `/api` disallowed | server-rendered; no floor/flat | current (+ price history) | ~800 pages / ~20 min a day | **Keep** (secondary) |
| house730 | listings | robots.txt answers **403** | not fetched | – | – | **Drop** (blocked) |
| Midland | listings | robots.txt answers **403** | not fetched | – | – | **Drop** (blocked) |
| SRPE | first-hand sales | everything allowed, **crawl-delay 30 s** | JS app + token API | not measured (platform since 2013) | ~3,600 requests / ~30 h one-off | **Keep, deferred** to Phase 3 |
| RVD | indices, popular developments, completions and stock | none (404) | legacy `.xls` files | 1979 on; latest Aug 2026 | ~20 files / ~1 min | **Keep** |
| Address Lookup Service | coordinates | no rules (robots.txt URL returns an HTML page) | XML API | – | ~25k lookups / ~10 h one-off | **Keep** |
| CSDI (MTR, buildings) | MTR exits, footprints | `api.csdi.gov.hk`: `Disallow: /`; `/geoportal/` disallowed | metadata pages; data via API | – | – | **Drop** (robots) |
| MTR open data (added) | station list | robots.txt 400 (treated as none) | one CSV, no coordinates | current | 1 file | **Keep** |

## Per source

### Centadata (Centaline transaction database)
- **Where it lives now:** `hk.centadata.com` is only a home page. Its building pages are served
  by `hk.centanet.com/findproperty/centadata-details/<estate>_<code>`, so the Centanet
  robots.txt applies (allowed).
- **Page structure:** server-rendered (Nuxt). One page per block (code `1-XXXXXXXXXX`; estate
  `2-`/`3-` codes open the first block). Each page holds a floor × flat grid with the
  **latest deal for each flat**: price, saleable and gross area, year, % change vs the
  previous deal, plus instrument date (`insDate`) and registration date (`regDate`) in the
  embedded data. It also has completion month (入伙日期), flat count, floor count, school net
  and the deal count for the last 3 years.
- **History depth:** the grid goes back to 1995, but **only one deal per flat** (the latest).
  The full per-flat history and every paged list (`/findproperty/list/transaction/...`,
  24 rows server-rendered, `?page=` ignored) come from `hk-api.centanet.com/centanetapi/api/`,
  which is a disallowed path. **We do not call it.** So Centadata cannot give a full deal
  history.
- **Estimated pages:** the estate sitemap lists 19,791 estates/phases; the region pages count
  5,630 (Island) + 8,821 (Kowloon) + 2,192 (NT East) + 2,140 (NT West) = 18,783 estates,
  including HOS and single buildings. About 25,000–30,000 block pages in all → roughly
  **10–12 hours** for one territory-wide pass.
- **Use:** master estate → block → flat list (names, codes, completion date, floors), and
  one older deal per flat. Weekly re-reads of block pages add new latest deals going forward.
- **Verdict: keep**, as the building master and a cross-check, not as transaction master.

### Centanet listings
- **robots.txt:** allowed for listing and detail pages; `/centanetapi/api/` and others
  disallowed; crawl-delay 1.
- **Page structure:** search pages are server-rendered but only the first 24 results
  (more pages come from the disallowed API). The sitemap
  `sitemap-index/findproperty/sitemap_TC_findproperty_detail.xml` (8.8 MB) lists **every live
  listing**: 35,549 sale + 8,180 rent today. Detail pages are server-rendered with floor band
  (低/中/高層), flat letter, price, saleable and gross area, rooms, age, facing, features and
  update date. They also show agent names, licence numbers and phones: **parsers must skip
  the agent block**.
- **History depth:** current listings only.
- **Estimated pages:** first full pass 43,700 detail pages ≈ **17 hours**. Daily after that:
  1 sitemap + new listings only (likely 1,000–2,000 ≈ 0.5–0.8 h). The sitemap has no useful
  `lastmod` (all today), so price changes on old listings need a rotating re-read (open item).
- **Verdict: keep.**

### 28Hse – transactions (new transaction master)
- **robots.txt:** allowed; avoid the `search_words_thing=` / `sortBy_by_text=` query params.
- **Page structure:** server-rendered HTML.
  - `/estate/detail/<name>-<id>/transaction/page-N`: 10 deals per page, newest first, with
    block, floor, flat, date, source (註冊處成交 = Land Registry, 市場成交 = agency report),
    rooms, saleable area, price and % gain vs the previous deal.
  - `/history`: one page per block, a floor × flat grid showing each flat's latest deal (year,
    price, deal count).
  - `/history/stateno-X/blockno-Y/unitid-Z`: one page per flat with all of its deals.
  - `/transaction/rent`: rental deals, same layout.
- **History depth (measured):** the site says **"我們只免費提供由2018年起之數據"** (free data only
  from 2018). Block grids show deals from 2018 on; the estate deal list ends at
  **2021-03-12** for Taikoo Shing (page 280 of 280; page 281 repeats it). Deals from
  2018 to March 2021 are therefore reachable only through the per-flat pages.
- **Estimated pages (territory-wide):** about 55,000 deals a year means about 300,000 deals
  from March 2021 → ~30,000 list pages ≈ **12 hours**; block grids ~15,000 pages ≈ 6 hours;
  per-flat pages for 2018–2021 deals ~150,000 pages ≈ **58 hours**. Full backfill ≈ **3 days**
  of run time (top of the agreed 1–3 days). The 2018–2021 per-flat pass can run last or be
  skipped.
- **Verdict: keep, and make it the transaction master** (Centadata cannot supply full
  history; see decisions.md).

### 28Hse – listings and rents
- **Page structure:** `/buy/apartment/page-N` and `/rent/apartment/page-N`, 15 listings per
  page, server-rendered. Today: **39,374** sale and **18,363** rent listings. Detail pages
  (`/buy/apartment/property-<id>`) have floor band and range, flat letter, gross and saleable
  area, rooms, facing, kitchen type, management fee, posting and update dates, features, plus
  agent free text (may contain agent names: keyword flags only, never store it).
- **Estimated pages:** full sweep of search pages ≈ 2,600 + 1,200 pages ≈ **1.5 hours a day**;
  detail pages for new listings only.
- **Verdict: keep.**

### Squarefoot (+ haunted-flat list)
- robots.txt returns **HTTP 403** to our user agent, so we treat the site as disallowing
  everything and fetched nothing else. The haunted-flat list is lost with it.
- **Verdict: drop** (blocked). Haunted flags come from listing text only.

### Spacious
- **robots.txt:** listing pages allowed; territory `/en/hong-kong/for-sale/page/N` only up to
  N=100; neighbourhood pages (`/en/hong-kong/n/<id>-<name>/for-sale/page/N`) have no cap;
  `/api` disallowed.
- **Page structure:** server-rendered; **15,961** sale listings today. Detail pages have
  price, saleable area, rooms, age, view and feature tags, and price history, but **no floor
  or flat**; agent licence shown (skip).
- **Estimated pages:** ~800 search pages a day via neighbourhood pages ≈ 20 minutes.
- **Verdict: keep** as a secondary listing source (weaker matching without floor).

### house730
- robots.txt returns **HTTP 403**. **Verdict: drop** (blocked).

### Midland
- robots.txt returns **HTTP 403**. **Verdict: drop** (blocked, as the plan expected).

### SRPE (first-hand sales)
- **robots.txt:** `Disallow:` (nothing), **`Crawl-delay: 30`**, now honoured by the fetcher.
- **Page structure:** the site is now a JavaScript single-page app (1.2 kB HTML shell). Data
  comes from `/api/SrpeWebService` after the visitor accepts a disclaimer, with a session
  token in an `Authorization` header. Not probed further.
- **History depth:** not measured (data behind the API). The platform started in 2013.
- **Estimated pages:** at 30 s per request, ~600 developments × ~6 requests ≈ **30 hours**
  for a one-off backfill, then weekly updates.
- **Verdict: keep, deferred to Phase 3** (Task 3.4): it needs a design decision on driving the
  disclaimer and API flow (open item).

### RVD (Rating and Valuation Department)
- **robots.txt:** none (404).
- **Page structure:** `publications/property_market_statistics.html` links legacy `.xls`
  files (`/doc/en/statistics/his_data_N.xls`, `private_domestic.xls`): average rents and
  prices by class (from 1982), rental and price indices by class (from 1979), popular
  developments index (from 1992), and completions and stock. Monthly, quarterly and annual
  sheets with bilingual headers. Needs `xlrd` (added).
- **History depth:** 1979 on; latest monthly value **August 2026** (provisional).
- **Estimated pages:** ~20 files ≈ 1 minute.
- **Verdict: keep.**

### Address Lookup Service (ALS)
- `https://www.als.gov.hk/lookup?q=<address>&n=1` returns XML with building name,
  district, HK1980 grid and WGS84 coordinates. One request per building.
- **Estimated pages:** ~25,000 buildings ≈ **10 hours** one-off.
- **Verdict: keep.**

### CSDI (MTR exits, building footprints)
- `api.csdi.gov.hk/robots.txt` is `Disallow: /`; portal `/geoportal/` is disallowed. Dataset
  pages under `/csdi-webpage/` are allowed but are metadata only; download links are served
  through the disallowed API.
- **Verdict: drop** for automated use. Replacement: MTR station names from MTR open data plus
  station coordinates from ALS (station-level, not exit-level). Building footprints and 3D
  view scoring are deferred (open item).

### MTR open data (added)
- `opendata.mtr.com.hk/data/mtr_lines_and_stations.csv`: line, station code, Chinese and
  English names (no coordinates). **Keep.**

## Pilot single-block building

**高雲臺 Goldwin Heights**, 2 Seymour Road, Mid-Levels West (Hong Kong Island). Single tower,
completed **July 1994**, **328 flats**, 41 floors. Centadata has a recorded latest deal for
**281 flats** (years 1995–2026) and **21 deals in the last 3 years**; 64 flats have a latest
deal in 2018 or later, so 28Hse should hold at least 64 deals since 2018. Together that
is well over the ≥100 deals the plan asks for. Centadata code
`2-SKSSGPYXPE`.

Also checked: 輝煌臺 Palatial Crest (single tower, 1996, 198 flats, smaller); 光明臺 (two towers);
嘉兆臺 (several blocks); 嘉薈軒 (single tower but completed 2007).

## Phase 1 results (2026-10-16)

**Centadata cross-check** (`hkestate crosscheck`; latest Centadata deal per flat vs 28Hse;
"in window" = 2018 on; match = same flat, price ±1%, date ±90 days):

| Estate | Blocks mapped | In window | Found in 28Hse | Rate |
|---|---|---|---|---|
| Taikoo Shing | 61/61 | 2,762 | 2,752 | 99.6% |
| Mei Foo Sun Chuen | 99/99 | 2,980 | 2,794 | 93.8% |
| City One Sha Tin | 52/52 | 2,971 | 2,945 | 99.1% |
| Goldwin Heights | 1/1 | 71 | 70 | 98.6% |
| LOHAS Park | pending (backfill still loading) | | | |

- 28Hse's date equals Centadata's **registration** date in 98% of matches (the rest are
  within a few days); never the agreement date (1 case).
- Mei Foo's misses are 2018–2020 deals 28Hse doesn't hold (its own per-flat counts are all
  loaded); from 2021 on Mei Foo is near-complete. The Goldwin Heights miss is a
  HK$29.5m deal at 2.3x the building's usual price that 28Hse doesn't list.
- Naming differences handled: Centadata groups Mei Foo blocks as address ranges
  (百老匯街1-3號, flats "1號A"), writes 蘭秀道 for 28Hse's 蘭秀街, and 廬山閣 for 盧山閣.
- Deals only Centadata has are stored in `txn_raw` with `source='centadata'` (with both
  agreement and registration dates); that includes each flat's latest pre-2018 deal.

**28Hse rate limit:** after ~1,600 requests at 1/s it answered 429 and then allowed little
for a while; the backfill now runs at one request per 4 s (see known-issues).

**Spot-check:** pending (runs after the LOHAS Park load).

## Territory-wide backfill estimate (transaction master)

| Pass | Pages | Time |
|---|---|---|
| Centadata block pages (building master + latest deal per flat) | ~27,000 | ~10.5 h |
| 28Hse estate deal lists, March 2021 → now | ~30,000 | ~12 h |
| 28Hse block grids | ~15,000 | ~6 h |
| 28Hse per-flat pages for 2018 → March 2021 deals | ~150,000 | ~58 h |

The hosts are different, so Centadata and 28Hse can run side by side: about **3 days** of
wall-clock time for 28Hse, which is the limit. History before 2018 exists only as one latest
deal per flat (Centadata), so the plan's "history from 2000" can't be met with free
sources (see open-items).
