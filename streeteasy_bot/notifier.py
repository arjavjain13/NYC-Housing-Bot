"""Email notifications via Gmail SMTP."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from config import EmailConfig
from models import Listing

log = logging.getLogger("streeteasy.notifier")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL


def _fmt(value, suffix: str = "", money: bool = False) -> str:
    if value is None:
        return "?"
    if money:
        return f"${int(value):,}"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value}{suffix}"


def _refurbished_label(listing: Listing) -> str:
    if listing.refurbished is None:
        return "refurbishment unknown"
    return "refurbished" if listing.refurbished else "not refurbished"


def _show_refurbished_line(listing: Listing) -> bool:
    """Show the body refurbished line for pre-war units (always) and for any
    unit we positively detect as refurbished. Hide it for non-pre-war units
    where refurbishment wasn't detected."""
    return listing.is_pre_war or listing.refurbished is True


def _subject(cfg: EmailConfig, listing: Listing) -> str:
    # Format: "[prefix] {cost}, {area}, {year built} built[, {refurbished if pre-war}]"
    cost = _fmt(listing.price, money=True)
    area = listing.neighborhood or "?"
    year = _fmt(listing.year_built)
    parts = [cost, area, f"{year} built"]
    if listing.is_pre_war:
        parts.append(_refurbished_label(listing))
    body = ", ".join(parts)
    return f"{cfg.subject_prefix} {body}".strip()


def _plain_body(listing: Listing) -> str:
    lines = [
        listing.title or "StreetEasy listing",
        "",
        f"Price:        {_fmt(listing.price, money=True)}",
        f"Beds:         {_fmt(listing.beds)}",
        f"Baths:        {_fmt(listing.baths)}",
        f"Year built:   {_fmt(listing.year_built)}",
        f"Available:    {listing.available_date or '?'}",
    ]
    if _show_refurbished_line(listing):
        lines.append(f"Refurbished:  {_refurbished_label(listing)}")
    lines += [
        "",
        f"LINK: {listing.url}",
    ]
    if listing.is_unverified:
        lines += [
            "",
            "NOTE - could not verify everything; double-check on the listing:",
            *(f"  - {r}" for r in listing.unverified_reasons),
        ]
    lines += ["", "Act fast - contact the listing agent before it's gone."]
    return "\n".join(lines)


def _html_body(listing: Listing) -> str:
    rows = [
        ("Price", _fmt(listing.price, money=True)),
        ("Beds", _fmt(listing.beds)),
        ("Baths", _fmt(listing.baths)),
        ("Year built", _fmt(listing.year_built)),
        ("Available", str(listing.available_date) if listing.available_date else "?"),
    ]
    if _show_refurbished_line(listing):
        rows.append(("Refurbished", _refurbished_label(listing)))
    row_html = "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#666'>{k}</td>"
        f"<td style='padding:2px 0;font-weight:600'>{v}</td></tr>"
        for k, v in rows
    )
    unverified_html = ""
    if listing.is_unverified:
        items = "".join(f"<li>{r}</li>" for r in listing.unverified_reasons)
        unverified_html = (
            "<p style='color:#b35900;background:#fff4e5;padding:8px 12px;"
            "border-radius:6px'><strong>Unverified:</strong><ul>" + items + "</ul></p>"
        )
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:520px">
  <h2 style="margin:0 0 4px">{listing.title or 'New StreetEasy listing'}</h2>
  <table style="border-collapse:collapse;margin:8px 0">{row_html}</table>
  {unverified_html}
  <p><a href="{listing.url}"
        style="display:inline-block;background:#0a6cff;color:#fff;text-decoration:none;
               padding:10px 18px;border-radius:6px;font-weight:600">
     View &amp; contact agent on StreetEasy</a></p>
  <p style="color:#888;font-size:12px">Act fast - new listings go quickly.</p>
</div>"""


class EmailNotifier:
    def __init__(self, cfg: EmailConfig):
        self.cfg = cfg

    def _send(self, msg: EmailMessage) -> None:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as server:
            server.login(self.cfg.user, self.cfg.app_password)
            server.send_message(msg)

    def send_listing(self, listing: Listing) -> bool:
        """Email all recipients about a listing. Returns True on success."""
        if not self.cfg.is_configured:
            log.error("Email not configured (check GMAIL_USER/GMAIL_APP_PASSWORD/recipients)")
            return False

        msg = EmailMessage()
        msg["Subject"] = _subject(self.cfg, listing)
        msg["From"] = formataddr(("StreetEasy Bot", self.cfg.user))
        msg["To"] = ", ".join(self.cfg.recipients)
        msg.set_content(_plain_body(listing))
        msg.add_alternative(_html_body(listing), subtype="html")

        try:
            self._send(msg)
            log.info("Notified %d recipient(s) about %s", len(self.cfg.recipients), listing.url)
            return True
        except Exception as exc:
            log.error("Failed to send email for %s: %s", listing.url, exc)
            return False

    def send_test(self) -> bool:
        """Send a quick self-test email to confirm SMTP credentials work."""
        if not self.cfg.is_configured:
            log.error("Email not configured; cannot send test.")
            return False
        msg = EmailMessage()
        msg["Subject"] = f"{self.cfg.subject_prefix} Test - bot is working"
        msg["From"] = formataddr(("StreetEasy Bot", self.cfg.user))
        msg["To"] = ", ".join(self.cfg.recipients)
        msg.set_content(
            "This is a test from your StreetEasy bot. If you received this, "
            "email notifications are configured correctly."
        )
        try:
            self._send(msg)
            log.info("Test email sent to %s", self.cfg.recipients)
            return True
        except Exception as exc:
            log.error("Test email failed: %s", exc)
            return False
