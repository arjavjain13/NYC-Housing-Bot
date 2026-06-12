"""HTTP fetch backend (no browser).

StreetEasy server-renders the search results (including the JSON-LD we parse) and
the detail pages, and a plain HTTP GET with realistic headers is currently not
challenged by PerimeterX -- even from datacenter IPs. This backend is therefore
the default: it's fast, dependency-light, runs under cron, and works on a free
cloud VM 24/7. If StreetEasy ever starts blocking plain requests, switch
``fetch_mode`` back to ``browser`` in config.yaml.

This class mirrors the public interface of scraper.StreetEasyScraper
(context manager + fetch_search / enrich_from_detail / fetch_html) so main.py can
use either backend interchangeably.
"""
from __future__ import annotations

import logging
import random
import time

import requests

from models import Listing
from scraper import (
    BlockedError,
    is_blocked_html,
    parse_detail_html,
    parse_search_html,
)

log = logging.getLogger("streeteasy.http")

# A small pool of realistic, current desktop user agents to rotate through.
_USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)


class HttpScraper:
    """Fetches StreetEasy pages over plain HTTP and parses them."""

    def __init__(
        self,
        detail_delay_seconds: float = 4.0,
        request_timeout: float = 30.0,
        **_ignored,
    ):
        self.detail_delay_seconds = detail_delay_seconds
        self.request_timeout = request_timeout
        self._session: requests.Session | None = None

    def __enter__(self) -> "HttpScraper":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "user-agent": random.choice(_USER_AGENTS),
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "accept-language": "en-US,en;q=0.9",
                "referer": "https://streeteasy.com/",
                "upgrade-insecure-requests": "1",
            }
        )

    def stop(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # pragma: no cover
                pass
        self._session = None

    def _get(self, url: str) -> str:
        assert self._session is not None, "Scraper not started"
        resp = self._session.get(url, timeout=self.request_timeout)
        html = resp.text
        if resp.status_code in (403, 429) or is_blocked_html(html):
            raise BlockedError(f"Blocked (HTTP {resp.status_code}) at {url}")
        resp.raise_for_status()
        return html

    def fetch_search(self, url: str) -> list[Listing]:
        log.info("Fetching search: %s", url)
        html = self._get(url)
        listings = parse_search_html(html, base_url=url)
        log.info("Parsed %d listing(s) from search page", len(listings))
        return listings

    def enrich_from_detail(self, listing: Listing) -> None:
        try:
            html = self._get(listing.url)
        except BlockedError:
            raise
        except Exception as exc:
            log.warning("Detail fetch failed for %s: %s", listing.url, exc)
            listing.unverified_reasons.append("detail page unavailable")
            return
        parse_detail_html(html, listing)
        # Jitter the delay a little to look less mechanical.
        time.sleep(self.detail_delay_seconds + random.uniform(0, 2))

    def fetch_html(self, url: str) -> str:
        return self._get(url)
