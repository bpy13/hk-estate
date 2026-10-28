"""Link 28Hse estates and buildings (`h28-` ids) to the master list (`cd-` ids).

The Centadata master pass mints `cd-<code>` ids; the 28Hse backfill mints `h28-<id>` ids. A
flat that both sources know must end up as one unit, or the duplicate rule can't pair their
deals and repeat-sales pairs would be split. The link is stored in `source_map`:

- (`hse28`, `building`, h28 building id) -> the master building id. `txn_all` reads it, so a
  28Hse deal in a mapped building gets the master building's unit id.
- (`hse28`, `estate`, `h28-<id>`) -> the first master estate it was matched to (information).
- The five pilot estates were loaded the other way round: the Centadata cross-check stored
  their Centadata deals under the 28Hse ids, so the 28Hse building is its own master building
  (matched_by `pilot`; Centadata block codes are recorded as (`centadata`, `building`, code)).

Estates are matched by name (28Hse estate name against Centadata estate/phase names), then
buildings by phase and block name with the same rules as the pilot cross-check
(`backfill.match_buildings`). Anything ambiguous stays unmapped and is counted, never guessed.
A row with matched_by `manual` is never overwritten.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from datetime import datetime

from hkestate import db
from hkestate.backfill import PILOT_ESTATES, _key, match_buildings

INDEX_DDL = """
CREATE TABLE IF NOT EXISTS hse28_estate (
    id            INTEGER PRIMARY KEY,   -- 28Hse estate id (/estate/detail/<name>-<id>)
    name          VARCHAR,               -- name as 28Hse writes it in the URL
    found_by      VARCHAR,               -- sitemap, listings, csv, pilot
    master_codes  VARCHAR,               -- matched Centadata 2- codes, comma separated
    match_status  VARCHAR,               -- exact, prefix, ambiguous, none
    excluded      BOOLEAN,               -- every matched master estate is outside the study
    size_flats    INTEGER,               -- flats (Centadata unitCnt) of the matched estates
    size_deals    INTEGER,               -- Centadata latest deals stored for them
    mapped_at     TIMESTAMP,
    added_at      TIMESTAMP DEFAULT current_timestamp
)"""


def ensure_tables(con) -> None:
    from hkestate.sources import cd_master

    con.execute(INDEX_DDL)
    cd_master.ensure_tables(con)


# ---- estate names ------------------------------------------------------------------------------

# What may follow an estate name when 28Hse names the whole estate and Centadata a phase.
_NUM = "0-9一二三四五六七八九十"
_PHASE_TAIL =re.compile(rf"^(第?[{_NUM}]+期|第?[{_NUM}a-z]+座|phase|期)")


def est_key(text: str | None) -> str:
    """Comparable estate name: Traditional, one spelling of the variant characters, no
    spaces or separators, lower case."""
    return re.sub(r"[\s\-·・()（）]", "", _key(text or "")).lower()


def _group_of(m: dict) -> str:
    return m["parent_code"] or m["code"]


def match_estate(key: str, masters: list[dict], keys: list[str]) -> tuple[str, list[dict]]:
    """(status, master estates) for one 28Hse estate name key. `masters` is sorted by name
    key and `keys` holds those keys, so the prefix search is a range lookup."""
    if len(key) < 2:
        return "none", []
    lo = bisect.bisect_left(keys, key)
    hi = bisect.bisect_left(keys, key + "\U0010ffff")
    near = masters[lo:hi]
    exact = [m for m in near if m["key"] == key]
    status, hits = "exact", exact
    if not exact:
        status = "prefix"
        hits = [m for m in near if _PHASE_TAIL.match(m["key"][len(key):])]
    if not hits:
        return "none", []
    if len({_group_of(m) for m in hits}) > 1:
        return "ambiguous", []  # same name in more than one estate (e.g. two districts)
    return status, hits


def map_estates(con, skip_ids: set[int] | None = None) -> dict:
    """Match every indexed 28Hse estate to master estates and store size and status in
    `hse28_estate`. Pilot estates (or `skip_ids`) are left alone. Returns counts per status."""
    ensure_tables(con)
    masters = [
        {"code": c, "parent_code": p, "key": est_key(n), "excluded": bool(x), "units": u or 0}
        for c, p, n, x, u in con.execute(
            "SELECT code, parent_code, name_zh, excluded, units FROM master_estate "
            "WHERE NOT coalesce(pilot, false)").fetchall()
        if est_key(n)
    ]
    masters.sort(key=lambda m: m["key"])
    keys = [m["key"] for m in masters]
    deals = dict(con.execute(
        "SELECT estate_id, count(*) FROM txn_latest WHERE source = 'centadata' "
        "GROUP BY estate_id").fetchall())
    if skip_ids is None:
        skip_ids = {r.id for refs in PILOT_ESTATES.values() for r in refs}
    counts: dict[str, int] = defaultdict(int)
    now = datetime.now()
    for hid, name in con.execute("SELECT id, name FROM hse28_estate").fetchall():
        if hid in skip_ids:
            continue
        status, hits = match_estate(est_key(name.replace("-", " ")), masters, keys)
        counts[status] += 1
        con.execute(
            "UPDATE hse28_estate SET master_codes = ?, match_status = ?, excluded = ?, "
            "size_flats = ?, size_deals = ?, mapped_at = ? WHERE id = ?",
            [",".join(m["code"] for m in hits) or None, status,
             bool(hits) and all(m["excluded"] for m in hits),
             sum(m["units"] for m in hits) if hits else None,
             sum(deals.get(f"cd-{m['code']}", 0) for m in hits) if hits else None,
             now, hid],
        )
        if hits:
            _put_map(con, "estate", f"h28-{hid}", f"cd-{hits[0]['code']}", name, status)
    return dict(counts)


# ---- estate subsets (`--estates`, test rebuilds) -------------------------------------------------
#
# A subset is a set of keys: pilot group names (`taikoo-shing`, ...: their data sits under 28Hse
# ids) and Centadata `2-` codes of master estates. Commands with `--estates` turn what the user
# typed (names, `2-`/`3-` codes, pilot group names, or `pilot`) into these keys once.

_CODE = re.compile(r"^[23]-[A-Z]+$")


def name_matches(key: str, candidate: str) -> bool:
    """`candidate` (an est_key) is the estate `key` names: the same name, or the name followed
    by a phase or block (e.g. 'xx' and 'xx第2期'). The same rule as `match_estate`."""
    if len(key) < 2 or not candidate.startswith(key):
        return False
    return candidate == key or bool(_PHASE_TAIL.match(candidate[len(key):]))


def pilot_group(token: str) -> str | None:
    """The pilot group a token names (its group name, Chinese name or Centadata entry code)."""
    from hkestate.sources.centadata import PILOT_ENTRIES

    for group, (name, code) in PILOT_ENTRIES.items():
        if token in (group, code) or est_key(token) == est_key(name):
            return group
    return None


def resolve_estates(con, tokens: list[str]) -> set[str]:
    """Subset keys for `--estates` tokens: pilot group names and master `2-` codes. A `3-`
    code (a big estate) gives its phases; a name gives the master estates it names. A token
    that matches nothing adds nothing (the caller says so)."""
    from hkestate.sources.centadata import PILOT_ENTRIES

    ensure_tables(con)
    masters = con.execute(
        "SELECT code, parent_code, name_zh FROM master_estate "
        "WHERE NOT coalesce(pilot, false)").fetchall()
    keys: set[str] = set()
    for raw in tokens:
        token = raw.strip()
        if not token:
            continue
        if token == "pilot":
            keys.update(PILOT_ENTRIES)
        elif group := pilot_group(token):
            keys.add(group)
        elif token.startswith("2-") and _CODE.match(token):
            keys.add(token)
        elif _CODE.match(token):  # 3-: the phases of the big estate
            keys.update(c for c, p, _ in masters if p == token)
        else:
            k = est_key(token)
            keys.update(c for c, _, n in masters if name_matches(k, est_key(n)))
    return keys


def subset_codes(keys: set[str] | None) -> set[str] | None:
    """The master `2-` codes of a subset (None stays None: no subset)."""
    return None if keys is None else {k for k in keys if k.startswith("2-")}


def subset_groups(keys: set[str] | None) -> set[str] | None:
    """The pilot group names of a subset (None stays None: no subset)."""
    return None if keys is None else {k for k in keys if not k.startswith("2-")}


def subset_building_ids(con, keys: set[str]) -> set[str]:
    """Every building id of a subset, in `master_building` and `building`: the master
    buildings of its `2-` codes, the 28Hse buildings of estates matched to those codes, and
    the buildings of its pilot groups."""
    ensure_tables(con)
    codes, groups = sorted(subset_codes(keys)), sorted(subset_groups(keys))
    out: set[str] = set()
    if codes:
        marks = ",".join("?" for _ in codes)
        out |= {r[0] for r in con.execute(
            f"SELECT building_id FROM master_building WHERE estate_code IN ({marks})",
            codes).fetchall()}
        h28 = [f"h28-{hid}" for hid, mc in con.execute(
            "SELECT id, master_codes FROM hse28_estate WHERE master_codes IS NOT NULL").fetchall()
            if set(mc.split(",")) & set(codes)]
        eids = [f"cd-{c}" for c in codes] + h28
        marks = ",".join("?" for _ in eids)
        out |= {r[0] for r in con.execute(
            f"SELECT building_id FROM building WHERE estate_id IN ({marks})", eids).fetchall()}
    if groups:
        marks = ",".join("?" for _ in groups)
        out |= {r[0] for r in con.execute(
            f"SELECT b.building_id FROM building b JOIN estate e USING (estate_id) "
            f"WHERE e.group_name IN ({marks})", groups).fetchall()}
    return out


def subset_name_keys(con, keys: set[str]) -> set[str]:
    """Comparable names (`est_key`) of a subset's estates: the master names of its codes and
    the pilot groups' names. For sources that know estates only by name (Ricacorp)."""
    from hkestate.sources.centadata import PILOT_ENTRIES

    ensure_tables(con)
    codes = sorted(subset_codes(keys))
    names = {PILOT_ENTRIES[g][0] for g in subset_groups(keys) if g in PILOT_ENTRIES}
    if codes:
        marks = ",".join("?" for _ in codes)
        names |= {r[0] for r in con.execute(
            f"SELECT name_zh FROM master_estate WHERE code IN ({marks})", codes).fetchall()}
    return {k for n in names if (k := est_key(n))}


def names_match(name: str, name_keys: set[str]) -> bool:
    """`name` is one of the subset's estates, the whole estate one of them is a phase of, or
    a phase of one of them."""
    k = est_key(name)
    return any(name_matches(k, s) or name_matches(s, k) for s in name_keys)


# ---- source_map ---------------------------------------------------------------------------------


def _put_map(con, kind: str, key: str, master_id: str, name: str, how: str,
             source: str = "hse28") -> bool:
    """Write a source_map row unless a manual one is there. Returns True if written."""
    row = con.execute(
        "SELECT matched_by FROM source_map WHERE source = ? AND kind = ? AND source_key = ?",
        [source, kind, key]).fetchone()
    if row and row[0] == "manual":
        return False
    con.execute("INSERT OR REPLACE INTO source_map (source, kind, source_key, master_id, name, "
                "matched_by) VALUES (?, ?, ?, ?, ?, ?)", [source, kind, key, master_id, name, how])
    return True


def link_buildings(con, hse_estate_id: int) -> dict:
    """Map the 28Hse buildings of one estate to master buildings. Needs the 28Hse grid
    (building rows) and the master list for the matched estates; call it again once either
    arrives. A master building matched by one 28Hse building is mapped; a 28Hse building
    claimed by two master buildings is ambiguous and left alone.

    Matching runs from the master side (as in the pilot cross-check): Centadata's block text
    names the 28Hse buildings, and one range block ('百老匯街1-3號') may cover several."""
    ensure_tables(con)
    row = con.execute("SELECT master_codes FROM hse28_estate WHERE id = ?",
                      [hse_estate_id]).fetchone()
    eid = f"h28-{hse_estate_id}"
    stats = {"master_buildings": 0, "mapped": 0, "ambiguous": 0, "unmapped": 0}
    if not row or not row[0]:
        return stats
    codes = row[0].split(",")
    marks = ",".join("?" for _ in codes)
    master = con.execute(
        f"SELECT building_id, coalesce(phase_name, ''), coalesce(block_name, '') "
        f"FROM master_building WHERE estate_code IN ({marks})", codes).fetchall()
    h28 = {
        r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT b.building_id, e.name_zh || ' ' || coalesce(p.name_zh, ''), b.name_zh "
            "FROM building b JOIN estate e USING (estate_id) LEFT JOIN phase p USING (phase_id) "
            "WHERE b.estate_id = ?", [eid]).fetchall()
    }
    stats["master_buildings"] = len(master)
    claims: dict[str, list[str]] = defaultdict(list)  # h28 building -> master buildings
    for mid, phase, block in master:
        for hb in match_buildings(f"{phase} {block}".strip(), h28):
            claims[hb].append(mid)
    names = {}
    for hb, mids in claims.items():
        if len(mids) > 1:
            stats["ambiguous"] += 1
            continue
        if _put_map(con, "building", hb, mids[0], h28[hb][1], "name"):
            stats["mapped"] += 1
            names[hb] = mids[0]
    stats["unmapped"] = len(h28) - stats["mapped"] - stats["ambiguous"]
    copy_units(con, list(names))
    return stats


def copy_units(con, h28_building_ids: list[str]) -> None:
    """Give each mapped 28Hse flat a unit row under the master building id, so floor number,
    areas and rooms are there when `txn_all` joins on the master unit id."""
    if not h28_building_ids:
        return
    marks = ",".join("?" for _ in h28_building_ids)
    con.execute(
        f"""
        INSERT INTO unit (unit_id, building_id, floor_label, floor_num, floor_kind, flat,
                          saleable_sqft, gross_sqft, rooms)
        SELECT sm.master_id || '|' || u.floor_label || '|' || u.flat, sm.master_id,
               u.floor_label, min(u.floor_num), min(u.floor_kind), u.flat,
               max(u.saleable_sqft), max(u.gross_sqft), max(u.rooms)
        FROM unit u JOIN source_map sm
          ON sm.source = 'hse28' AND sm.kind = 'building' AND sm.source_key = u.building_id
        WHERE u.building_id IN ({marks})
        GROUP BY sm.master_id, u.floor_label, u.flat
        ON CONFLICT DO UPDATE SET
            floor_num = coalesce(unit.floor_num, excluded.floor_num),
            saleable_sqft = coalesce(unit.saleable_sqft, excluded.saleable_sqft),
            gross_sqft = coalesce(unit.gross_sqft, excluded.gross_sqft),
            rooms = coalesce(unit.rooms, excluded.rooms)
        """,
        h28_building_ids,
    )


def link_loaded(con, estates: set[str] | None = None) -> dict:
    """Re-map the buildings of every indexed estate whose grid is loaded. Returns totals.
    `estates` (subset keys): only 28Hse estates matched to one of these master codes."""
    total: dict[str, int] = defaultdict(int)
    rows = con.execute(
        "SELECT h.id, h.master_codes FROM hse28_estate h WHERE h.master_codes IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM building b WHERE b.estate_id = 'h28-' || h.id)").fetchall()
    codes = subset_codes(estates)
    ids = [hid for hid, mc in rows if codes is None or set(mc.split(",")) & codes]
    for hid in ids:
        for k, v in link_buildings(con, hid).items():
            total[k] += v
    total["estates"] = len(ids)
    return dict(total)


# ---- pilot -------------------------------------------------------------------------------------


def link_pilot(con) -> dict:
    """Record the pilot link. 28Hse pilot buildings are their own master buildings; the
    Centadata block code of each Centadata deal (it is the last part of the page URL) is
    recorded against the 28Hse building the cross-check placed the deal in."""
    ensure_tables(con)
    pilot_ids = [f"h28-{r.id}" for refs in PILOT_ESTATES.values() for r in refs]
    marks = ",".join("?" for _ in pilot_ids)
    own = con.execute(
        f"SELECT building_id, name_zh FROM building WHERE estate_id IN ({marks})",
        pilot_ids).fetchall()
    for bid, name in own:
        _put_map(con, "building", bid, bid, name, "pilot")
    blocks = con.execute(
        f"""
        SELECT DISTINCT regexp_extract(url, '_(1-[A-Z]+)$', 1) AS code, building_id
        FROM txn_raw WHERE source = 'centadata' AND estate_id IN ({marks})
          AND regexp_extract(url, '_(1-[A-Z]+)$', 1) <> ''""", pilot_ids).fetchall()
    per_code: dict[str, list[str]] = defaultdict(list)
    for code, bid in blocks:
        per_code[code].append(bid)
    n = 0
    for code, bids in per_code.items():
        for bid in bids:
            # one Centadata block over several 28Hse buildings (Mei Foo ranges): code|building
            key = code if len(bids) == 1 else f"{code}|{bid}"
            n += _put_map(con, "building", key, bid, "", "pilot", source="centadata")
    return {"hse28_buildings": len(own), "centadata_blocks": n}


def shared_unit_report(con) -> list[tuple]:
    """Per pilot estate: flats with deals from both sources on the same unit id, and
    Centadata deals whose unit is unknown to the unit table. For the pilot check."""
    return con.execute(
        """
        WITH t AS (SELECT source, unit_id, building_id FROM txn_all WHERE building_id LIKE 'h28-%')
        SELECT split_part(building_id, '-', 2) AS estate,
               count(DISTINCT unit_id) FILTER (WHERE source = 'hse28') AS hse_units,
               count(DISTINCT unit_id) FILTER (WHERE source = 'centadata') AS cd_units,
               count(DISTINCT unit_id) FILTER (WHERE unit_id IN (
                   SELECT unit_id FROM t WHERE source = 'hse28')
                 AND source = 'centadata') AS shared_units
        FROM t GROUP BY 1 ORDER BY 1
        """).fetchall()


def link_all(con, estates: set[str] | None = None) -> dict:
    """Everything: pilot, estate matching, building matching. `estates` (subset keys) limits
    the building matching; the pilot link and the name matching are quick and run whole."""
    db.init_db(con)
    ensure_tables(con)
    return {"pilot": link_pilot(con), "estates": map_estates(con),
            "buildings": link_loaded(con, estates)}

