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
- **CSDI data API disallowed by robots.txt.** MTR distance uses station centres from MTR open
  data, not station exits.
- **Spacious listings have no floor or flat.** They match only to a building.

- **28Hse dates are registration dates**, 2–6 weeks after the agreement (see decisions).
- **LOHAS Park is 22 separate developments on 28Hse** (首都, 領都, ... Grand Seasons), loaded
  as estates grouped under `lohas-park` (`estate.group_name`). 28Hse's own "日出康城" page is
  empty.
- **28Hse agency-reported ("market") deals often give only a floor band** (高層/中層/低層).
  They are kept and dropped as duplicates once the Land Registry deal arrives; a market
  deal that never gets registered stays with a band instead of a floor.

## Scraping

- **28Hse rate-limits long runs.** After ~45 minutes at 1 request/s it answered 429. The
  backfill now waits 2 s between requests, pauses 10 minutes on a 429/5xx and stops after
  three in a row; rerunning resumes.
- **DuckDB allows one process at a time.** Long jobs open the database only while writing;
  other commands wait for the file (up to 5 minutes).

- **robots.txt wildcards.** Python's built-in `urllib.robotparser` ignores `*` patterns, which
  Centanet, 28Hse and Spacious use, so the fetcher uses `protego` instead. Don't swap it back.
- **A failed robots.txt fetch blocks that host for the run.** This is deliberate (strict).
  The fetcher retries robots.txt twice on a server or network error first, so only a
  lasting failure stops a source.
