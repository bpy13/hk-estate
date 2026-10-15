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

## Scraping

- **robots.txt wildcards.** Python's built-in `urllib.robotparser` ignores `*` patterns, which
  Centanet, 28Hse and Spacious use, so the fetcher uses `protego` instead. Don't swap it back.
- **A failed robots.txt fetch blocks that host for the run.** This is deliberate (strict), but
  a single network error can stop a source until retries are added (see open-items).
