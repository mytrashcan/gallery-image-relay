"""Smoke tests for DCBot instantiation and basic process_post logic.

DCBot inherits discord.Client, so we monkeypatch the crawler and image_handler
to avoid real network/Discord dependencies.
"""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Module.dcbot import DCBot
from Module.delivery_result import ChannelDelivery, DeliveryOutcome, DeliveryResult


async def successful_discord_payload(
    channel,
    items,
    *,
    files,
    embeds,
    destination_id,
    requested_media,
    on_delivered=None,
):
    return ChannelDelivery(
        transport="discord",
        destination_id=destination_id,
        outcome=DeliveryOutcome.SUCCEEDED,
        requested_media=requested_media,
        delivered_media=requested_media,
        ack_eligible=True,
    )


@pytest.fixture
def mock_dependencies():
    """Return (mock_crawler_class, mock_image_handler, mock_message_sender) patches."""
    crawler_mock = MagicMock()
    image_handler_mock = MagicMock()

    with (
        patch("Module.dcbot.DCInsideCrawler", return_value=crawler_mock),
        patch("Module.dcbot.ImageHandler", return_value=image_handler_mock),
    ):
        yield crawler_mock, image_handler_mock


@pytest.fixture
def bot(mock_dependencies):
    """Build a DCBot whose crawler / image_handler / message_sender are all mocks."""
    import discord

    intents = discord.Intents.default()
    b = DCBot(
        token="fake-token",
        base_url="https://gall.dcinside.com/mgallery/board/lists/?id=test",
        channel_ids=["123456789"],
        telegram_token="fake-telegram-token",
        telegram_chat_id="fake-chat-id",
        intents=intents,
    )
    # Replace get_channel so Discord transport doesn't need a real channel.
    b.get_channel = MagicMock(return_value=MagicMock())
    # Make async methods on message_sender actually awaitable
    b.message_sender.send_to_discord = AsyncMock()
    b.message_sender.send_discord_payload = AsyncMock(side_effect=successful_discord_payload)
    b.message_sender.send_to_telegram = AsyncMock()
    return b


@pytest.mark.asyncio
async def test_dcbot_instantiation(bot):
    """Verify the bot can be instantiated without errors."""
    assert bot.token == "fake-token"
    assert len(bot.channel_ids) == 1
    assert bot.crawler is not None
    assert bot.image_handler is not None
    assert bot.message_sender is not None


def test_gallery_process_shares_one_archive_between_dedup_layers() -> None:
    import discord

    archive = MagicMock()
    base_url = "https://gall.dcinside.com/mgallery/board/lists/?id=test"
    with (
        patch("Module.dcbot.DeliveryArchive", return_value=archive) as archive_class,
        patch("Module.dcbot.DCInsideCrawler") as crawler_class,
        patch("Module.dcbot.ImageHandler") as handler_class,
    ):
        DCBot(
            token="fake-token",
            base_url=base_url,
            channel_ids=[],
            telegram_token="fake-telegram-token",
            telegram_chat_id="fake-chat-id",
            intents=discord.Intents.default(),
            gallery_name="cats",
        )

    archive_class.assert_called_once()
    crawler_class.assert_called_once_with(
        base_url,
        gallery_name="cats",
        delivery_archive=archive,
    )
    handler_class.assert_called_once_with(
        source="dcinside",
        gallery_name="cats",
        delivery_archive=archive,
    )


@pytest.mark.asyncio
async def test_setup_hook_starts_only_one_crawler_task(bot):
    """The reconnect-safe crawler task is created once."""
    bot._run_crawler = AsyncMock()

    await bot.setup_hook()
    task = bot._crawler_task
    await bot.setup_hook()
    await task

    assert bot._crawler_task is task
    bot._run_crawler.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_ready_does_not_start_another_crawler_loop(bot):
    """Discord may emit on_ready repeatedly after reconnects."""
    bot.start_crawling = AsyncMock()

    await bot.on_ready()
    await bot.on_ready()

    bot.start_crawling.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_post_with_images(mock_dependencies, bot):
    """process_post downloads images and calls Discord batch + Telegram transports."""
    _, image_handler_mock = mock_dependencies
    image_handler_mock.download_images.return_value = [
        (io.BytesIO(b"discord"), io.BytesIO(b"telegram"), "photo.png", False, b"original", "hash"),
    ]

    post = {"title": "Test Post", "link": "https://example.com/post/1", "has_image": True}
    await bot.process_post(post)

    image_handler_mock.download_images.assert_called_once_with(post["link"])
    bot.message_sender.send_discord_payload.assert_awaited_once()
    bot.message_sender.send_to_telegram.assert_called_once()
    image_handler_mock.mark_hash_sent.assert_called_once_with("hash")


@pytest.mark.asyncio
async def test_process_post_no_images(mock_dependencies, bot):
    """process_post returns early when no images are found."""
    _, image_handler_mock = mock_dependencies
    image_handler_mock.download_images.return_value = []

    post = {"title": "No Img Post", "link": "https://example.com/post/2"}
    await bot.process_post(post)

    image_handler_mock.download_images.assert_called_once()
    bot.message_sender.send_discord_payload.assert_not_called()
    bot.message_sender.send_to_telegram.assert_not_called()


@pytest.mark.asyncio
async def test_start_crawling_retries_on_error(mock_dependencies, bot):
    """start_crawling does not crash on transient errors; it logs and continues.

    The while True loop never exits, so we use asyncio.wait_for with a short
    timeout to verify it runs through at least one error cycle.
    """
    crawler_mock, _ = mock_dependencies
    crawler_mock.get_latest_post.side_effect = Exception("Transient network error")

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bot.start_crawling(), timeout=0.1)

    assert crawler_mock.get_latest_post.call_count >= 1


@pytest.mark.asyncio
async def test_start_crawling_processes_post_with_image(mock_dependencies, bot):
    """start_crawling calls process_post when get_latest_post returns an image post."""
    crawler_mock, image_handler_mock = mock_dependencies

    post = {
        "title": "Gallery Post",
        "link": "https://example.com/post/3",
        "post_id": "3",
        "has_image": True,
    }
    crawler_mock.get_latest_post.return_value = post

    image_handler_mock.download_images.return_value = [
        (io.BytesIO(b"discord"), io.BytesIO(b"telegram"), "img.png", False, b"original", "hash"),
    ]

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bot.start_crawling(), timeout=0.1)

    image_handler_mock.download_images.assert_called_with(post["link"])
    bot.message_sender.send_discord_payload.assert_awaited()
    crawler_mock.mark_sent.assert_called_with("3")


@pytest.mark.asyncio
async def test_cache_command_only_leader_sends_ack(mock_dependencies, bot):
    _, image_handler_mock = mock_dependencies
    bot._command_leader.is_leader = False
    bot._command_leader.try_acquire = MagicMock(return_value=False)
    message = MagicMock()
    message.author = object()
    message.content = "!쓰담쓰담"
    message.channel.send = AsyncMock()

    await bot.on_message(message)

    image_handler_mock.clear_seen_hashes.assert_called_once()
    message.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_delivery_is_not_acknowledged(mock_dependencies, bot):
    crawler_mock, _ = mock_dependencies
    post = {"title": "retry", "link": "https://example.com/4", "post_id": "4", "has_image": True}
    crawler_mock.get_latest_post.return_value = post
    bot.process_post = AsyncMock(return_value=False)

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bot.start_crawling(), timeout=0.05)

    crawler_mock.mark_sent.assert_not_called()


@pytest.mark.asyncio
async def test_failed_distribution_does_not_acknowledge_image_hash(mock_dependencies, bot):
    _, image_handler_mock = mock_dependencies
    image_handler_mock.download_images.return_value = [
        (io.BytesIO(b"discord"), io.BytesIO(b"telegram"), "img.png", False, b"original", "hash"),
    ]
    bot.media_pipeline.distribute = AsyncMock(return_value=DeliveryResult((
        ChannelDelivery(
            transport="discord",
            destination_id="123456789",
            outcome=DeliveryOutcome.FAILED,
            requested_media=("hash",),
            delivered_media=(),
            ack_eligible=True,
            reason="send_failed",
        ),
    )))

    assert await bot.process_post({"title": "x", "link": "https://example.com"}) is False
    image_handler_mock.mark_hash_sent.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_success_does_not_acknowledge_when_discord_fails(mock_dependencies, bot):
    _, image_handler_mock = mock_dependencies
    image_handler_mock.download_images.return_value = [
        (io.BytesIO(b"discord"), io.BytesIO(b"telegram"), "img.png", False, b"original", "hash"),
    ]
    bot.message_sender.send_discord_payload.side_effect = None
    bot.message_sender.send_discord_payload.return_value = ChannelDelivery(
        transport="discord",
        destination_id="123456789",
        outcome=DeliveryOutcome.FAILED,
        requested_media=("hash",),
        delivered_media=(),
        ack_eligible=True,
        reason="send_failed",
    )
    bot.message_sender.send_to_telegram.return_value = True

    acknowledged = await bot.process_post({"title": "x", "link": "https://example.com"})

    assert acknowledged is False
    bot.message_sender.send_discord_payload.assert_awaited_once()
    bot.message_sender.send_to_telegram.assert_awaited_once()
    image_handler_mock.mark_hash_sent.assert_not_called()


@pytest.mark.asyncio
async def test_all_destination_failures_do_not_acknowledge_hash(mock_dependencies, bot):
    _, image_handler_mock = mock_dependencies
    image_handler_mock.download_images.return_value = [
        (io.BytesIO(b"discord"), io.BytesIO(b"telegram"), "img.png", False, b"original", "hash"),
    ]
    bot.message_sender.send_discord_payload.side_effect = None
    bot.message_sender.send_discord_payload.return_value = ChannelDelivery(
        transport="discord",
        destination_id="123456789",
        outcome=DeliveryOutcome.FAILED,
        requested_media=("hash",),
        delivered_media=(),
        ack_eligible=True,
        reason="send_failed",
    )
    bot.message_sender.send_to_telegram.return_value = False

    acknowledged = await bot.process_post({"title": "x", "link": "https://example.com"})

    assert acknowledged is False
    bot.message_sender.send_discord_payload.assert_awaited_once()
    bot.message_sender.send_to_telegram.assert_awaited_once()
    image_handler_mock.mark_hash_sent.assert_not_called()
