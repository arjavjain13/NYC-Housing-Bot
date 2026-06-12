"""Configuration loading: merges config.yaml with environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


@dataclass
class EmailConfig:
    user: str
    app_password: str
    recipients: list[str]
    subject_prefix: str = "[StreetEasy]"

    @property
    def is_configured(self) -> bool:
        return bool(self.user and self.app_password and self.recipients)


@dataclass
class Config:
    raw: dict[str, Any]
    email: EmailConfig
    db_path: Path

    # --- convenience accessors -------------------------------------------------
    @property
    def search(self) -> dict[str, Any]:
        return self.raw.get("search", {})

    @property
    def filters(self) -> dict[str, Any]:
        return self.raw.get("filters", {})

    @property
    def poll(self) -> dict[str, Any]:
        return self.raw.get("poll", {})

    @property
    def browser(self) -> dict[str, Any]:
        return self.raw.get("browser", {})


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_config(config_path: str | os.PathLike[str] | None = None) -> Config:
    """Load configuration from YAML and overlay secrets from the environment."""
    load_dotenv(BASE_DIR / ".env")

    path = Path(config_path) if config_path else BASE_DIR / "config.yaml"
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    email_section = raw.get("email", {}) or {}
    recipients = list(email_section.get("recipients", []) or [])
    recipients += _split_csv(os.getenv("MAIL_RECIPIENTS"))
    # De-duplicate while preserving order.
    recipients = list(dict.fromkeys(recipients))

    email = EmailConfig(
        user=os.getenv("GMAIL_USER", ""),
        app_password=os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", ""),
        recipients=recipients,
        subject_prefix=email_section.get("subject_prefix", "[StreetEasy]"),
    )

    storage = raw.get("storage", {}) or {}
    db_path = BASE_DIR / storage.get("db_path", "seen_listings.db")

    return Config(raw=raw, email=email, db_path=db_path)
