"""Entry point: poll StreetEasy, filter, and email new matches.

Usage:
    python main.py                 # run forever (default)
    python main.py --once          # run a single cycle and exit
    python main.py --test-email    # send a test email and exit
    python main.py --notify-existing  # also notify on listings already live now

On the very first run (empty database) the bot records everything currently
listed as a silent baseline so you don't get flooded; only listings that appear
*after* that are emailed. Use --notify-existing to override.
"""
from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time

from config import BASE_DIR, load_config
from filters import passes_filters
from notifier import EmailNotifier
from http_scraper import HttpScraper
from scraper import BlockedError, StreetEasyScraper, build_search_urls
from store import SeenStore

log = logging.getLogger("streeteasy")

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    log.info("Received signal %s; finishing current cycle then exiting.", signum)
    _stop = True


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def make_scraper(config):
    """Build the configured fetch backend (HTTP by default, browser as fallback)."""
    mode = str(config.raw.get("fetch_mode", "http")).lower()
    bcfg = config.browser
    if mode == "http":
        return HttpScraper(
            detail_delay_seconds=float(bcfg.get("detail_delay_seconds", 4)),
        )

    debug_dir_cfg = bcfg.get("debug_dir", "debug")
    return StreetEasyScraper(
        headless=bcfg.get("headless", False),
        detail_delay_seconds=float(bcfg.get("detail_delay_seconds", 4)),
        user_data_dir=BASE_DIR / bcfg.get("user_data_dir", ".browser_profile"),
        warmup=bcfg.get("warmup", True),
        debug_dir=(BASE_DIR / debug_dir_cfg) if debug_dir_cfg else None,
        solve_interactively=bcfg.get("solve_interactively", True),
        solve_timeout_seconds=float(bcfg.get("solve_timeout_seconds", 180)),
        channel=bcfg.get("channel", "chrome"),
        cdp_url=bcfg.get("cdp_url"),
    )


def run_cycle(
    scraper: StreetEasyScraper,
    store: SeenStore,
    notifier: EmailNotifier,
    config,
    *,
    baseline: bool,
    max_notify: int = 0,
) -> int:
    """Run one full poll across all search URLs. Returns number of new matches notified.

    ``max_notify`` > 0 caps how many emails a single cycle sends (handy for a
    controlled one-email test).
    """
    fetch_detail = config.browser.get("fetch_detail_pages", True)
    notified = 0

    for url in build_search_urls(config):
        listings = scraper.fetch_search(url)

        for listing in listings:
            if store.is_notified(listing.listing_id):
                continue

            # Baseline run: suppress everything currently live so we only alert on
            # genuinely new listings going forward.
            if baseline:
                store.record_seen(listing.listing_id, listing.url)
                store.mark_notified(listing.listing_id)
                continue

            store.record_seen(listing.listing_id, listing.url)

            # Cheap pre-filter on the search-card data (price/beds/baths) so we
            # don't open a detail page -- and risk a bot challenge -- for listings
            # that already clearly fall outside the guidelines.
            ok, reason = passes_filters(listing, config.filters, record_missing=False)
            if not ok:
                log.info("Skip %s (%s)", listing.url, reason)
                store.mark_notified(listing.listing_id)
                continue

            if fetch_detail:
                scraper.enrich_from_detail(listing)

            ok, reason = passes_filters(listing, config.filters)
            if not ok:
                log.info("Skip %s (%s)", listing.url, reason)
                # Mark notified so we don't re-evaluate a known non-match forever.
                store.mark_notified(listing.listing_id)
                continue

            log.info("MATCH %s (%s)", listing.url, reason)
            if notifier.send_listing(listing):
                store.mark_notified(listing.listing_id)
                notified += 1
                if max_notify and notified >= max_notify:
                    log.info("Reached max-notify cap (%d); stopping this cycle.", max_notify)
                    return notified
            else:
                log.warning("Will retry %s next cycle (email failed).", listing.url)

    return notified


def _preview_email(config, notifier: EmailNotifier) -> int:
    """Fetch a live listing and email it so the user can see the real alert format.

    Prefers a listing that actually passes the filters; otherwise falls back to the
    first one found. Does NOT touch the dedup DB, so it won't affect the baseline.
    """
    fetch_detail = config.browser.get("fetch_detail_pages", True)
    chosen = None
    fallback = None
    with make_scraper(config) as scraper:
        for url in build_search_urls(config):
            for listing in scraper.fetch_search(url):
                if fallback is None:
                    fallback = listing
                ok, _ = passes_filters(listing, config.filters, record_missing=False)
                if not ok:
                    continue
                if fetch_detail:
                    scraper.enrich_from_detail(listing)
                ok, _ = passes_filters(listing, config.filters)
                if ok:
                    chosen = listing
                    break
            if chosen:
                break

        target = chosen or fallback
        if target is None:
            log.error("No listings found to preview.")
            return 1
        if target is fallback and fetch_detail and not target.description:
            scraper.enrich_from_detail(target)

        kind = "matching" if chosen else "first-available (no full match right now)"
        log.info("Sending preview email using a %s listing: %s", kind, target.url)
        return 0 if notifier.send_listing(target) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="StreetEasy housing notification bot")
    parser.add_argument("--once", action="store_true", help="run a single cycle then exit")
    parser.add_argument("--test-email", action="store_true", help="send a plain test email then exit")
    parser.add_argument(
        "--preview-email",
        action="store_true",
        help="fetch a real current listing and email it (to preview the alert format), then exit",
    )
    parser.add_argument(
        "--notify-existing",
        action="store_true",
        help="on first run, notify on currently-live listings instead of seeding a baseline",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument(
        "--max-notify",
        type=int,
        default=0,
        help="cap emails per cycle (0 = unlimited); use 1 for a single-email test",
    )
    parser.add_argument(
        "--dump-html",
        action="store_true",
        help="fetch the first search URL, save its rendered HTML to debug/search.html, then exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    config = load_config(args.config)

    notifier = EmailNotifier(config.email)

    if args.test_email:
        return 0 if notifier.send_test() else 1

    if args.dump_html:
        urls = build_search_urls(config)
        out = BASE_DIR / "debug" / "search.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        with make_scraper(config) as scraper:
            html = scraper.fetch_html(urls[0])
        out.write_text(html, encoding="utf-8")
        log.info("Saved %d bytes of search HTML to %s", len(html), out)
        return 0

    if not config.email.is_configured:
        log.error(
            "Email is not configured. Copy .env.example to .env and set "
            "GMAIL_USER, GMAIL_APP_PASSWORD, and MAIL_RECIPIENTS."
        )
        return 1

    if args.preview_email:
        return _preview_email(config, EmailNotifier(config.email))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    poll_cfg = config.poll
    min_s = float(poll_cfg.get("min_seconds", 60))
    max_s = float(poll_cfg.get("max_seconds", 120))

    store = SeenStore(config.db_path)
    baseline = store.count() == 0 and not args.notify_existing
    if baseline:
        log.info("Empty database: first cycle will seed a silent baseline.")

    exit_code = 0
    try:
        with make_scraper(config) as scraper:
            while not _stop:
                try:
                    n = run_cycle(
                        scraper, store, notifier, config,
                        baseline=baseline, max_notify=args.max_notify,
                    )
                    if baseline:
                        log.info("Baseline seeded with %d listing(s).", store.count())
                        baseline = False
                    elif n:
                        log.info("Cycle complete: %d new match(es) notified.", n)
                    else:
                        log.info("Cycle complete: no new matches.")
                    sleep_s = random.uniform(min_s, max_s)
                except BlockedError as exc:
                    # Backed off hard: StreetEasy is challenging us. Wait longer.
                    sleep_s = random.uniform(max_s * 4, max_s * 8)
                    log.warning("%s -- backing off %.0fs.", exc, sleep_s)
                except Exception as exc:  # keep the loop alive on transient errors
                    sleep_s = random.uniform(min_s, max_s)
                    log.exception("Cycle error: %s", exc)

                if args.once:
                    break

                # Sleep in short slices so signals are handled promptly.
                slept = 0.0
                while slept < sleep_s and not _stop:
                    chunk = min(2.0, sleep_s - slept)
                    time.sleep(chunk)
                    slept += chunk
    finally:
        store.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
