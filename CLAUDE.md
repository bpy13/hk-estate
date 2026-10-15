# HK Estate – notes for Claude

A personal research tool for Hong Kong residential property. It collects transaction
history, current listings and flat attributes (size, rooms, floor, facing, location), stores
them in a structured local database, and analyses them to find (a) listings priced below
market and (b) flat attributes linked to better long-run price growth. Single user (the
owner); not a public service. Scope and details are still in discovery: see docs.

Read these before substantial changes:

- `docs/decisions.md` – what was decided and why (don't re-litigate without a reason)
- `docs/open-items.md` – pending work and questions waiting on the user
- `docs/plans/` – the latest plan
- `docs/architecture.md` – modules, data flow, where things live (once it exists)
- `docs/known-issues.md` – limits and traps found so far (once it exists)
- `README.md` – user-facing setup and commands

## Environment
- Install: <to be set in phase 1 (conda env, python-env skill)>
- Run: <tbd>
- Tests: <tbd>
- Lint: <tbd>

## Project rules
- Scrape politely: obey robots.txt, at least 1 request/second per site, identify a cache so
  pages are never fetched twice. Never call paths robots.txt disallows (e.g. centanet
  `/centanetapi/api/`).
- Never store personal data: no owner/buyer names, no agent names or phone numbers (keep
  only the listing URL). Hong Kong's privacy law (PDPO) applies.
- Raw scraped data and the database stay out of git (data/ is ignored).

## Conventions
<tbd>
