# Ranking diagnosis: why the top of the bargain list is wrong (2026-10-23)

Plan 2026-10-23, Task 1. Read-only probe of `data/reports/ranked_listings_20261022.csv`
(29,744 rows) against the model snapshot `data/model/snapshot/` (buildings, deals, listings,
estates). No code changed. Tasks 2-3 build on the rules below.

## 1. The top 200 rows, one cause each

Precedence when a row fits two classes: subsidised, wrong building, area typo, gross area,
duplicate, first-hand comps, few comps, plausible.

| Class | Rows | Ranks |
|---|---|---|
| Plausible bargain | 105 | 16, 23, 24, 28, 33, 34, 37, 38, 40-42, 45-53, 55, 58-61, 63-65, 67, 68, 70-74, 77, 79, 83, 85-90, 92, 93, 96-98, 100, 103, 104, 108, 111, 112, 114, 118-120, 122, 124, 126-135, 138-140, 142, 144-146, 152, 155-157, 159, 160, 164-172, 174-176, 178, 179, 181, 184, 187-190, 199 |
| Few comparable sales (`comps_24m` < 10) | 39 | 14, 27, 35, 39, 54, 57, 62, 66, 75, 78, 84, 94, 95, 102, 105-107, 109, 110, 115, 116, 123, 125, 141, 143, 147-151, 153, 158, 161, 162, 173, 182, 192, 193, 200 |
| Duplicate listing | 17 | 15, 29, 31, 36, 44, 76, 99, 101, 113, 136, 183, 186, 194-198 |
| Wrong building match | 13 | 8, 12, 17, 19, 26, 30, 32, 69, 80-82, 117, 185 |
| Saleable vs gross area mixed up (or two flats in one listing) | 10 | 2, 5, 6, 9, 10, 11, 25, 154, 163, 191 |
| First-hand prices in the comparables | 8 | 7, 20, 22, 43, 56, 121, 177, 180 |
| Area typo or not one flat | 6 | 1, 4, 13, 21, 91, 137 |
| Subsidised estate | 2 | 3, 18 |

Two labels in the plan header do not hold up:

- **景翠苑 (ranks 2, 9, 163) is not subsidised by any price measure.** Its 21 deals since 2016
  run at HK$12,700-18,950/sq ft; its 24-month median (13,725) equals the Sham Shui Po median
  (13,539). The listings' areas (376, 443 sq ft) match none of the recorded unit areas (304,
  306, 561); 376/304 = 1.24, a typical gross-to-saleable ratio. Classed as gross area.
- **豐盛苑 (ranks 8, 19) is a wrong match.** The listings name blocks 富盛閣 (C座) and 榮盛閣
  (A座) with numbered flats of 381 sq ft. The master building `豐盛苑` is one 5-floor,
  20-flat building with flats A-D of 325-550 sq ft selling at 13,000-21,400/sq ft. The
  listings (and 7 Ricacorp 2026 deals at 7,900-10,500/sq ft) belong to a different, multi-block
  estate that shares the name. Classed as wrong building.

Only 銀蔚苑 (Mui Wo, 2018) behaves like a Home Ownership Scheme court: 24-month median
5,022/sq ft against 10,068 for Islands district (0.50x), and its 2018 launch sales at
3,800-4,400/sq ft.

## 2. Each class: what it looks like, how to detect it, proposed rule

### 2.1 Area typo or not one flat (6 rows)

Rank 1 is 瑧環 at 15,666 sq ft (its 14 recorded unit areas top out at 1,946). Rank 13 is a
3-floor, 6-flat building sold whole (9,053 sq ft, HK$160M). Rank 91 is "basement to 2/F, 4
floors" (5,403 sq ft). Rank 4 is "15/F" in a 5-floor building from 1959. Ranks 21 and 137 are
1.9x and 1.7x the nearest recorded unit area.

Fields: `area_sqft`; the building's recorded saleable areas (`deals.area_sqft` where
`area_basis = 'saleable'`, grouped by `building_id` and `flat`); `listings.features`.

Rule `check_area`: area under 150 or over 5,000 sq ft (whole ranking: 18 and 12 rows), **or**
area at least 1.5x the nearest recorded unit area when the building has 3+ recorded areas and
the listing's features do not say 複式, 天台 or 相鄰單位 (duplex, roof, adjacent flats). The
second part adds 4 of the 6 rows here; the plan's 150-5,000 band alone catches only ranks 1,
13 and 91.

### 2.2 Saleable vs gross mixed up, or two flats in one listing (10 rows)

Ranks 10/11 (維景灣畔 16座): 1,026 sq ft listed; recorded unit areas nearest 867 (1.18x).
Rank 154 (堅威大廈 flat D): 800 listed, recorded 600 (1.33x). Rank 191: 1.22x. 景翠苑: 1.24x.
Ranks 5/6 (維景灣畔 6座 "A+B", features 相鄰單位): two adjacent flats, 1,045 sq ft, asking
HK$9.1M, which is 8,708/sq ft against 15,122 for the block; the price looks like one flat's.
Rank 25 (香港黃金海岸): 1,052 vs nearest 721 (1.46x), borderline.

In the top 200, 54 rows give a flat letter; 51 of them match the recorded area of that flat
exactly (ratio 0.998-1.000). So the recorded unit areas are a reliable yardstick.

Fields: as 2.1, plus `listings.flat` and `features`.

Rule `check_area_basis`: with a flat letter, listed area / that flat's recorded saleable area
in 1.12-1.45; without one, listed area / nearest recorded unit area in 1.12-1.45 with 3+
recorded areas. Features containing 相鄰 get `check_combined`. Suggested order of checks:
exact match (within 3%) passes; 1.12-1.45 is gross; 1.5+ is `check_area`.

### 2.3 Duplicate listing (17 rows)

Same building, same area and price, different listing ids on the **same** site (ranks 5/6,
10/11, 22/29, 43/44 on Centanet; 193-198 are one 幹德道40號 flat listed by six Spacious
agents). `rank.py` only merges cross-site `dup_group`s, so these all stay.

Fields: `building_id`, `area_sqft`, `asking_hkd`, `floor_band`, `flat`, `source`.

Rule: same building, area within 2%, price within 2%, and floor band and flat **equal or
blank on either side**. Counts (whole ranking / top 200): Task 3's wording "equal or both
blank" catches 2,482 / 14; "equal or either blank" catches 3,126 / 17; ignoring band and flat
catches 6,274 / 21, but that merges different flats of the same type listed at the same
price (御海灣 flats E and H, ranks 22 and 31). Recommend "equal or either blank". Keep the
lowest asking price and list the others in `also_listed`, as now.

### 2.4 Wrong building match (13 rows)

- 逸瓏灣 (Mayfair by the Sea, Tai Po, about 13,000-17,000/sq ft) matched to 逸瓏 (Ho Man Tin,
  23,000-25,000/sq ft) by `match_method = contains`: ranks 12, 17, 26, 30, 32, 69, 117, 185.
  Nine rows in the whole ranking, all this estate.
- 峰景大廈 (Braemar Hill, URL says 寶馬山) matched by `single` to 峯景 in Kowloon City:
  ranks 80-82.
- 豐盛苑 (see section 1): ranks 8, 19.

Fields: `listings.match_method`, `listings.building_text`, `buildings.estate_name`,
`buildings.building_name`, `listings.url`.

Rules: (a) a `contains` match where the listing text continues the estate name with a Chinese
character (逸瓏 followed by 灣) is no match; a following digit, letter, space or bracket is a
block code and is fine (慧雅閣B座). (b) A `single` or `contains` match whose text names a
block (…閣, …座) that is not among the estate's buildings gets `check_match` (豐盛苑 富盛閣).
(c) Where the source URL carries an area name (Spacious: 寶馬山), compare it with the
building's district. Rule (a) is a one-line fix; (b) and (c) are flags for the manual check.
Whole ranking: 253 `contains`, 6,839 `single`, 31 `fuzzy` matches.

### 2.5 First-hand prices in the comparables (8 rows) and the 御海灣 question

御海灣 2期 1座 (rank 7): asking 9,000/sq ft, `comps_median_psf` 16,901, `fair_psf` 17,637,
confidence `normal` with 16 comps. The comps are **not** gross-area or wrong-building rows
(all 19 deals in the window are `saleable` and in the right building). They are mixed with
developer sales. Of the 16 "clean" comps, 6 are the flat's first-ever recorded sale, at a
median 21,593/sq ft, with developer-style prices (HK$10,753,100; 10,753,200; 11,618,000).
The 10 resales have a median 15,469. Only 2 of the building's 2024-25 developer sales carry
Centadata's first-hand label; the first-hand rule in `features.py` misses the rest because it
only counts a first sale within 24 months of completion (February 2021), and these sold 38-52
months after. SRPE does not cover the building (it covers 139 buildings in the snapshot; none
of 御海灣, The Henley, Novo Land, The Yoho Hub, 睿峯, 上然, 滙都, 柏瓏 are among them).

Effect on the numbers: the comps median is 9% above the resale level and the fair price 14-20%
above it (`fair_psf` / resale median = 1.14-1.20 for the 御海灣 rows). Rank 22's asking of
11,161 is 28% below the resale median, not 43% below fair. Rank 7's 9,000 is still 42% below
every resale and is probably a listing error; it needs the page check (Task 5).

Same pattern: 御海灣 1期 2座 (7 of 17 comps are first sales, 19,493 vs 14,710), The Henley
(99 of 102 comps, 31,381 vs 28,184), 睿峯 (7 of 12, 19,913 vs 17,642).

How big it is in the data: in buildings completed 2010 or later, 4,153 clean deals are a
flat's first recorded sale 24-72 months after completion. They sell at a median 1.12x the
same building's resales (1.13x at 24-36 months, 1.11x at 36-48, 1.14x at 48-60, 1.07x at 60-84,
then about 1.03x). Within 24 months the rule already removes the first sales; past 72 months
first sales look like ordinary resales. A round-price test (multiples of HK$10,000) does not
separate them: 32-46% of these late first sales are round, and the round ones are priced the
same (1.11x). At least 566 ranked rows sit in buildings with 3+ such sales in the comps window.

Fields: `deals.unit_id`, `agreement_date`, `buildings.completion_date`, `first_hand_label`,
`exclude_reason`.

Rule (for `features.first_hand_rule`, not for `rank.py`): for buildings completed 2010 or
later, a flat's first recorded sale counts as first-hand when it falls within **72 months** of
completion (today 24), unless Centadata labels it otherwise. Older buildings keep the 24-month
window because their histories are incomplete and a "first recorded sale" there is usually just
the first one we have. Retrain after the change; the fair prices of 2019-2024 estates move.
This is a `features.py` change and belongs with Task 8 (cheap-sale rule), which already edits
that file; the plan has no task for it yet.

### 2.6 Few comparable sales (39 rows)

Rows with `comps_24m` under 10 (`confidence = low`): old walk-ups, single-building Spacious
listings, luxury blocks with 0-6 deals. Where the building does have 3-6 comps, the model
sits far above them: 羅便臣道31號 fair 26,343 vs comps 20,355 (1.29x); 華翠園 1.32x; 樂怡閣
1.34x; 旭輝台 1.40x. The asking price sits at the comps level, so the "discount" is the model's
error, not a bargain.

Fields: `comps_24m`, `comps_median_psf`, `fair_psf`.

Rules: keep `low` off the shortlist (Task 4: shortlist from `normal` rows only). Add
`check_model` when `comps_24m` is 5 or more and `fair_psf` exceeds 1.25x `comps_median_psf`
(whole ranking 477 rows, top 200: 14). For context, with 10+ comps the ratio fair/comps is
centred at 1.06 (quartiles 1.02-1.10), so 1.25 is well outside normal.

### 2.7 Subsidised estate (2 rows) and how to detect one

What the snapshot offers:

- `buildings.me_excluded` is False for all 41,474 buildings and `exclude_reason` is null for
  all. `estates.parquet` has 2,083 excluded codes, but none of them has a building row: the
  master pass saves an excluded estate with `blocks = 0` and loads nothing under it
  (`cd_master.load_estate`). So the master flag can never mark a ranked listing. It removes 0
  active sale listings.
- The flag comes from Centadata's listing badges (`hos_flag`, `prh_flag`). An estate with no
  listing on the day of the master pass shows no badge and passes (the `cd_master` docstring
  says so). 銀蔚苑 passed this way.
- Names: 535 active sale listings sit in 113 estates ending 苑, 538 in 17 ending 邨 (321 of
  them 美孚新邨, private). Most are private; the suffix alone is useless.
- Price: estate 24-month clean median under 0.7x its district median, with 3+ deals, and a
  name ending 苑 or 邨: 47 estates, 7 active sale listings, 7 ranked rows (銀蔚苑 2, 和記新邨 2,
  安國新邨, 桃苑, 龍翔苑). Under 0.6x: 30 estates, 4 listings. Without the name test the 0.7x
  rule hits 1,258 estates and 940 listings, mostly old tenement blocks, so the name test is
  needed.

Rule `subsidised`, in this order: (1) `estates.excluded` or `me_excluded` true, or a code in
`data/manual/master_overrides.csv` with action `exclude` (0 rows today, kept for when the
master pass is rerun); (2) a new `data/manual/subsidised_estates.csv` of names, seeded with
銀蔚苑 and filled from the Housing Authority's published list of HOS, PSPS and TPS courts
(the robust fix; the names are public and stable); (3) as a stop-gap flag `check_subsidised`,
not an exclusion: name ends 苑 or 邨 and estate median under 0.7x district (7 listings). Task
2's suggested "ending in 苑 with no private sale history" cannot be evaluated: every estate's
deals are "private" in our data because the HOS second market is not labelled.

### 2.8 Plausible bargain (105 rows)

Normal confidence, exact or block-code match, area equal to the recorded flat area, no
developer sales in the comps, no duplicate. 60 are in Tuen Mun and 27 in Yuen Long. Checks:

- Against the last 6 months rather than 24: 81 rows have 3+ deals in the last 6 months; their
  asking is a median 0.73x that figure. The market is rising, not falling (every district's
  6-month median is 1-20% above its 24-month median), so the lag does not explain them.
- 67 of the 105 ask less than the lowest clean deal of their building in 24 months.
- Measured against the comps median instead of the model (asking x 0.95 / comps), 87 of the
  105 still show 20% or more.
- Why Tuen Mun and Yuen Long: asking prices there sit closest to deal prices (median
  asking/comps 1.10, against 1.22 on Hong Kong Island), and the two districts carry 3,294
  ranked rows with 10+ comps. A listing 25% under the comps is rarer elsewhere because sellers there ask
  more.

Rule: none excludes these. Add an information flag `below_all_recent` (asking per sq ft under
the building's lowest clean deal in 24 months; 67 rows here) so the manual check (Task 5)
starts with the strongest claims. Task 2's `check_price` at 0.4x / 2.5x the building median
catches 1 row below and 90 above in the whole ranking (0.5x: 5; 0.6x: 12); it is a typo guard,
not a filter, and 0.6x is a better lower bound (rank 1 is at 0.21x; nothing real is under
0.6x).

## 3. Other findings

- **Every row uses the assumed 5% asking-to-deal gap** (`gap_basis = assumed` for all 29,744
  rows): no district has reached 30 listing-deal pairs. The discount is therefore
  1 - 0.95 x asking / fair everywhere; the gap adds nothing yet.
- 12,025 of 29,744 rows are `low` confidence; 1,243 have no comps at all.
- `rank.py` merges only cross-site duplicates; same-site repeats are 2,482-3,126 rows (8-11%
  of the ranking) depending on the rule in 2.3.
- The master `exclude_reason` column is typed null in the snapshot (no value ever written),
  so `building_table`'s house/subsidised split (`excluded_estate`) is dead code on this data.

## 4. What this changes for Tasks 2-3

1. Task 2's "subsidised from `me_excluded` / `exclude_reason`" finds nothing; the master flag
   cannot reach a ranked building (2.7). Use the manual names file plus the 0.7x price flag.
2. Task 2's `check_area` needs the unit-area comparison (2.1) as well as the 150-5,000 band;
   the band alone leaves ranks 4, 21, 137 and all of 2.2 in place. Add `check_area_basis`
   (1.12-1.45x) and `check_combined` (相鄰).
3. Task 2's `check_price` lower bound: 0.6x instead of 0.4x; it stays a typo guard.
4. Task 3's duplicate rule: "band and flat equal or either blank", not "both blank" (2.3).
5. New, not in the plan: `check_match` for `contains`/`single` matches (2.4), `check_model`
   for fair price 1.25x above 5+ comps (2.6), `below_all_recent` as information (2.8).
6. New, not in the plan: extend the first-hand window to 72 months for buildings completed
   2010 or later (2.5), then retrain. Without it, every new estate's fair price and comps
   carry developer prices and the top of the list stays full of 2019-2024 estates.
7. The plan header's labels for 景翠苑 (subsidised) and 豐盛苑 (subsidised) are wrong by the
   data: gross area and wrong building respectively (section 1).

Scratch scripts and the joined top-200 table are in the session scratchpad (`t1\`), not in
the repo.
