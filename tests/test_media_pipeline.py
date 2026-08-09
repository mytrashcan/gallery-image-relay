import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from Module.delivery_result import ChannelDelivery, DeliveryOutcome, DeliveryResult
from Module.media_pipeline import MediaPipeline


def discord_delivery(
    destination_id: str,
    requested_media: tuple[str, ...],
    outcome: DeliveryOutcome = DeliveryOutcome.SUCCEEDED,
    delivered_media: tuple[str, ...] | None = None,
) -> ChannelDelivery:
    if delivered_media is None:
        delivered_media = requested_media if outcome is DeliveryOutcome.SUCCEEDED else ()
    return ChannelDelivery(
        transport="discord",
        destination_id=destination_id,
        outcome=outcome,
        requested_media=requested_media,
        delivered_media=delivered_media,
        ack_eligible=True,
        reason=None if outcome is DeliveryOutcome.SUCCEEDED else "send_failed",
    )


async def successful_discord_payload(
    channel,
    items,
    *,
    files,
    embeds,
    destination_id,
    requested_media,
):
    return discord_delivery(destination_id, requested_media)


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
async def test_dc_single_and_arca_multi_use_shared_discord_payload_builder() -> None:
    client = MagicMock()
    client.get_channel.return_value = MagicMock()

    dc_sender = MagicMock()
    dc_sender.send_discord_payload = AsyncMock(side_effect=successful_discord_payload)
    dc_pipeline = MediaPipeline(
        dc_sender,
        client,
        [12345],
        telegram_enabled=False,
        source_label="디시인사이드",
    )
    dc_builder = MagicMock(wraps=dc_pipeline._build_discord_payload)
    dc_pipeline._build_discord_payload = dc_builder
    dc_item = {
        "discord_buffer": io.BytesIO(b"dc"),
        "telegram_buffer": io.BytesIO(b"dc"),
        "filename": "dc.jpg",
        "is_gif": False,
        "content_hash": "dc-hash",
        "validated": True,
    }

    await dc_pipeline.distribute(
        [dc_item],
        title="DC 제목",
        link="https://gall.dcinside.com/1",
    )

    arca_sender = MagicMock()
    arca_sender.send_discord_payload = AsyncMock(side_effect=successful_discord_payload)
    arca_pipeline = MediaPipeline(arca_sender, client, [12345], telegram_enabled=False)
    arca_builder = MagicMock(wraps=arca_pipeline._build_discord_payload)
    arca_pipeline._build_discord_payload = arca_builder
    arca_batch = [
        {"discord_buffer": io.BytesIO(b"a"), "filename": "a.jpg", "content_hash": "h1"},
        {"discord_buffer": io.BytesIO(b"b"), "filename": "b.jpg", "content_hash": "h2"},
    ]

    await arca_pipeline.send_discord_batch(
        arca_batch,
        title="Arca 제목",
        link="https://arca.live/1",
    )

    dc_builder.assert_called_once()
    arca_builder.assert_called_once()
    assert len(dc_builder.call_args.args[0]) == 1
    assert len(arca_builder.call_args.args[0]) == 2


def test_discord_payload_uses_source_specific_footer_counts() -> None:
    dc_pipeline = MediaPipeline(MagicMock(), MagicMock(), [], source_label="디시인사이드")
    _, dc_embeds = dc_pipeline._build_discord_payload(
        [{"discord_buffer": io.BytesIO(b"dc"), "filename": "dc.jpg"}],
        title="DC 제목",
        link="https://gall.dcinside.com/1",
    )
    arca_pipeline = MediaPipeline(MagicMock(), MagicMock(), [], source_label="아카라이브")
    _, arca_embeds = arca_pipeline._build_discord_payload(
        [
            {"discord_buffer": io.BytesIO(b"a"), "filename": "a.jpg"},
            {"discord_buffer": io.BytesIO(b"b"), "filename": "b.jpg"},
        ],
        title="Arca 제목",
        link="https://arca.live/1",
    )

    assert dc_embeds[0].footer.text == "디시인사이드 · 1개 이미지"
    assert arca_embeds[0].footer.text == "아카라이브 · 2개 이미지"
    assert arca_embeds[1].footer.text is None


def test_only_first_global_embed_has_post_metadata() -> None:
    pipeline = MediaPipeline(MagicMock(), MagicMock(), [])
    batch = [
        {"discord_buffer": io.BytesIO(b"a"), "filename": "a.jpg"},
        {"discord_buffer": io.BytesIO(b"b"), "filename": "b.jpg"},
    ]

    _, first_batch_embeds = pipeline._build_discord_payload(
        batch,
        title="제목",
        link="https://arca.live/1",
        start_index=0,
    )
    _, later_batch_embeds = pipeline._build_discord_payload(
        batch,
        title="제목",
        link="https://arca.live/1",
        start_index=2,
    )

    assert first_batch_embeds[0].title == "제목"
    assert first_batch_embeds[0].url == "https://arca.live/1"
    assert first_batch_embeds[0].footer.text == "아카라이브 · 2개 이미지"
    assert first_batch_embeds[1].title is None
    assert first_batch_embeds[1].url is None
    assert first_batch_embeds[1].footer.text is None
    assert all(embed.title is None for embed in later_batch_embeds)
    assert all(embed.url is None for embed in later_batch_embeds)
    assert all(embed.footer.text is None for embed in later_batch_embeds)


@pytest.mark.asyncio
async def test_any_successful_discord_channel_acknowledges_batch_without_retry() -> None:
    sender = MagicMock()
    sender.send_discord_payload = AsyncMock(side_effect=[
        discord_delivery("111", ("hash-a",), DeliveryOutcome.FAILED),
        discord_delivery("222", ("hash-a",)),
    ])
    client = MagicMock()
    channel_a = MagicMock()
    channel_b = MagicMock()
    client.get_channel.side_effect = [channel_a, channel_b]
    pipeline = MediaPipeline(sender, client, [111, 222])

    result = await pipeline.send_discord_batch(
        [{
            "discord_buffer": io.BytesIO(b"x"),
            "filename": "a.jpg",
            "content_hash": "hash-a",
            "validated": True,
        }],
        title="title",
        link="https://example.com/1",
    )

    assert result.acknowledged is True
    assert [delivery.outcome for delivery in result.deliveries] == [
        DeliveryOutcome.FAILED,
        DeliveryOutcome.SUCCEEDED,
    ]
    assert [delivery.reason for delivery in result.deliveries] == ["send_failed", None]
    assert sender.send_discord_payload.await_count == 2
    assert sender.send_discord_payload.await_args_list[0].args[0] is channel_a
    assert sender.send_discord_payload.await_args_list[1].args[0] is channel_b


@pytest.mark.asyncio
async def test_missing_channel_records_channel_not_found() -> None:
    sender = MagicMock()
    sender.send_discord_payload = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = None
    pipeline = MediaPipeline(sender, client, [111])

    result = await pipeline.send_discord_batch(
        [{
            "discord_buffer": io.BytesIO(b"x"),
            "filename": "a.jpg",
            "content_hash": "hash-a",
        }],
        title="title",
        link=None,
    )

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
    sender.send_discord_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_success_acknowledges_failed_discord() -> None:
    sender = MagicMock()
    sender.telegram_chat_id = "telegram-chat"
    sender.send_discord_payload = AsyncMock(return_value=discord_delivery(
        "111", ("hash-a",), DeliveryOutcome.FAILED
    ))
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
    sender.send_discord_payload = AsyncMock(return_value=discord_delivery(
        "111", ("hash-a",), DeliveryOutcome.FAILED
    ))
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
async def test_discord_payload_is_rebuilt_and_buffers_are_rewound_per_channel() -> None:
    sender = MagicMock()
    observed_payloads = []

    async def consume_payload(
        channel,
        items,
        *,
        files,
        embeds,
        destination_id,
        requested_media,
    ):
        observed_payloads.append((
            [discord_file.fp.read() for discord_file in files],
            tuple(id(discord_file) for discord_file in files),
            tuple(id(embed) for embed in embeds),
        ))
        return discord_delivery(destination_id, requested_media)

    sender.send_discord_payload = AsyncMock(side_effect=consume_payload)
    client = MagicMock()
    client.get_channel.side_effect = [MagicMock(), MagicMock()]
    pipeline = MediaPipeline(sender, client, [111, 222])
    batch = [
        {"discord_buffer": io.BytesIO(b"first"), "filename": "first.jpg", "content_hash": "h1"},
        {"discord_buffer": io.BytesIO(b"second"), "filename": "second.jpg", "content_hash": "h2"},
    ]

    result = await pipeline.send_discord_batch(
        batch,
        title="title",
        link="https://example.com/1",
    )

    assert result.acknowledged is True
    assert observed_payloads[0][0] == [b"first", b"second"]
    assert observed_payloads[1][0] == [b"first", b"second"]
    assert observed_payloads[0][1] != observed_payloads[1][1]
    assert observed_payloads[0][2] != observed_payloads[1][2]


@pytest.mark.asyncio
async def test_dc_distribution_preserves_per_image_telegram_calls_and_delay() -> None:
    sender = MagicMock()
    sender.telegram_chat_id = "telegram-chat"
    sender.send_discord_payload = AsyncMock(side_effect=successful_discord_payload)
    sender.send_to_telegram = AsyncMock(return_value=True)
    channel = MagicMock()
    client = MagicMock()
    client.get_channel.return_value = channel
    pipeline = MediaPipeline(sender, client, [111], source_label="디시인사이드")
    first_telegram = io.BytesIO(b"telegram-1")
    second_telegram = io.BytesIO(b"telegram-2")
    images = [
        {
            "discord_buffer": io.BytesIO(b"discord-1"),
            "telegram_buffer": first_telegram,
            "filename": "first.jpg",
            "is_gif": False,
            "content_hash": "h1",
            "validated": True,
        },
        {
            "discord_buffer": io.BytesIO(b"discord-2"),
            "telegram_buffer": second_telegram,
            "filename": "second.gif",
            "is_gif": True,
            "content_hash": "h2",
            "validated": True,
        },
    ]

    with patch("Module.media_pipeline.asyncio.sleep", AsyncMock()) as sleep:
        result = await pipeline.distribute(
            images,
            title="title",
            link="https://example.com/1",
            inter_image_delay=1.0,
        )

    assert result.acknowledged is True
    assert sender.send_discord_payload.await_count == 2
    assert sender.send_discord_payload.await_args_list[0].args[1] == [images[0]]
    assert sender.send_discord_payload.await_args_list[1].args[1] == [images[1]]
    first_embed = sender.send_discord_payload.await_args_list[0].kwargs["embeds"][0]
    second_embed = sender.send_discord_payload.await_args_list[1].kwargs["embeds"][0]
    assert first_embed.title == "title"
    assert first_embed.url == "https://example.com/1"
    assert second_embed.title is None
    assert second_embed.url is None
    assert sender.send_to_telegram.await_args_list == [
        call(first_telegram, "first.jpg", False, validated=True),
        call(second_telegram, "second.gif", True, validated=True),
    ]
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_dc_web_enqueue_is_independent_of_delivery_ack() -> None:
    sender = MagicMock()
    sender.telegram_chat_id = "telegram-chat"
    sender.send_discord_payload = AsyncMock(return_value=discord_delivery(
        "111", ("hash-a",), DeliveryOutcome.FAILED
    ))
    sender.send_to_telegram = AsyncMock(return_value=False)
    client = MagicMock()
    client.get_channel.return_value = MagicMock()
    pipeline = MediaPipeline(sender, client, [111], web_gallery_enabled=True)
    web_delivery = ChannelDelivery(
        transport="web_gallery",
        destination_id="gallery",
        outcome=DeliveryOutcome.QUEUED,
        requested_media=("hash-a",),
        delivered_media=(),
        ack_eligible=False,
    )
    pipeline.attach_to_web_gallery = AsyncMock(return_value=DeliveryResult((web_delivery,)))

    result = await pipeline.distribute([{
        "discord_buffer": io.BytesIO(b"discord"),
        "telegram_buffer": io.BytesIO(b"telegram"),
        "filename": "a.jpg",
        "is_gif": False,
        "content_hash": "hash-a",
        "validated": True,
    }])

    pipeline.attach_to_web_gallery.assert_awaited_once()
    assert result.deliveries[-1] is web_delivery
    assert result.acknowledged is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "delivered_media", "expected_web_calls"),
    [
        (DeliveryOutcome.SUCCEEDED, ("h1", "h2"), 2),
        # PARTIAL fallback에서 실제 전달된 media만 웹 갤러리에 적재 (회귀 방지)
        (DeliveryOutcome.PARTIAL, ("h1",), 1),
        (DeliveryOutcome.FAILED, (), 0),
    ],
)
async def test_arca_web_enqueue_requires_successful_discord_batch(
    outcome,
    delivered_media,
    expected_web_calls,
) -> None:
    sender = MagicMock()
    sender.send_discord_payload = AsyncMock(return_value=discord_delivery(
        "111",
        ("h1", "h2"),
        outcome,
        delivered_media,
    ))
    client = MagicMock()
    client.get_channel.return_value = MagicMock()
    pipeline = MediaPipeline(
        sender,
        client,
        [111],
        web_gallery_enabled=True,
        telegram_enabled=False,
        web_publish_requires_discord_success=True,
    )
    pipeline.attach_to_web_gallery = AsyncMock(return_value=DeliveryResult((
        ChannelDelivery(
            transport="web_gallery",
            destination_id="gallery",
            outcome=DeliveryOutcome.QUEUED,
            requested_media=("web",),
            delivered_media=(),
            ack_eligible=False,
        ),
    )))
    batch = [
        {
            "discord_buffer": io.BytesIO(b"first"),
            "filename": "first.jpg",
            "content_hash": "h1",
        },
        {
            "discord_buffer": io.BytesIO(b"second"),
            "filename": "second.jpg",
            "content_hash": "h2",
        },
    ]

    result = await pipeline.send_discord_batch(
        batch,
        title="title",
        link="https://arca.live/1",
    )

    assert pipeline.attach_to_web_gallery.await_count == expected_web_calls
    assert result.acknowledged is (outcome is DeliveryOutcome.SUCCEEDED)


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
