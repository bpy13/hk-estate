"""Turn Chinese page text into comparable values: floor labels, names, prices, areas, rooms.

Floor labels are kept as text (the unit key uses them) and also mapped to a number:
G/F and podium = 0, basements negative, numbered floors as labelled. Hong Kong buildings
often skip unlucky labels (4, 13, 14, 24, ...), so label 15 can be the 12th storey;
`physical_floor` corrects for that given the labels a building skips.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

# Labels Hong Kong developers commonly leave out.
CONVENTIONAL_SKIPS = {4, 13, 14, 24, 34, 44, 54, 64, 74}

_GROUND = {"G", "GF", "地下", "地", "UG", "高地"}
_BASEMENT = {"LG": -1, "低地": -1, "B": -1, "B1": -1, "B2": -2}
_PODIUM = {"平台", "PODIUM", "P"}
_TOP = {"頂層", "頂樓", "PH", "天台", "頂"}
_BANDS = {"高層", "中層", "低層"}


@dataclass(frozen=True)
class FloorInfo:
    label: str  # cleaned text label, used in the unit key
    num: int | None  # number as labelled (G/F = 0), None if not a single floor
    kind: str  # number, ground, basement, podium, top, band, unknown


def parse_floor(text: str | None) -> FloorInfo:
    raw = unicodedata.normalize("NFKC", text or "").strip().upper()
    core = re.sub(r"(樓|/F|F)$", "", raw).strip()
    core = core.replace("/", "")
    if re.fullmatch(r"\d+", core):
        return FloorInfo(str(int(core)), int(core), "number")
    if core in _GROUND:
        return FloorInfo("G", 0, "ground")
    if core in _BASEMENT:
        return FloorInfo(core, _BASEMENT[core], "basement")
    if core in _PODIUM:
        return FloorInfo("平台", 0, "podium")
    if core in _TOP:
        return FloorInfo("頂層", None, "top")
    if core in _BANDS:
        return FloorInfo(core, None, "band")
    return FloorInfo(core, None, "unknown")


def infer_skipped(labels) -> set[int]:
    """Conventional unlucky labels missing below the highest numbered floor seen."""
    nums = {parse_floor(lab).num for lab in labels}
    nums = {x for x in nums if x is not None and x > 0}
    if not nums:
        return set()
    top = max(nums)
    return {s for s in CONVENTIONAL_SKIPS if s < top and s not in nums}


def physical_floor(num: int | None, skipped: set[int]) -> int | None:
    """Storey count for a labelled floor, given the labels the building skips."""
    if num is None or num <= 0:
        return num
    return num - sum(1 for s in skipped if s < num)


def resolve_top_floor(nums) -> int | None:
    """A 'top floor' (頂層) unit sits one above the highest numbered floor."""
    nums = [x for x in nums if x is not None]
    return max(nums) + 1 if nums else None


# ---- names ----------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _s2t():
    import opencc

    return opencc.OpenCC("s2hk")


def norm_name(text: str | None) -> str:
    """Traditional Chinese, half-width, single spaces. Use for comparing names across sources."""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return _s2t().convert(t)


# ---- prices, areas, rooms --------------------------------------------------------------------


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_price_hkd(text: str | None) -> int | None:
    """'$875 萬元' -> 8,750,000; '$1.2億' -> 120,000,000; '$8,750,000' -> 8,750,000."""
    t = unicodedata.normalize("NFKC", text or "").replace(" ", "")
    if not re.search(r"\d", t):
        return None
    total = 0.0
    m = re.search(r"([\d,.]+)億", t)
    if m:
        total += _num(m.group(1)) * 100_000_000
        t = t[m.end() :]
    m = re.search(r"([\d,.]+)萬", t)
    if m:
        total += _num(m.group(1)) * 10_000
    elif total == 0:
        m = re.search(r"([\d,.]+)", t)
        total = _num(m.group(1))
    return int(round(total))


def parse_area(text: str | None) -> tuple[str | None, float | None]:
    """'實530呎' -> ('saleable', 530); '建628呎' -> ('gross', 628); '580呎' -> (None, 580)."""
    t = unicodedata.normalize("NFKC", text or "")
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:呎|平方呎|sq)", t)
    if not m:
        return (None, None)
    value = _num(m.group(1))
    if "實" in t:
        return ("saleable", value)
    if "建" in t:
        return ("gross", value)
    return (None, value)


def resolve_area(
    saleable: float | None, gross: float | None, ratio: float | None
) -> tuple[float | None, str | None]:
    """Comparable area and its basis: saleable, converted (gross x building ratio) or gross_only."""
    if saleable:
        return (saleable, "saleable")
    if gross and ratio:
        return (gross * ratio, "converted")
    if gross:
        return (gross, "gross_only")
    return (None, None)


def saleable_to_gross(pairs) -> float | None:
    """A building's own saleable/gross ratio: the median over units that have both."""
    ratios = [s / g for s, g in pairs if s and g]
    return statistics.median(ratios) if ratios else None


def parse_rooms(text: str | None) -> int | None:
    t = unicodedata.normalize("NFKC", text or "")
    if "開放式" in t:
        return 0
    m = re.search(r"(\d+)\s*房", t)
    return int(m.group(1)) if m else None
