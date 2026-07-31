from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from Module.config import AppConfig
from Module.delivery_archive import DeliveryArchive, image_key, post_key


def test_add_and_check_are_idempotent(tmp_path) -> None:
    archive_path = tmp_path / "delivery.sqlite3"

    with DeliveryArchive(archive_path) as archive:
        key = post_key("123")
        assert archive.check("dcinside", "cats", key) is False

        archive.add("dcinside", "cats", key)
        archive.add("dcinside", "cats", key)

        assert archive.check("dcinside", "cats", key) is True

    with sqlite3.connect(archive_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM delivery_archive"
        ).fetchone()[0]
    assert count == 1


def test_reopening_archive_preserves_delivery_keys(tmp_path) -> None:
    archive_path = tmp_path / "delivery.sqlite3"
    key = image_key("abc123")

    with DeliveryArchive(archive_path) as archive:
        archive.add("arcalive", "animals", key)

    with DeliveryArchive(archive_path) as reopened:
        assert reopened.check("arcalive", "animals", key) is True


def test_post_and_image_keys_are_separate(tmp_path) -> None:
    archive_path = tmp_path / "delivery.sqlite3"

    with DeliveryArchive(archive_path) as archive:
        archive.add("dcinside", "cats", post_key("same-value"))

        assert archive.check("dcinside", "cats", post_key("same-value")) is True
        assert archive.check("dcinside", "cats", image_key("same-value")) is False


def test_source_and_gallery_are_part_of_the_key(tmp_path) -> None:
    archive_path = tmp_path / "delivery.sqlite3"
    key = post_key("123")

    with DeliveryArchive(archive_path) as archive:
        archive.add("dcinside", "cats", key)

        assert archive.check("dcinside", "dogs", key) is False
        assert archive.check("arcalive", "cats", key) is False


def test_prune_removes_only_entries_before_cutoff(tmp_path) -> None:
    archive_path = tmp_path / "delivery.sqlite3"
    current_time = [datetime(2026, 1, 1, tzinfo=UTC)]

    with DeliveryArchive(archive_path, clock=lambda: current_time[0]) as archive:
        archive.add("dcinside", "cats", post_key("old"))
        current_time[0] = datetime(2026, 1, 10, tzinfo=UTC)
        archive.add("dcinside", "cats", post_key("new"))

        removed = archive.prune(datetime(2026, 1, 5, tzinfo=UTC))

        assert removed == 1
        assert archive.check("dcinside", "cats", post_key("old")) is False
        assert archive.check("dcinside", "cats", post_key("new")) is True


def test_uses_wal_journal_mode(tmp_path) -> None:
    archive_path = tmp_path / "delivery.sqlite3"

    with DeliveryArchive(archive_path):
        with sqlite3.connect(archive_path) as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert journal_mode.casefold() == "wal"


def test_two_live_connections_share_one_archive(tmp_path) -> None:
    archive_path = tmp_path / "delivery.sqlite3"

    with (
        DeliveryArchive(archive_path) as first,
        DeliveryArchive(archive_path) as second,
    ):
        first.add("dcinside", "cats", post_key("1"))
        second.add("arcalive", "dogs", post_key("2"))

        assert second.check("dcinside", "cats", post_key("1")) is True
        assert first.check("arcalive", "dogs", post_key("2")) is True


def test_schema_contains_only_identifiers_and_timestamp(tmp_path) -> None:
    archive_path = tmp_path / "delivery.sqlite3"

    with DeliveryArchive(archive_path):
        with sqlite3.connect(archive_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(delivery_archive)"
                ).fetchall()
            }

    assert columns == {"source", "gallery_name", "delivery_key", "delivered_at"}


def test_archive_path_is_loaded_from_environment(monkeypatch, tmp_path) -> None:
    archive_path = tmp_path / "configured.sqlite3"
    monkeypatch.setenv("ARCHIVE_PATH", str(archive_path))

    assert AppConfig.from_env().archive_path == str(archive_path)
