"""Archive I/O must not stall the event loop while another process holds the SQLite write lock."""
from __future__ import annotations

import asyncio
import io
import sqlite3
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.delivery_archive import DeliveryArchive, destination_key, image_key, post_key
from Module.image_handler import ImageHandler
from Module.media_pipeline import MediaPipeline, PreparedMedia
from Module.message_sender import MessageSender

LOCK_SECONDS = 0.8
MAX_LOOP_GAP = 0.3


@contextmanager
def write_locked(path, seconds: float = LOCK_SECONDS):
    """Hold the database write lock from a second connection, like another crawler mid-commit."""
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    released = threading.Event()

    def release():
        other.execute("COMMIT")
        released.set()

    timer = threading.Timer(seconds, release)
    timer.start()
    try:
        yield released
    finally:
        timer.join()
        other.close()


async def longest_loop_gap(awaitable) -> tuple[float, object]:
    gaps = []
    last = time.monotonic()

    async def ticker():
        nonlocal last
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(0.05)  # let the ticker start before work that may never yield
        result = await awaitable
        await asyncio.sleep(0.05)  # let it record a stall that ended just now
    finally:
        tick.cancel()
        await asyncio.gather(tick, return_exceptions=True)
    return max(gaps, default=0.0), result


def media(media_id: str) -> PreparedMedia:
    return PreparedMedia(io.BytesIO(b"x"), io.BytesIO(b"x"), f"{media_id}.png", media_id, False, b"x", True)


@pytest.mark.asyncio
async def test_receipt_write_waits_off_the_event_loop(tmp_path):
    path = tmp_path / "archive.sqlite3"
    sender = MagicMock()
    sender.send_discord_payload = AsyncMock(return_value=MessageSender._discord_delivery("1", ("a",), ("a",)))
    client = MagicMock()
    client.get_channel.return_value = MagicMock()
    with DeliveryArchive(path) as archive:
        pipeline = MediaPipeline(sender, client, [1], telegram_enabled=False,
                                 delivery_archive=archive, source="dcinside", gallery_name="cats")
        with write_locked(path):
            gap, _ = await longest_loop_gap(pipeline.send_discord_batch([media("a")], title="", link=None))

        assert gap < MAX_LOOP_GAP
        assert archive.check("dcinside", "cats", destination_key("discord", "1", "a"))


@pytest.mark.asyncio
async def test_413_fallback_receipt_waits_off_the_loop_and_survives_cancellation(tmp_path):
    import discord

    path = tmp_path / "archive.sqlite3"
    calls = 0

    async def send(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise discord.HTTPException(SimpleNamespace(status=413, reason="large"), "large")
        if calls == 3:
            raise asyncio.CancelledError

    channel = SimpleNamespace(send=send)
    with DeliveryArchive(path) as archive:
        pipeline = MediaPipeline(MessageSender(None, None), SimpleNamespace(get_channel=lambda _: channel), [1],
                                 telegram_enabled=False, delivery_archive=archive, source="arcalive", gallery_name="t")
        async def send_batch():
            try:
                await pipeline.send_discord_batch([media("a"), media("b")], title="", link=None)
            except asyncio.CancelledError:
                return "cancelled"

        with write_locked(path):
            gap, outcome = await longest_loop_gap(send_batch())

        assert outcome == "cancelled"
        assert gap < MAX_LOOP_GAP
        assert archive.check("arcalive", "t", destination_key("discord", "1", "a"))
        assert not archive.check("arcalive", "t", destination_key("discord", "1", "b"))


@pytest.mark.asyncio
async def test_dc_post_acknowledgement_waits_off_the_event_loop(tmp_path, monkeypatch):
    import discord

    from Module.dcbot import DCBot

    path = tmp_path / "archive.sqlite3"
    monkeypatch.setattr("Module.dcbot.app_config.archive_path", str(path))
    monkeypatch.setattr("Module.dcbot.ProcessLeaderLock", MagicMock)
    bot = DCBot("token", "https://gall.dcinside.com/mgallery/board/lists/?id=test", ["1"], "", "",
                discord.Intents.none(), gallery_name="cats")
    bot.crawler.get_latest_post = MagicMock(return_value={
        "title": "text", "link": "https://gall.dcinside.com/x", "post_id": "7", "has_image": False,
    })
    try:
        with write_locked(path) as released:
            async def until_acknowledged():
                crawl = asyncio.create_task(bot.start_crawling())
                while "7" not in bot.crawler.sent_post_ids:  # memory only: no archive call here
                    await asyncio.sleep(0.02)
                crawl.cancel()
                await asyncio.gather(crawl, return_exceptions=True)

            gap, _ = await longest_loop_gap(until_acknowledged())
            assert released.is_set()

        assert gap < MAX_LOOP_GAP
        assert bot.delivery_archive.check("dcinside", "cats", post_key("7"))
    finally:
        bot.crawler.session.close()
        bot.image_handler.session.close()
        bot.delivery_archive.close()


def test_hash_lock_is_not_held_during_archive_writes(tmp_path):
    path = tmp_path / "archive.sqlite3"
    with DeliveryArchive(path) as archive:
        handler = ImageHandler(source="arcalive", gallery_name="cats", delivery_archive=archive)
        with write_locked(path):
            writer = threading.Thread(target=handler.mark_hash_sent, args=("h1",))
            writer.start()
            time.sleep(0.1)  # the writer is now waiting on SQLite

            started = time.monotonic()
            handler.release_hash("other")
            assert handler.reserve_pending_hash("h2") is True
            loop_side = time.monotonic() - started
            writer.join()

        assert loop_side < 0.1
        assert archive.check("arcalive", "cats", image_key("h1"))
        assert handler.has_seen_hash("h1") is True
        assert handler.reserve_pending_hash("h1") is False
