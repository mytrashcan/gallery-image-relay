import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.media_pipeline import MediaPipeline


@pytest.mark.asyncio
async def test_send_single_forwards_source_footer() -> None:
    pipeline = MediaPipeline(MagicMock(), MagicMock(), [12345], source_label="디시인사이드")
    pipeline.message_sender.send_to_discord = AsyncMock(return_value=True)

    sent = await pipeline.send_single_to_channels(
        {"discord_buffer": io.BytesIO(b"x"), "filename": "a.jpg", "validated": True},
        title="제목",
        link="https://gall.dcinside.com/1",
    )

    assert sent is True
    pipeline.message_sender.send_to_discord.assert_awaited_once()
    call = pipeline.message_sender.send_to_discord.await_args
    assert call is not None
    assert call.kwargs["footer"] == "디시인사이드 · 1개 이미지"


@pytest.mark.asyncio
async def test_batch_footer_uses_source_label() -> None:
    pipeline = MediaPipeline(MagicMock(), MagicMock(), [12345])
    channel = MagicMock()
    channel.send = AsyncMock()

    batch = [
        {"discord_buffer": io.BytesIO(b"a"), "filename": "a.jpg"},
        {"discord_buffer": io.BytesIO(b"b"), "filename": "b.jpg"},
    ]
    ok = await pipeline.send_batch_to_channel(
        channel, batch, title="제목", link="https://arca.live/1", batch_index=0
    )

    assert ok is True
    sent = channel.send.await_args
    embeds = sent.kwargs["embeds"]
    assert embeds[0].footer.text == "아카라이브 · 2개 이미지"
    assert embeds[1].footer.text is None


@pytest.mark.asyncio
async def test_batch_footer_custom_label() -> None:
    pipeline = MediaPipeline(MagicMock(), MagicMock(), [12345], source_label="디시인사이드")
    channel = MagicMock()
    channel.send = AsyncMock()

    batch = [{"discord_buffer": io.BytesIO(b"a"), "filename": "a.jpg"}]
    await pipeline.send_batch_to_channel(
        channel, batch, title="제목", link="https://gall.dcinside.com/1", batch_index=0
    )

    sent = channel.send.await_args
    assert sent.kwargs["embeds"][0].footer.text == "디시인사이드 · 1개 이미지"


@pytest.mark.asyncio
async def test_web_publish_is_enqueued_without_waiting_for_network() -> None:
    pipeline = MediaPipeline(MagicMock(), MagicMock(), [], web_gallery_enabled=True)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_publish(*args, **kwargs):
        started.set()
        await release.wait()
        return {"id": "x"}

    pipeline.gallery_client.publish_async = slow_publish
    queued = await pipeline.attach_to_web_gallery(b"image", "x.jpg", 0, "title", "link")

    assert queued == {"queued": True}
    await started.wait()
    release.set()
    await pipeline.close()


@pytest.mark.asyncio
async def test_full_web_queue_drops_without_blocking(monkeypatch) -> None:
    monkeypatch.setattr("Module.config.app_config.web_upload_queue_size", 1)
    pipeline = MediaPipeline(MagicMock(), MagicMock(), [], web_gallery_enabled=True)
    pipeline._ensure_web_worker = MagicMock()
    pipeline._web_queue = asyncio.Queue(maxsize=1)
    pipeline._web_queue.put_nowait(((), {}))

    assert await pipeline.attach_to_web_gallery(b"next", "x.jpg", 0, "", "") == {}


def test_web_image_uses_original_only_within_ingest_limit(monkeypatch) -> None:
    monkeypatch.setattr("Module.config.app_config.web_ingest_max_mb", 1)
    compressed = io.BytesIO(b"compressed")

    assert MediaPipeline._web_image_data({
        "original_data": b"original",
        "discord_buffer": compressed,
    }) == b"original"
    assert MediaPipeline._web_image_data({
        "original_data": b"x" * (1024 * 1024 + 1),
        "discord_buffer": compressed,
    }) == b"compressed"
