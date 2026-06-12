"""StreetEasy fetching and parsing.

Uses Playwright with stealth patches to render search pages, then parses listings
out of the HTML. StreetEasy is protected by PerimeterX, so we (a) randomize the
fingerprint/timing and (b) detect block pages and surface them so the caller can
back off instead of hammering a blocked endpoint.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

try:  # Imported lazily-friendly: the module still imports without the browser.
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import Page, Browser, BrowserContext
except Exception:  # pragma: no cover - playwright not installed yet
    sync_playwright = None  # type: ignore
    Page = Browser = BrowserContext = Any  # type: ignore

try:
    from playwright_stealth import stealth_sync
except Exception:  # pragma: no cover
    stealth_sync = None

from models import Listing

log = logging.getLogger("streeteasy.scraper")

BASE = "https://streeteasy.com"

# A realistic, current desktop Chrome UA. Update occasionally to stay plausible.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Phrases that indicate a genuine PerimeterX / access-denied interstitial.
# NOTE: deliberately specific. StreetEasy loads the PerimeterX *sensor* script on
# every normal page, so broad markers like "perimeterx" cause false positives.
_BLOCK_MARKERS = (
    "px-captcha",
    "press & hold",
    "press and hold",
    "access to this page has been denied",
    "verify you are a human",
    "are you a robot",
)

_PRICE_RE = re.compile(r"\$\s*([\d,]{3,})")
_BEDS_RE = re.compile(r"([\d.]+)\s*bed", re.IGNORECASE)
_STUDIO_RE = re.compile(r"\bstudio\b", re.IGNORECASE)
_BATHS_RE = re.compile(r"([\d.]+)\s*bath", re.IGNORECASE)
# Most reliable: the embedded page JSON carries "yearBuilt":2021.
_YEAR_BUILT_JSON_RE = re.compile(r'"yearBuilt"\s*:\s*((?:1[89]|20)\d{2})')
# Visible-text fallbacks. The "About the building" section renders the year
# *before* the word "built" (e.g. "2021 built"); also handle "built in 2021"
# and "year built: 2021".
_YEAR_BUILT_RE = re.compile(
    r"(?:(?:built in|year built\s*[:\-]?\s*)\s*((?:1[89]|20)\d{2})"
    r"|((?:1[89]|20)\d{2})\s*built)",
    re.IGNORECASE,
)
# Detail-page description mentions of a refurbishment/renovation.
_REFURBISHED_RE = re.compile(
    r"\b(refurbished|refurbishment|renovated|renovation|gut[- ]?renovat\w*"
    r"|restored|remodel\w*|modernized|rebuilt|redone)\b",
    re.IGNORECASE,
)
# Matches detail URLs like /rental/123456 or /building/slug/unit
_LISTING_HREF_RE = re.compile(r"^/(?:rental|building)/[^?#]+", re.IGNORECASE)


class BlockedError(RuntimeError):
    """Raised when StreetEasy serves a bot-challenge / access-denied page."""


def is_blocked_html(html: str) -> bool:
    """True if the HTML is a PerimeterX challenge / access-denied page."""
    lowered = html.lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


# --------------------------------------------------------------------------- #
# URL building
# --------------------------------------------------------------------------- #
def build_search_urls(config) -> list[str]:
    """Return the list of search URLs to poll.

    Prefers explicitly pasted URLs (``search.urls``); otherwise assembles one
    from the ``search.build`` components.
    """
    search = config.search
    urls = [u for u in (search.get("urls") or []) if u]
    if urls:
        return urls

    build = search.get("build", {}) or {}
    parts: list[str] = []

    min_price = build.get("min_price") or 0
    max_price = build.get("max_price")
    if max_price:
        parts.append(f"price:{min_price or ''}-{max_price}")

    beds = build.get("beds")
    if beds is not None:
        parts.append(f"beds:{beds}")

    min_baths = build.get("min_baths")
    if min_baths:
        parts.append(f"baths>={min_baths}")

    if build.get("no_fee"):
        parts.append("no_fee:1")

    area_ids = build.get("area_ids") or []
    if area_ids:
        parts.append("area:" + ",".join(str(a) for a in area_ids))

    path = "/for-rent/nyc/"
    if parts:
        path += "%7C".join(parts)

    url = urljoin(BASE, path)
    sort_by = build.get("sort_by")
    if sort_by:
        url += f"?sort_by={sort_by}"

    if not area_ids:
        log.warning(
            "No search.urls and no area_ids configured; falling back to a "
            "city-wide search. Paste your StreetEasy search URL into config for "
            "proper neighborhood filtering."
        )
    return [url]


# --------------------------------------------------------------------------- #
# Browser session
# --------------------------------------------------------------------------- #
class StreetEasyScraper:
    """Manages a stealthy Playwright session for fetching StreetEasy pages.

    Uses a *persistent* browser context so the PerimeterX clearance cookie sticks
    across requests, and (by default) runs headful, which evades bot detection far
    better than headless. On a headless server, set headless=True and run under
    a virtual display (e.g. ``xvfb-run``).
    """

    def __init__(
        self,
        headless: bool = False,
        detail_delay_seconds: float = 4.0,
        user_data_dir: str | os.PathLike[str] = ".browser_profile",
        warmup: bool = True,
        debug_dir: str | os.PathLike[str] | None = "debug",
        solve_interactively: bool = True,
        solve_timeout_seconds: float = 180.0,
        channel: str | None = "chrome",
        cdp_url: str | None = None,
    ):
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install -r requirements.txt "
                "&& playwright install chromium"
            )
        self.headless = headless
        self.detail_delay_seconds = detail_delay_seconds
        self.user_data_dir = str(user_data_dir)
        self.warmup = warmup
        self.channel = channel or None
        # When set, connect to an already-running Chrome (remote debugging) instead
        # of launching one. This rides on the user's real, human session.
        self.cdp_url = cdp_url or None
        self.debug_dir = Path(debug_dir) if debug_dir else None
        self.solve_interactively = solve_interactively
        self.solve_timeout_seconds = solve_timeout_seconds
        self._pw = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self._connected = False
        self._warmed_up = False

    def __enter__(self) -> "StreetEasyScraper":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        self._pw = sync_playwright().start()

        if self.cdp_url:
            log.info("Connecting to your Chrome over CDP at %s ...", self.cdp_url)
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
            # Reuse the existing context so we inherit your cookies / human session.
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else self._browser.new_context()
            self._connected = True
            log.info("Connected to existing Chrome session.")
            return

        # Persistent context keeps cookies/localStorage between runs so we don't
        # have to clear a PerimeterX challenge every single cycle.
        kwargs = dict(
            headless=self.headless,
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1366, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        # Driving the real, installed Google Chrome (channel="chrome") renders the
        # PerimeterX challenge correctly and looks far less like a bot than the
        # bundled Chromium. Fall back to Chromium if Chrome isn't installed.
        if self.channel:
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    self.user_data_dir, channel=self.channel, **kwargs
                )
                return
            except Exception as exc:
                log.warning(
                    "Could not launch channel=%r (%s); falling back to bundled Chromium.",
                    self.channel,
                    exc,
                )
        self._context = self._pw.chromium.launch_persistent_context(
            self.user_data_dir, **kwargs
        )

    def stop(self) -> None:
        # When connected to the user's own Chrome, never close their context/browser
        # (that would kill their tabs); just disconnect Playwright.
        if not self._connected:
            try:
                if self._context is not None:
                    self._context.close()
            except Exception:  # pragma: no cover
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        self._context = self._browser = self._pw = None

    def _new_page(self) -> Page:
        assert self._context is not None, "Scraper not started"
        page = self._context.new_page()
        # Don't tamper with the user's real Chrome session via stealth patches.
        if stealth_sync is not None and not self._connected:
            try:
                stealth_sync(page)
            except Exception:  # pragma: no cover
                log.debug("stealth_sync failed; continuing without it")
        return page

    def _warm_up(self) -> None:
        """Visit the homepage once so the session looks organic and picks up cookies."""
        if self._warmed_up or not self.warmup or self._connected:
            return
        self._warmed_up = True
        page = self._new_page()
        try:
            log.info("Warming up session via homepage...")
            page.goto(BASE, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(random.randint(2500, 4500))
            self._humanize(page)
        except Exception as exc:  # pragma: no cover
            log.debug("Warm-up navigation failed: %s", exc)
        finally:
            page.close()

    @staticmethod
    def _humanize(page: Page) -> None:
        """Small mouse/scroll movements so the PerimeterX sensor sees human behavior."""
        try:
            page.mouse.move(random.randint(100, 800), random.randint(100, 500))
            page.wait_for_timeout(random.randint(300, 900))
            page.mouse.wheel(0, random.randint(400, 1200))
            page.wait_for_timeout(random.randint(400, 1100))
        except Exception:  # pragma: no cover
            pass

    def _dump_block_debug(self, url: str, html: str, page: Optional[Page]) -> None:
        if not self.debug_dir:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (self.debug_dir / f"block-{stamp}.html").write_text(html, encoding="utf-8")
            if page is not None:
                page.screenshot(path=str(self.debug_dir / f"block-{stamp}.png"), full_page=True)
            log.warning("Saved block debug artifacts to %s (block-%s.*)", self.debug_dir, stamp)
        except Exception as exc:  # pragma: no cover
            log.debug("Could not save block debug: %s", exc)

    @staticmethod
    def _is_block_html(html: str) -> bool:
        lowered = html.lower()
        return any(marker in lowered for marker in _BLOCK_MARKERS)

    def _await_manual_solve(self, page: Page, url: str, html: str) -> str:
        """Wait for the human to solve a Press & Hold challenge in the window.

        Polls the page until the block markers disappear (challenge cleared) or a
        timeout elapses. Only used in headful mode; headless can't be solved by a
        human so it fails fast.
        """
        if self.headless or not self.solve_interactively:
            self._dump_block_debug(url, html, page)
            raise BlockedError(f"Bot challenge detected at {url}")

        log.warning(
            "PerimeterX challenge detected. >>> PLEASE SOLVE the 'Press & Hold' "
            "prompt in the browser window now. <<< Waiting up to %.0fs...",
            self.solve_timeout_seconds,
        )
        deadline = time.time() + self.solve_timeout_seconds
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            try:
                current = page.content()
            except Exception:
                continue
            if not self._is_block_html(current):
                log.info("Challenge cleared - clearance cookie saved to profile.")
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                return page.content()

        final_html = ""
        try:
            final_html = page.content()
        except Exception:
            pass
        self._dump_block_debug(url, final_html or html, page)
        raise BlockedError(
            f"Challenge not solved within {self.solve_timeout_seconds:.0f}s at {url}"
        )

    def _get_html(self, url: str) -> str:
        self._warm_up()
        page = self._new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.bring_to_front()
            except Exception:
                pass
            self._humanize(page)
            # Give client-side rendering / PX challenge time to resolve.
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(random.randint(1200, 2600))
            html = page.content()

            if self._is_block_html(html):
                html = self._await_manual_solve(page, url, html)
            return html
        finally:
            page.close()

    # ----------------------------------------------------------------------- #
    # Public fetch methods
    # ----------------------------------------------------------------------- #
    def fetch_search(self, url: str) -> list[Listing]:
        log.info("Fetching search: %s", url)
        html = self._get_html(url)
        listings = parse_search_html(html, base_url=url)
        log.info("Parsed %d listing(s) from search page", len(listings))
        return listings

    def fetch_html(self, url: str) -> str:
        """Public helper to fetch a page's rendered HTML (used by --dump-html)."""
        return self._get_html(url)

    def enrich_from_detail(self, listing: Listing) -> None:
        """Fetch the listing's detail page and fill in year built / available date.

        Updates ``listing`` in place. On any failure the relevant fields stay
        ``None`` and an unverified reason is recorded.
        """
        try:
            html = self._get_html(listing.url)
        except BlockedError:
            raise
        except Exception as exc:  # network/timeout/etc.
            log.warning("Detail fetch failed for %s: %s", listing.url, exc)
            listing.unverified_reasons.append("detail page unavailable")
            return

        parse_detail_html(html, listing)
        time.sleep(self.detail_delay_seconds)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _listing_id_from_href(href: str) -> Optional[str]:
    """Derive a stable dedup id from a listing href path."""
    match = _LISTING_HREF_RE.match(href)
    if not match:
        return None
    path = match.group(0).rstrip("/")
    # /rental/123456 -> "rental:123456"; building paths keep their slug+unit.
    return path.lower().lstrip("/").replace("/", ":")


def _parse_int_price(text: str) -> Optional[int]:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_beds(text: str) -> Optional[float]:
    if _STUDIO_RE.search(text):
        return 0.0
    m = _BEDS_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_baths(text: str) -> Optional[float]:
    m = _BATHS_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


# JSON-LD @type values that represent a rental listing.
_LISTING_LD_TYPES = {
    "Apartment",
    "SingleFamilyResidence",
    "Residence",
    "House",
    "ApartmentComplex",
}
_INRECT_RE = re.compile(
    r"in_rect:(-?\d+\.?\d*),(-?\d+\.?\d*),(-?\d+\.?\d*),(-?\d+\.?\d*)"
)


def _rect_from_url(url: str) -> Optional[tuple[float, float, float, float]]:
    """Parse the StreetEasy in_rect:lat1,lat2,lon1,lon2 map bound from a URL."""
    m = _INRECT_RE.search(url)
    if not m:
        return None
    try:
        return tuple(float(x) for x in m.groups())  # type: ignore[return-value]
    except ValueError:
        return None


def _within_rect(lat: float, lon: float, rect: tuple[float, float, float, float]) -> bool:
    lat1, lat2, lon1, lon2 = rect
    return (min(lat1, lat2) <= lat <= max(lat1, lat2)) and (
        min(lon1, lon2) <= lon <= max(lon1, lon2)
    )


def _iter_ld_graph(soup: BeautifulSoup) -> "Iterable[dict]":
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict):
            graph = data.get("@graph", [data])
        elif isinstance(data, list):
            graph = data
        else:
            continue
        for node in graph or []:
            if isinstance(node, dict):
                yield node


def _monthly_rent(additional: Any) -> Optional[int]:
    """Pull the integer monthly rent from a JSON-LD additionalProperty list."""
    for prop in additional or []:
        if str(prop.get("name", "")).strip().lower() in ("monthly rent", "rent"):
            return _parse_int_price(str(prop.get("value", "")))
    return None


def _parse_jsonld_listings(soup: BeautifulSoup, search_url: str) -> list[Listing]:
    """Extract listings from the page's JSON-LD (reliable, structured)."""
    rect = _rect_from_url(search_url)
    listings: dict[str, Listing] = {}
    dropped_out_of_area = 0

    for node in _iter_ld_graph(soup):
        if node.get("@type") not in _LISTING_LD_TYPES:
            continue
        url = (node.get("url") or node.get("@id") or "").split("?")[0]
        if not url:
            continue
        listing_id = _listing_id_from_href(urlparse(url).path)
        if not listing_id or listing_id in listings:
            continue

        # Drop injected/sponsored listings that fall outside the searched map area.
        geo = node.get("geo") or {}
        lat, lon = geo.get("latitude"), geo.get("longitude")
        if rect and lat is not None and lon is not None:
            try:
                if not _within_rect(float(lat), float(lon), rect):
                    dropped_out_of_area += 1
                    continue
            except (TypeError, ValueError):
                pass

        addr = node.get("address") or {}
        baths = node.get("numberOfBathroomsTotal")
        if baths is None:
            baths = node.get("numberOfFullBathrooms")

        listings[listing_id] = Listing(
            listing_id=listing_id,
            url=url,
            title=node.get("name") or url,
            price=_monthly_rent(node.get("additionalProperty")),
            beds=_as_float(node.get("numberOfBedrooms")),
            baths=_as_float(baths),
            neighborhood=str(addr.get("addressLocality") or ""),
        )

    if dropped_out_of_area:
        log.info("Dropped %d out-of-area (sponsored) listing(s).", dropped_out_of_area)
    return list(listings.values())


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_search_html(html: str, base_url: str) -> list[Listing]:
    """Extract listings from a StreetEasy search results page.

    Prefers the page's structured JSON-LD data (stable, includes price/beds/baths/
    neighborhood/geo). Falls back to scanning the DOM if JSON-LD is unavailable.
    """
    soup = BeautifulSoup(html, "lxml")
    listings = _parse_jsonld_listings(soup, base_url)
    if listings:
        return listings
    log.warning("No JSON-LD listings found; falling back to DOM scan.")
    return _parse_dom_listings(soup)


def _parse_dom_listings(soup: BeautifulSoup) -> list[Listing]:
    """Fallback parser: scan anchors and read price/beds/baths from card text."""
    listings: dict[str, Listing] = {}

    for anchor in soup.find_all("a", href=True):
        listing_id = _listing_id_from_href(anchor["href"])
        if not listing_id or listing_id in listings:
            continue

        url = urljoin(BASE, anchor["href"].split("?")[0])
        # Walk up to a container that holds the card's price text.
        container = anchor
        card_text = anchor.get_text(" ", strip=True)
        for _ in range(4):
            parent = container.parent
            if parent is None:
                break
            container = parent
            card_text = container.get_text(" ", strip=True)
            if "$" in card_text:
                break

        title = anchor.get_text(" ", strip=True) or url
        listings[listing_id] = Listing(
            listing_id=listing_id,
            url=url,
            title=title[:200],
            price=_parse_int_price(card_text),
            beds=_parse_beds(card_text),
            baths=_parse_baths(card_text),
        )

    return list(listings.values())


def _parse_available_date(text: str) -> Optional[date]:
    """Best-effort parse of a StreetEasy 'available' date from free text."""
    if re.search(r"available\s+(now|immediately)", text, re.IGNORECASE):
        return date.today()

    # Look for an explicit availability phrase first to avoid grabbing unrelated dates.
    window = text
    m = re.search(r"(?:available|move[- ]?in|date available)\D{0,15}", text, re.IGNORECASE)
    if m:
        window = text[m.start(): m.start() + 60]

    patterns = (
        ("%b %d, %Y", r"([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})"),
        ("%m/%d/%Y", r"(\d{1,2}/\d{1,2}/\d{4})"),
        ("%Y-%m-%d", r"(\d{4}-\d{2}-\d{2})"),
    )
    for fmt, pattern in patterns:
        found = re.search(pattern, window)
        if not found:
            continue
        raw = found.group(1).replace(".", "").replace(",", "")
        for try_fmt in (fmt, fmt.replace(", ", " ").replace(",", "")):
            try:
                return datetime.strptime(raw.strip(), try_fmt.replace(",", "")).date()
            except ValueError:
                continue
    return None


def _extract_year_built(html: str, text: str) -> int | None:
    """Pull the building's year built, preferring the embedded JSON.

    StreetEasy's detail page carries a reliable `"yearBuilt":<year>` field in the
    embedded page data. As a fallback we read the visible "About the building"
    section, which renders the year just before the word "built" (e.g. "2021 built").
    """
    json_match = _YEAR_BUILT_JSON_RE.search(html)
    if json_match:
        return int(json_match.group(1))
    text_match = _YEAR_BUILT_RE.search(text)
    if text_match:
        year = text_match.group(1) or text_match.group(2)
        if year:
            return int(year)
    return None


def parse_detail_html(html: str, listing: Listing) -> None:
    """Fill year_built, available_date, no_fee, and refine price/beds/baths."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    # Keep the page text for keyword filtering (e.g. excluding NYC Housing Connect).
    listing.description = text

    year_built = _extract_year_built(html, text)
    if year_built is not None:
        listing.year_built = year_built
    else:
        listing.unverified_reasons.append("year built unknown")

    available = _parse_available_date(text)
    if available:
        listing.available_date = available
    else:
        listing.unverified_reasons.append("move-in date unknown")

    if listing.price is None:
        listing.price = _parse_int_price(text)
    if listing.beds is None:
        listing.beds = _parse_beds(text)
    if listing.baths is None:
        listing.baths = _parse_baths(text)
    if listing.no_fee is None:
        listing.no_fee = bool(re.search(r"no\s*fee", text, re.IGNORECASE))

    listing.refurbished = bool(_REFURBISHED_RE.search(text))
