"""Risk and feature flags from listing text, by keyword rules (no AI; decisions.md).

Rules live in keywords.yml next to this file: per flag, `terms` that set it and `negations`
that cancel a mention ("非凶宅" is not a haunted flat, "開放式廚房" is not a studio). A flag
is set when at least one mention of a term lies outside every negation phrase.

Text comes from the listing's title, blurbs and feature tags (Listing.text_for_flags and
.features). Building and estate names are removed first, so 太古城's 海景花園 block names
don't read as "sea view". Only the flag and the matched term are stored, never the text.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

from hkestate.listing_types import Listing
from hkestate.normalise import norm_name

RULES_PATH = Path(__file__).with_name("keywords.yml")


@lru_cache(maxsize=4)
def load_rules(path: str | None = None) -> dict[str, dict[str, list[str]]]:
    data = yaml.safe_load(Path(path or RULES_PATH).read_text(encoding="utf-8")) or {}
    return {
        flag: {
            "terms": [str(t) for t in (spec or {}).get("terms") or []],
            "negations": [str(t) for t in (spec or {}).get("negations") or []],
        }
        for flag, spec in data.items()
    }


def _prep(text: str) -> str:
    return norm_name(unicodedata.normalize("NFKC", text or "")).casefold()


def _spans(text: str, needle: str) -> list[tuple[int, int]]:
    out, start, n = [], 0, _prep(needle)
    if not n:
        return out
    while (i := text.find(n, start)) >= 0:
        out.append((i, i + len(n)))
        start = i + 1
    return out


def find_flags(text: str, rules: dict | None = None) -> dict[str, str]:
    """{flag: matched term} for every flag with an un-negated mention in `text`."""
    rules = rules or load_rules()
    t = _prep(text)
    found: dict[str, str] = {}
    for flag, spec in rules.items():
        neg = [s for phrase in spec["negations"] for s in _spans(t, phrase)]
        for term in sorted(spec["terms"], key=len, reverse=True):
            free = [
                (s, e) for s, e in _spans(t, term)
                if not any(ns <= s and e <= ne for ns, ne in neg)
            ]
            if free:
                found[flag] = term
                break
    return found


def listing_flags(lst: Listing) -> dict[str, str]:
    text = " ".join([lst.text_for_flags or "", *lst.features])
    for name in (lst.building_text, lst.estate_name):
        if name:
            text = text.replace(name, " ")
    found = find_flags(text)
    if lst.rooms == 0 and "studio" not in found:
        found["studio"] = "rooms=0"
    return found


def apply_flags(con, items: list[Listing], now: datetime) -> int:
    """Replace the stored flags of each listing read in this run. Returns rows written."""
    n = 0
    for lst in items:
        con.execute(
            "DELETE FROM risk_flag WHERE source = ? AND listing_id = ?",
            [lst.source, lst.listing_id],
        )
        rows = [[lst.source, lst.listing_id, flag, ev, now]
                for flag, ev in sorted(listing_flags(lst).items())]
        if rows:
            con.executemany(
                "INSERT INTO risk_flag (source, listing_id, flag, evidence, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            n += len(rows)
    return n
