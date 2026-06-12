"""SQLite-backed dedup store so each listing only notifies once."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("streeteasy.store")


class SeenStore:
    """Tracks listing IDs we've already processed.

    Two states are recorded so a transient email failure doesn't permanently
    lose a listing: a row is inserted when first seen, and ``notified_at`` is set
    only after a successful notification.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_listings (
                listing_id   TEXT PRIMARY KEY,
                url          TEXT,
                first_seen   TEXT NOT NULL,
                notified_at  TEXT
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def is_notified(self, listing_id: str) -> bool:
        """True only if we've already successfully notified for this listing."""
        cur = self._conn.execute(
            "SELECT notified_at FROM seen_listings WHERE listing_id = ?",
            (listing_id,),
        )
        row = cur.fetchone()
        return bool(row and row[0])

    def record_seen(self, listing_id: str, url: str) -> None:
        self._conn.execute(
            """
            INSERT INTO seen_listings (listing_id, url, first_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(listing_id) DO NOTHING
            """,
            (listing_id, url, self._now()),
        )
        self._conn.commit()

    def mark_notified(self, listing_id: str) -> None:
        self._conn.execute(
            "UPDATE seen_listings SET notified_at = ? WHERE listing_id = ?",
            (self._now(), listing_id),
        )
        self._conn.commit()

    def count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM seen_listings")
        return int(cur.fetchone()[0])

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "SeenStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
