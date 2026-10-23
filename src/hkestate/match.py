"""Match listings to master buildings and floor bands, and find cross-site duplicates.

Building names are compared after normalising (Traditional Chinese, half-width, lower case,
brackets and hyphens as spaces, 臺/台-style variants, 第N座 = N座, 街/道/路/徑 alike before
a street number). In order:
1. manual   - the manual map CSV (data/manual/building_map.csv) names the building;
2. single   - the estate has one building;
3. exact    - a building's block name is a word of the listing text; when several estates
              or phases share the block name, the one whose estate/phase words appear wins;
4. contains - the block name appears inside the text (no spaces between words);
5. fuzzy    - the whole text is close (rapidfuzz ratio >= 85) to exactly one building's
              "estate phase block" and clearly closer than to any other.
Otherwise the listing stays unmatched ('ambiguous' when several buildings fit equally).
Two guards run first: text that continues the estate name with another Chinese character
(逸瓏灣 against 逸瓏) is no match, and a 'single' or 'contains' hit whose text names a block
the estate lacks (豐盛苑 榮盛閣) returns (None, 'check_match') for the manual check.

Listings without a flat get a floor band (low / mid / high, thirds of the building's height)
so they can be valued per band (decisions.md, "Matching").
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from hkestate.config import get_data_dir
from hkestate.normalise import norm_name, parse_floor

_VARIANTS = str.maketrans({"臺": "台", "廬": "盧", "峯": "峰", "邨": "村", "（": "(", "）": ")"})
FUZZY_MIN = 85
FUZZY_MARGIN = 8
DUP_AREA_TOL = 0.02
SOURCE_ORDER = {"centanet": 0, "hse28": 1, "spacious": 2}


@dataclass(frozen=True)
class Building:
    building_id: str
    estate: str
    phase: str
    block: str
    top_floor: int | None = None


def norm_key(text: str | None) -> str:
    t = norm_name(text).translate(_VARIANTS).casefold()
    t = re.sub(r"[()\[\]\-_/|,，、]+", " ", t)
    t = re.sub(r"[街道路徑](?=\d)", "街", t)
    t = re.sub(r"(?<![\w])第(?=[0-9a-z])", "", t)  # 第1座 -> 1座
    t = re.sub(r"\s+", " ", t).strip()
    return " ".join(w[1:] if re.fullmatch(r"第[0-9a-z]+座", w) else w for w in t.split())


_RANGE_IN_TEXT = re.compile(r"([^\s\d()\-]*?)(\d+)-(\d+)(號)")


def _tokens(text: str) -> tuple[set[str], set[str]]:
    """Words of the text, plus each street number of an address range ('百老匯街19-21號'
    gives 百老匯街19號, 百老匯街20號, 百老匯街21號). Returns (tokens, range tokens)."""
    toks = set(norm_key(text).split())
    base = re.sub(r"[街道路徑](?=\d)", "街", norm_name(text).translate(_VARIANTS).casefold())
    ranged: set[str] = set()
    for m in _RANGE_IN_TEXT.finditer(base):
        lo, hi = int(m.group(2)), int(m.group(3))
        if 0 < hi - lo < 20:
            ranged |= {f"{m.group(1)}{n}{m.group(4)}" for n in range(lo, hi + 1)}
    # '2座 (2A)': tower 2, wing A, which 28Hse calls 第2A座
    wings = {f"{m.group(2)}座" for m in re.finditer(r"(\d+)座\s*\((\1[a-z])\)", base)}
    return toks | ranged | wings, ranged


def _compact(text: str) -> str:
    return norm_key(text).replace(" ", "")


def _context_score(b: Building, tokens: set[str], compact: str) -> int:
    words = set(norm_key(f"{b.estate} {b.phase}").split())
    return sum(1 for w in words if w in tokens or (len(w) >= 2 and w in compact))


def _pick(hits: list[Building], tokens: set[str], compact: str) -> list[Building]:
    if len(hits) <= 1:
        return hits
    scores = {b.building_id: _context_score(b, tokens, compact) for b in hits}
    best = max(scores.values())
    return [b for b in hits if scores[b.building_id] == best] if best > 0 else hits


_CJK = re.compile(r"[一-鿿]")


def _continues_estate_name(text: str, buildings: list[Building]) -> bool:
    """True when the text names a longer estate than ours: the estate name is followed by
    another Chinese character that starts neither a phase nor a block name (逸瓏灣 against
    estate 逸瓏; Centanet's unspaced 太古城海天花園... is fine because 海天花園 is a phase).
    A digit, letter or space after the name is a block code and does not count."""
    estates = {_compact(b.estate) for b in buildings if len(_compact(b.estate)) >= 2}
    follow = {_compact(b.phase) for b in buildings} | {_compact(b.block) for b in buildings}
    follow.discard("")
    found = False
    spaced = norm_key(text)
    for name in estates:
        for m in re.finditer(re.escape(name), spaced):
            found = True
            rest = spaced[m.end():]
            if not _CJK.match(rest) or any(rest.replace(" ", "").startswith(f) for f in follow):
                return False
    return found


def _block_in_text(block: str, compact: str) -> bool:
    """The block name occurs in the text; a block starting with a digit does not count when
    it is only the tail of a longer number ('5座' inside '15座')."""
    for m in re.finditer(re.escape(block), compact):
        if block[0].isdigit() and m.start() and compact[m.start() - 1].isdigit():
            continue
        return True
    return False


def _unknown_block(text: str, buildings: list[Building]) -> bool:
    """True when a word of the text names a block (ends in 閣 or 座) that none of the
    estate's buildings, phases or the estate itself carries: '豐盛苑 榮盛閣' against an
    estate with the single block 豐盛苑. A wing code after the block ('慧雅閣B座') is fine."""
    known = {c for b in buildings for c in (_compact(b.block), _compact(b.phase),
                                           _compact(b.estate)) if c}
    for word in norm_key(text).split():
        if not word.endswith(("閣", "座")) or not _CJK.search(word[:-1]):
            continue  # a number or wing code ('63座', 'a座') is not a block name
        if not any(k in word or word in k for k in known):
            return True
    return False


def match_building(
    text: str | None,
    buildings: list[Building],
    *,
    source: str | None = None,
    manual: dict | None = None,
) -> tuple[str | None, str]:
    """(building_id, method); building_id is None when no single building fits."""
    if manual:
        key = norm_key(text)
        hit = manual.get((source, key)) or manual.get(("*", key))
        if hit:
            return hit, "manual"
    if len(buildings) == 1:
        if text and _continues_estate_name(text, buildings):
            return None, "none"
        if text and _unknown_block(text, buildings):
            return None, "check_match"
        return buildings[0].building_id, "single"
    if not text or not buildings:
        return None, "none"
    tokens, ranged = _tokens(text)
    compact = _compact(text)
    if _continues_estate_name(text, buildings):
        return None, "none"

    exact = [b for b in buildings if norm_key(b.block) and set(norm_key(b.block).split()) <= tokens]
    exact = _pick(exact, tokens, compact)
    if len(exact) == 1:
        return exact[0].building_id, "exact"
    if exact and ranged and all(norm_key(b.block) in ranged for b in exact):
        # One physical block with two street numbers (Mei Foo's 百老匯街19-21號), which
        # 28Hse lists as one building per number: the lowest number stands for the block.
        first = min(exact, key=lambda b: int(re.search(r"(\d+)號", b.block).group(1)))
        return first.building_id, "range"
    if exact:
        return None, "ambiguous"

    contains = [
        b for b in buildings
        if len(_compact(b.block)) >= 2 and _block_in_text(_compact(b.block), compact)
    ]
    # prefer the longest block name ('21座' over '1座')
    if contains:
        longest = max(len(_compact(b.block)) for b in contains)
        contains = [b for b in contains if len(_compact(b.block)) == longest]
    contains = _pick(contains, tokens, compact)
    if len(contains) == 1:
        if _unknown_block(text, buildings):
            return None, "check_match"
        return contains[0].building_id, "contains"
    if contains:
        return None, "ambiguous"

    scored = sorted(
        ((fuzz.ratio(compact, _compact(f"{b.estate} {b.phase} {b.block}")), b) for b in buildings),
        key=lambda x: -x[0],
    )
    if scored and scored[0][0] >= FUZZY_MIN and (
        len(scored) == 1 or scored[0][0] - scored[1][0] >= FUZZY_MARGIN
    ):
        return scored[0][1].building_id, "fuzzy"
    return None, "none"


# ---- floor bands ------------------------------------------------------------------------------

_BAND_WORDS = {"高層": "high", "高": "high", "中層": "mid", "中": "mid", "低層": "low", "低": "low"}


def _band_of(num: float, top: int) -> str:
    frac = num / top
    if frac <= 1 / 3:
        return "low"
    if frac > 2 / 3:
        return "high"
    return "mid"


def floor_band(
    floor_text: str | None, top_floor: int | None, *, floor_range: str | None = None
) -> str:
    """low / mid / high / unknown, from a band word, a floor number, or 28Hse's 'a-b/top'."""
    t = (floor_text or "").strip()
    if t in _BAND_WORDS:
        return _BAND_WORDS[t]
    if t:
        fl = parse_floor(t)
        if fl.kind in ("ground", "podium", "basement"):
            return "low"
        if fl.kind == "top":
            return "high"
        if fl.num is not None and top_floor:
            return _band_of(fl.num, top_floor)
    m = re.fullmatch(r"(\d+)-(\d+)/(\d+)", floor_range or "")
    if m:
        lo, hi, top = (int(x) for x in m.groups())
        if top:
            return _band_of((lo + hi) / 2, top)
    return "unknown"


# ---- cross-site duplicates --------------------------------------------------------------------


def dedupe(rows: list[dict]) -> dict[tuple[str, str], str]:
    """Group the same flat listed on different sites: same building, kind and floor band,
    saleable area within 2%. A listing without a band (Spacious) joins a group only when
    exactly one fits. Two listings from the same site are never merged. Returns
    (source, listing_id) -> group key. Groups are bucketed by (building, kind), so a
    territory-sized list stays fast."""
    buckets: dict[tuple, list[dict]] = {}
    out: dict[tuple[str, str], str] = {}

    def fits(g, r, any_band: bool) -> bool:
        if r["source"] in g["sources"]:
            return False
        if not any_band and g["band"] != r["floor_band"]:
            return False
        a, b = g["area"], r.get("saleable_sqft")
        return bool(a and b) and abs(a - b) <= DUP_AREA_TOL * a

    known = [r for r in rows if r.get("floor_band") not in (None, "unknown")]
    unknown = [r for r in rows if r.get("floor_band") in (None, "unknown")]
    unknown_ids = {id(r) for r in unknown}
    order = lambda r: (SOURCE_ORDER.get(r["source"], 9), str(r["listing_id"]))  # noqa: E731
    for r in sorted(known, key=order) + sorted(unknown, key=order):
        key = (r["source"], str(r["listing_id"]))
        bucket_key = (r.get("building_id"), r.get("kind"))
        bucket = buckets.setdefault(bucket_key, [])
        cands = []
        if r.get("building_id"):
            cands = [g for g in bucket if fits(g, r, id(r) in unknown_ids)]
        if id(r) not in unknown_ids and len(cands) > 1:
            cands = [min(cands, key=lambda g: abs(g["area"] - r["saleable_sqft"]))]
        if len(cands) == 1:
            g = cands[0]
            g["sources"].add(r["source"])
        else:
            g = {"key": f"{key[0]}:{key[1]}", "building_id": r.get("building_id"),
                 "kind": r.get("kind"), "band": r.get("floor_band"),
                 "area": r.get("saleable_sqft"), "sources": {r["source"]}}
            bucket.append(g)
        out[key] = g["key"]
    return out

# ---- data access ------------------------------------------------------------------------------


def manual_map_path() -> Path:
    return get_data_dir() / "manual" / "building_map.csv"


def load_manual_map(path: Path | None = None) -> dict[tuple[str, str], str]:
    """Rows of `source,building_text,building_id` (source '*' = any site)."""
    path = path or manual_map_path()
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("building_id") and row.get("building_text"):
                out[((row.get("source") or "*").strip(), norm_key(row["building_text"]))] = (
                    row["building_id"].strip()
                )
    return out


def load_buildings(con, group: str) -> list[Building]:
    rows = con.execute(
        """
        SELECT b.building_id, e.name_zh, coalesce(p.name_zh, ''), b.name_zh, max(u.floor_num)
        FROM building b JOIN estate e USING (estate_id)
        LEFT JOIN phase p USING (phase_id)
        LEFT JOIN unit u USING (building_id)
        WHERE e.group_name = ?
        GROUP BY ALL
        """,
        [group],
    ).fetchall()
    return [Building(r[0], r[1], r[2], r[3], r[4]) for r in rows]


# ---- territory matching ------------------------------------------------------------------------


class TerritoryMatcher:
    """Match listings outside the pilot estates to master (Centadata) buildings.

    1. A Centanet detail page names the Centadata block it belongs to (`1-` code): that is
       the building (`cd-<code>`), no guessing.
    2. Otherwise the estate is found by name in the master list (the longest leading part of
       the text that is an estate or the whole name of its phases), and the text is matched
       to that estate's buildings with the same rules as the pilot (`match_building`).
    Estates that are excluded from the study (subsidised, villages) are not matched."""

    def __init__(self, con):
        from hkestate.link import est_key

        self._est_key = est_key
        self.con = con
        self.masters = sorted(
            ({"code": c, "parent_code": p, "key": est_key(n)}
             for c, p, n in con.execute(
                "SELECT code, parent_code, name_zh FROM master_estate "
                "WHERE NOT coalesce(pilot, false) AND NOT coalesce(excluded, false)").fetchall()
             if est_key(n)),
            key=lambda m: m["key"])
        self.keys = [m["key"] for m in self.masters]
        self.block_ids = {r[0] for r in con.execute(
            "SELECT building_id FROM master_building WHERE building_id LIKE 'cd-%'").fetchall()}
        self._buildings: dict[str, list[Building]] = {}

    def _estate_hits(self, text: str) -> list[dict]:
        from hkestate.link import match_estate

        key = self._est_key(text)
        for n in range(len(key), 1, -1):
            status, hits = match_estate(key[:n], self.masters, self.keys)
            if hits:
                return hits
            if status == "ambiguous":
                return []
        return []

    def _buildings_of(self, codes: list[str]) -> list[Building]:
        out: list[Building] = []
        for code in codes:
            if code not in self._buildings:
                self._buildings[code] = [
                    Building(r[0], r[1] or "", r[2] or "", r[3] or "", r[4])
                    for r in self.con.execute(
                        "SELECT building_id, estate_name, phase_name, block_name, floors "
                        "FROM master_building WHERE estate_code = ?", [code]).fetchall()]
            out += self._buildings[code]
        return out

    def top_floor(self, building_id: str | None) -> int | None:
        if building_id is None:
            return None
        row = self.con.execute("SELECT floors FROM master_building WHERE building_id = ?",
                               [building_id]).fetchone()
        return row[0] if row else None

    def match(self, text: str | None, *, block_key: str | None = None,
              estate: str | None = None, source: str | None = None,
              manual: dict | None = None, url: str | None = None) -> tuple[str | None, str]:
        bid, how = self._match(text, block_key=block_key, estate=estate, source=source,
                               manual=manual)
        if bid is None and source == "centanet" and url:
            # The page title often names only the estate ('尚悅'); the link names the block
            # ('尚悅-3座'). About 2,900 active sale listings, 2026-10-23.
            from hkestate.sources.centanet import slug_text

            alt = slug_text(url)
            if alt and norm_key(alt) != norm_key(text):
                bid2, how2 = self._match(alt, estate=estate, source=source)
                if bid2 is not None:
                    return bid2, f"link_{how2}"
        return bid, how

    def _match(self, text: str | None, *, block_key: str | None = None,
               estate: str | None = None, source: str | None = None,
               manual: dict | None = None) -> tuple[str | None, str]:
        if block_key and f"cd-{block_key}" in self.block_ids:
            return f"cd-{block_key}", "block_code"
        if manual and text:
            hit = manual.get((source, norm_key(text))) or manual.get(("*", norm_key(text)))
            if hit:
                return hit, "manual"
        hits = self._estate_hits(estate) if estate else []
        if not hits and text:
            hits = self._estate_hits(text)
        if not hits:
            return None, "none"
        return match_building(text, self._buildings_of([h["code"] for h in hits]),
                              source=source)
