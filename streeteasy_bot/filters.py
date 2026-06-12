"""Decide whether a parsed listing should trigger a notification.

The search URL already filters at the source (price band, beds, baths, area,
pre-war, available-after). This module is defense-in-depth plus the rules that
StreetEasy's URL can't express precisely (exact move-in window, hard price cap),
and it implements the "notify even when data is missing" preference.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from models import Listing

log = logging.getLogger("streeteasy.filters")


def _parse_iso_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        log.warning("Bad date in config: %r (expected YYYY-MM-DD)", value)
        return None


def passes_filters(
    listing: Listing, filters_cfg: dict[str, Any], record_missing: bool = True
) -> tuple[bool, str]:
    """Return ``(should_notify, reason)`` for a listing.

    Known values that clearly violate a constraint always reject. Unknown values
    follow ``notify_on_missing_data``: when true (default) the listing still
    notifies, with the gaps recorded on ``listing.unverified_reasons``.

    ``record_missing=False`` performs only the hard rejections on known values and
    skips the missing-data accounting -- used as a cheap pre-filter on search-card
    data before deciding whether to open a (slow, block-prone) detail page.
    """
    # Keyword blocklist (e.g. "NYC Housing Connect" lottery listings). Only has an
    # effect once the detail page description has been fetched.
    keywords = filters_cfg.get("exclude_keywords") or []
    if keywords and listing.description:
        haystack = f"{listing.title}\n{listing.description}".lower()
        for kw in keywords:
            if str(kw).lower() in haystack:
                return False, f"excluded keyword: {kw}"

    max_price = filters_cfg.get("max_price")
    if max_price and listing.price is not None and listing.price > max_price:
        return False, f"price ${listing.price:,} over ${max_price:,} cap"

    want_beds = filters_cfg.get("beds")
    if want_beds is not None and listing.beds is not None and listing.beds != want_beds:
        return False, f"{listing.beds:g} beds != {want_beds}"

    min_baths = filters_cfg.get("min_baths")
    max_baths = filters_cfg.get("max_baths")
    if listing.baths is not None:
        if min_baths and listing.baths < min_baths:
            return False, f"{listing.baths:g} baths < {min_baths}"
        if max_baths and listing.baths > max_baths:
            return False, f"{listing.baths:g} baths > {max_baths}"

    exclude_pre_war = filters_cfg.get("exclude_pre_war", True)
    min_year = filters_cfg.get("min_year_built", 1947)
    if exclude_pre_war and listing.year_built is not None and listing.year_built < min_year:
        return False, f"pre-war (built {listing.year_built})"

    move_start = _parse_iso_date(filters_cfg.get("move_in_start"))
    move_end = _parse_iso_date(filters_cfg.get("move_in_end"))
    if listing.available_date is not None:
        if move_start and listing.available_date < move_start:
            return False, f"available {listing.available_date} before {move_start}"
        if move_end and listing.available_date > move_end:
            return False, f"available {listing.available_date} after {move_end}"

    if not record_missing:
        return True, "passes card pre-filter"

    # Record any data we could not confirm, so it can be flagged in the email.
    missing: list[str] = []
    if exclude_pre_war and listing.year_built is None:
        missing.append("year built unknown")
    if (move_start or move_end) and listing.available_date is None:
        missing.append("move-in date unknown")
    for reason in missing:
        if reason not in listing.unverified_reasons:
            listing.unverified_reasons.append(reason)

    notify_on_missing = filters_cfg.get("notify_on_missing_data", True)
    if listing.unverified_reasons and not notify_on_missing:
        return False, "missing data: " + ", ".join(listing.unverified_reasons)

    return True, "match"
