from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

BUSY_TIMEOUT_MILLISECONDS = 5_000


def post_key(post_id: str) -> str:
    """Build a namespaced archive key for a delivered post."""
    return _namespaced_key("post", post_id)


def image_key(sha256: str) -> str:
    """Build a namespaced archive key for a delivered image hash."""
    return _namespaced_key("image", sha256)


def destination_key(transport: str, destination: str, media_id: str) -> str:
    """Collision-free receipt key; only identifiers, never image bytes or secrets."""
    return "receipt:" + json.dumps([transport, destination, media_id], separators=(",", ":"))


def _namespaced_key(kind: str, identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier:
        raise ValueError(f"{kind} identifier must be a non-empty string")
    return f"{kind}:{identifier}"


def _utc_timestamp(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).timestamp()


class DeliveryArchive:
    """SQLite ledger containing only successful delivery identifiers."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MILLISECONDS / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._configure()
        except Exception:
            self.close()
            raise

    def _configure(self) -> None:
        connection = self._get_connection()
        with self._lock:
            connection.execute(
                f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}"
            )
            journal_mode = connection.execute(
                "PRAGMA journal_mode = WAL"
            ).fetchone()[0]
            if str(journal_mode).casefold() != "wal":
                raise RuntimeError("delivery archive requires SQLite WAL mode")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_archive (
                    source TEXT NOT NULL,
                    gallery_name TEXT NOT NULL,
                    delivery_key TEXT NOT NULL,
                    delivered_at REAL NOT NULL,
                    PRIMARY KEY (source, gallery_name, delivery_key)
                ) WITHOUT ROWID
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS delivery_archive_age ON delivery_archive(delivered_at)"
            )

    def add_many(self, source: str, gallery: str, keys: list[str]) -> None:
        """Commit one acknowledged payload atomically, preserving existing receipts."""
        rows = [(*self._validate_fields(source, gallery, key), _utc_timestamp(self._clock())) for key in keys]
        with self._lock:
            connection = self._get_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    "INSERT OR IGNORE INTO delivery_archive VALUES (?, ?, ?, ?)", rows
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def check(self, source: str, gallery: str, key: str) -> bool:
        """Return whether a delivery key has already been acknowledged."""
        source, gallery, key = self._validate_fields(source, gallery, key)
        with self._lock:
            row = self._get_connection().execute(
                """
                SELECT 1
                FROM delivery_archive
                WHERE source = ? AND gallery_name = ? AND delivery_key = ?
                """,
                (source, gallery, key),
            ).fetchone()
        return row is not None

    def add(self, source: str, gallery: str, key: str) -> None:
        """Idempotently record a key after its delivery has succeeded."""
        source, gallery, key = self._validate_fields(source, gallery, key)
        delivered_at = _utc_timestamp(self._clock())
        with self._lock:
            self._get_connection().execute(
                """
                INSERT OR IGNORE INTO delivery_archive (
                    source,
                    gallery_name,
                    delivery_key,
                    delivered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (source, gallery, key, delivered_at),
            )

    def prune(self, before: datetime) -> int:
        """Remove entries delivered before ``before`` and return their count."""
        cutoff = _utc_timestamp(before)
        with self._lock:
            cursor = self._get_connection().execute(
                "DELETE FROM delivery_archive WHERE delivered_at < ?",
                (cutoff,),
            )
        return max(0, cursor.rowcount)

    def close(self) -> None:
        """Close this process's shared SQLite connection."""
        with self._lock:
            if self._connection is None:
                return
            self._connection.close()
            self._connection = None

    def _get_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("delivery archive is closed")
        return self._connection

    @staticmethod
    def _validate_fields(
        source: str,
        gallery: str,
        key: str,
    ) -> tuple[str, str, str]:
        fields = {"source": source, "gallery": gallery, "key": key}
        for name, value in fields.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        return source, gallery, key

    def __enter__(self) -> DeliveryArchive:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
