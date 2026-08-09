import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.delivery_result import ChannelDelivery, DeliveryOutcome, DeliveryResult
from Module.media_pipeline import MediaPipeline


def test_delivery_result_merge_is_pure_and_has_no_bool_override() -> None:
    failed = ChannelDelivery(
        transport="discord",
        destination_id="111",
        outcome=DeliveryOutcome.FAILED,
        requested_media=("hash-a",),
        delivered_media=(),
        ack_eligible=True,
        reason="send_failed",
    )
    succeeded = ChannelDelivery(
        transport="discord",
        destination_id="222",
        outcome=DeliveryOutcome.SUCCEEDED,
        requested_media=("hash-a",),
        delivered_media=("hash-a",),
        ack_eligible=True,
    )
    first = DeliveryResult((failed,))
    second = DeliveryResult((succeeded,))

    merged = first.merge(second)

    assert merged.deliveries == (failed, succeeded)
    assert merged.acknowledged is True
    assert first.deliveries == (failed,)
    assert first.acknowledged is False
    assert "__bool__" not in DeliveryResult.__dict__


@pytest.mark.asyncio
async def test_send_single_forwards_source_footer() -> None:
    pipeline = MediaPipeline(MagicMock(), MagicMock(), [12345], source_label="디시인사이드")
    pipeline.message_sender.send_to_discord = AsyncMock(return_value=True)

    result = await pipeline.send_single_to_channels(
        {"discord_buffer": io.BytesIO(b"x"), "filename": "a.jpg", "validated": True},
        title="제목",
        link="https://gall.dcinside.com/1",
    )

    assert result.acknowledged is True
    assert result.deliveries[0].outcome is DeliveryOutcome.SUCCEEDED
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
    result = await pipeline.send_batch_to_channel(
        channel, batch, title="제목", link="https://arca.live/1", batch_index=0
    )

    assert result.acknowledged is True
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
async def test_channel_failure_and_success_are_preserved_without_retry() -> None:
    sender = MagicMock()
    sender.send_to_discord = AsyncMock(side_effect=[False, True])
    client = MagicMock()
    channel_a = MagicMock()
    channel_b = MagicMock()
    client.get_channel.side_effect = [channel_a, channel_b]
    pipeline = MediaPipeline(sender, client, [111, 222])

    result = await pipeline.send_single_to_channels({
        "discord_buffer": io.BytesIO(b"x"),
        "filename": "a.jpg",
        "content_hash": "hash-a",
        "validated": True,
    })

    assert result.acknowledged is True
    assert [delivery.outcome for delivery in result.deliveries] == [
        DeliveryOutcome.FAILED,
        DeliveryOutcome.SUCCEEDED,
    ]
    assert [delivery.reason for delivery in result.deliveries] == ["send_failed", None]
    assert sender.send_to_discord.await_count == 2
    assert sender.send_to_discord.await_args_list[0].args[0] is channel_a
    assert sender.send_to_discord.await_args_list[1].args[0] is channel_b


@pytest.mark.asyncio
async def test_missing_channel_records_channel_not_found() -> None:
    sender = MagicMock()
    sender.send_to_discord = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = None
    pipeline = MediaPipeline(sender, client, [111])

    result = await pipeline.send_single_to_channels({
        "discord_buffer": io.BytesIO(b"x"),
        "filename": "a.jpg",
        "content_hash": "hash-a",
    })

    assert result == DeliveryResult((
        ChannelDelivery(
            transport="discord",
            destination_id="111",
            outcome=DeliveryOutcome.FAILED,
            requested_media=("hash-a",),
            delivered_media=(),
            ack_eligible=True,
            reason="channel_not_found",
        ),
    ))
    assert result.acknowledged is False
    sender.send_to_discord.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_success_acknowledges_failed_discord() -> None:
    sender = MagicMock()
    sender.telegram_chat_id = "telegram-chat"
    sender.send_to_discord = AsyncMock(return_value=False)
    sender.send_to_telegram = AsyncMock(return_value=True)
    client = MagicMock()
    client.get_channel.return_value = MagicMock()
    pipeline = MediaPipeline(sender, client, [111])

    result = await pipeline.distribute([{
        "discord_buffer": io.BytesIO(b"discord"),
        "telegram_buffer": io.BytesIO(b"telegram"),
        "filename": "a.jpg",
        "is_gif": False,
        "content_hash": "hash-a",
        "validated": True,
    }])

    assert result.acknowledged is True
    assert [delivery.outcome for delivery in result.deliveries] == [
        DeliveryOutcome.FAILED,
        DeliveryOutcome.SUCCEEDED,
    ]
    assert [delivery.transport for delivery in result.deliveries] == ["discord", "telegram"]


@pytest.mark.asyncio
async def test_all_destination_failures_are_not_acknowledged() -> None:
    sender = MagicMock()
    sender.telegram_chat_id = "telegram-chat"
    sender.send_to_discord = AsyncMock(return_value=False)
    sender.send_to_telegram = AsyncMock(return_value=False)
    client = MagicMock()
    client.get_channel.return_value = MagicMock()
    pipeline = MediaPipeline(sender, client, [111])

    result = await pipeline.distribute([{
        "discord_buffer": io.BytesIO(b"discord"),
        "telegram_buffer": io.BytesIO(b"telegram"),
        "filename": "a.jpg",
        "is_gif": False,
        "content_hash": "hash-a",
        "validated": True,
    }])

    assert result.acknowledged is False
    assert all(delivery.outcome is DeliveryOutcome.FAILED for delivery in result.deliveries)


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

    assert queued.deliveries[0].outcome is DeliveryOutcome.QUEUED
    assert queued.deliveries[0].ack_eligible is False
    assert queued.acknowledged is False
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

    result = await pipeline.attach_to_web_gallery(b"next", "x.jpg", 0, "", "")

    assert result.deliveries[0].outcome is DeliveryOutcome.FAILED
    assert result.deliveries[0].reason == "queue_full"
    assert result.acknowledged is False


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
