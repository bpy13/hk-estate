"""The shape every listing parser returns, whatever the site.

Agent names, phone numbers, licence numbers and agency names are never read into these
fields. `text_for_flags` holds the listing's free text (titles, blurbs) only so the keyword
rules in flags.py can read it; it may contain names or phones and is never stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Listing:
    source: str  # centanet, hse28, spacious
    listing_id: str  # the site's own id
    kind: str  # sale or rent
    url: str
    estate_name: str | None = None
    building_text: str | None = None  # phase / block text as the site writes it
    floor_text: str | None = None  # 高層 / 中層 / 低層 / 25樓; None if not shown
    floor_range: str | None = None  # 28Hse detail pages: '20-30/30' (band floors / top floor)
    flat: str | None = None
    price_hkd: int | None = None  # sale price, or monthly rent for kind='rent'
    saleable_sqft: float | None = None
    gross_sqft: float | None = None
    rooms: int | None = None  # 0 = open plan (開放式)
    facing: str | None = None
    age_years: int | None = None
    posted_date: date | None = None
    updated_date: date | None = None
    features: list[str] = field(default_factory=list)
    source_estate_key: str | None = None  # e.g. 28Hse estate id
    source_building_key: str | None = None  # e.g. Centadata block code, Spacious building id
    extra: dict = field(default_factory=dict)  # other parsed facts (management fee, ...)
    text_for_flags: str = field(default="", repr=False)  # never stored
