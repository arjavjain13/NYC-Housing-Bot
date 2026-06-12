"""Shared data structures for the StreetEasy bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# Buildings built before this year are considered "pre-war".
PRE_WAR_CUTOFF = 1947


@dataclass
class Listing:
    """A single rental listing parsed from StreetEasy.

    Fields that StreetEasy does not expose on the search results page are filled
    in later from the detail page, and may stay ``None`` if unavailable.
    """

    listing_id: str
    url: str
    title: str = ""
    price: Optional[int] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    neighborhood: str = ""
    no_fee: Optional[bool] = None
    year_built: Optional[int] = None
    available_date: Optional[date] = None
    # Whether the detail-page description mentions a refurbishment/renovation.
    # None when unknown (e.g. detail page not fetched).
    refurbished: Optional[bool] = None
    # Full visible text of the detail page (transient; used for keyword filtering).
    description: str = ""
    # Reasons a listing could not be fully verified (e.g. missing year built).
    # Surfaced in the notification so the user knows to double-check.
    unverified_reasons: list[str] = field(default_factory=list)

    @property
    def is_unverified(self) -> bool:
        return bool(self.unverified_reasons)

    @property
    def is_pre_war(self) -> bool:
        return self.year_built is not None and self.year_built < PRE_WAR_CUTOFF
