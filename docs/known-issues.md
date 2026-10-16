# Known issues and limits

How things behave that a newcomer would trip over: limits we live with, traps, gotchas.
Bugs that will be fixed go in `open-items.md` instead.

## Data coverage

- **Full deal history only from 2018.** 28Hse gives free full histories from 2018 on. Before
  that, Centadata shows only each flat's latest deal (back to 1995). The full Centaline
  history sits behind `/centanetapi/api/`, which robots.txt disallows, so we never call it.
  Long-horizon growth figures are limited by this; see open-items for options.
- **Blocked sources.** Squarefoot (including its haunted-flat list), house730 and Midland
  return 403 to our user agent, even for robots.txt. They are dropped, not worked around.
- **MTR station positions come from Wikipedia** (title coordinates of each station's article,
  about 50 m accuracy; 14 stations from ALS) because no robots-allowed official file has them.
  Distances are to the station centre, not an exit.
- **CSDI data API disallowed by robots.txt.** MTR distance uses station centres from MTR open
  data, not station exits.
- **Spacious listings have no floor or flat.** They match only to a building.
- **Many Centanet and 28Hse listings hide the floor and flat** (agents leave them out), so
  their floor band is `unknown`; they still match to a building.
- **Listing free text is never stored.** Titles and blurbs may name agents or give phone
  numbers, so they are read only for keyword flags; to re-flag old listings, the page has to
  be read again.
- **28Hse search pages may show promoted listings from other estates**; listings whose
  estate id differs from the estate being read are dropped.

- **28Hse dates are registration dates**, 2–6 weeks after the agreement (see decisions).
- **LOHAS Park is 22 separate developments on 28Hse** (首都, 領都, ... Grand Seasons), loaded
  as estates grouped under `lohas-park` (`estate.group_name`). 28Hse's own "日出康城" page is
  empty.
- **28Hse agency-reported ("market") deals often give only a floor band** (高層/中層/低層).
  They are kept and dropped as duplicates once the Land Registry deal arrives; a market
  deal that never gets registered stays with a band instead of a floor.

## Fair price and ranking (Phase 4)

- **Every district's asking-vs-deal gap is the 5% default (`assumed`)** until 30 listing-deal
  pairs exist in a district; listings only started on 2026-10-16.
- **Most listings give no exact floor.** They are valued at their band's middle (高層 = 5/6 of
  the building's height) or at mid-height when no band is given (`floor_basis` `imputed`),
  so floor premiums are averaged out for them.
- **Valuations run a few % low in a rising market.** The out-of-time test shows a median
  signed error of about -3% (index frozen at the cut-off); listings use the RVD index carried
  forward from its last published month, about two months old.
- **Missing features stay missing.** MTR distance and coordinates cover about 72% of training
  deals (geocoding is partial), rooms 9%, facing 6% (only from listings that state it).
  Coverage is in `data/reports/model_eval.md`; retrain after the geocoding and backfill runs.
- **Rooms come almost only from listings**, so the model learns little from them.
- **Listings in buildings the model never saw** get no building effect (the category is
  missing to the model) and lean on estate, district and location.
- **The dashboard shows the last `hkestate rank`**, not live listings; rerun rank after the
  daily job.

## Scraping

- **28Hse rate-limits long runs.** After ~45 minutes at 1 request/s (~1,600 requests) it
  answered 429, then allowed only ~25 requests per 10-minute pause (a rolling quota). The
  backfill now waits 4 s between requests (~900 an hour), pauses 15 minutes on a 429/5xx
  and stops after three in a row on one page; rerunning resumes. A territory-wide backfill
  at this pace is far slower than the Phase 0 estimate (see open items).
- **The daily job skips 28Hse while a backfill runs** (lock `data/locks/hse28.lock`), so on
  those days 28Hse listings get no snapshot; the day doesn't count towards delisting.
- **Cached listing pages are deleted after 7 days** (they show agent details), so a listing
  page older than that can't be re-parsed from the cache.
- **DuckDB allows one process at a time.** Long jobs open the database only while writing;
  other commands wait for the file (up to 5 minutes).

- **robots.txt wildcards.** Python's built-in `urllib.robotparser` ignores `*` patterns, which
  Centanet, 28Hse and Spacious use, so the fetcher uses `protego` instead. Don't swap it back.
- **A failed robots.txt fetch blocks that host for the run.** This is deliberate (strict).
  The fetcher retries robots.txt twice on a server or network error first, so only a
  lasting failure stops a source.
