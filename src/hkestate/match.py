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
        return buildings[0].building_id, "single"
    if not text or not buildings:
        return None, "none"
    tokens, ranged = _tokens(text)
    compact = _compact(text)

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
        b for b in buildings if len(_compact(b.block)) >= 2 and _compact(b.block) in compact
    ]
    # prefer the longest block name ('21座' over '1座')
    if contains:
        longest = max(len(_compact(b.block)) for b in contains)
        contains = [b for b in contains if len(_compact(b.block)) == longest]
    contains = _pick(contains, tokens, compact)
    if len(contains) == 1:
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
    (source, listing_id) -> group key."""
    groups: list[dict] = []
    out: dict[tuple[str, str], str] = {}

    def fits(g, r, any_band: bool) -> bool:
        if g["building_id"] != r["building_id"] or g["kind"] != r["kind"]:
            return False
        if r["source"] in g["sources"]:
            return False
        if not any_band and g["band"] != r["floor_band"]:
            return False
        a, b = g["area"], r.get("saleable_sqft")
        return bool(a and b) and abs(a - b) <= DUP_AREA_TOL * a

    known = [r for r in rows if r.get("floor_band") not in (None, "unknown")]
    unknown = [r for r in rows if r.get("floor_band") in (None, "unknown")]
    order = lambda r: (SOURCE_ORDER.get(r["source"], 9), str(r["listing_id"]))  # noqa: E731
    for r in sorted(known, key=order) + sorted(unknown, key=order):
        key = (r["source"], str(r["listing_id"]))
        cands = []
        if r.get("building_id"):
            cands = [g for g in groups if fits(g, r, r in unknown)]
        if r in known and len(cands) > 1:
            cands = [min(cands, key=lambda g: abs(g["area"] - r["saleable_sqft"]))]
        if len(cands) == 1:
            g = cands[0]
            g["sources"].add(r["source"])
        else:
            g = {"key": f"{key[0]}:{key[1]}", "building_id": r.get("building_id"),
                 "kind": r.get("kind"), "band": r.get("floor_band"),
                 "area": r.get("saleable_sqft"), "sources": {r["source"]}}
            groups.append(g)
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
