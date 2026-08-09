"""Smoke tests for ArcaBot instantiation and basic process_post logic.

ArcaBot inherits discord.Client, so we monkeypatch the crawler and image_handler
to avoid real network/Discord dependencies.
"""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from PIL import Image

from Module.arca_bot import ArcaBot
from Module.delivery_result import ChannelDelivery, DeliveryOutcome, DeliveryResult
from Module.media_candidate import MediaCandidate
from Module.media_download import MediaDownloadTooLarge


@pytest.fixture
def mock_dependencies():
    """Return (mock_crawler, mock_image_handler, mock_message_sender) instances."""
    crawler_mock = MagicMock()
    image_handler_mock = MagicMock()
    message_sender_mock = MagicMock()

    with (
        patch("Module.arca_bot.ArcaliveCrawler", return_value=crawler_mock),
        patch("Module.arca_bot.ImageHandler", return_value=image_handler_mock),
        patch("Module.arca_bot.MessageSender", return_value=message_sender_mock),
    ):
        yield crawler_mock, image_handler_mock, message_sender_mock


@pytest.fixture
def bot(mock_dependencies):
    """Build an ArcaBot whose dependencies are all mocked.

    We also patch get_channel so internal calls don't need a real Discord connection.
    """
    intents = discord.Intents.default()
    b = ArcaBot(
        token="fake-token",
        base_url="https://arca.live/b/test",
        channel_ids=["123456789"],
        intents=intents,
    )
    # Replace get_channel so _send_image_batch doesn't need real channel
    b.get_channel = MagicMock(return_value=MagicMock())
    return b


@pytest.mark.asyncio
async def test_arca_bot_instantiation(bot):
    """Verify the ArcaBot can be instantiated without errors."""
    assert bot.token == "fake-token"
    assert len(bot.channel_ids) == 1
    assert bot.crawler is not None
    assert bot.image_handler is not None
    assert bot.message_sender is not None
    assert bot.media_pipeline.telegram_enabled is False
    assert bot.media_pipeline.source_label == "아카라이브"
    assert bot.media_pipeline.web_publish_requires_discord_success is True


def test_gallery_process_shares_one_archive_between_dedup_layers() -> None:
    archive = MagicMock()
    base_url = "https://arca.live/b/test"
    with (
        patch("Module.arca_bot.DeliveryArchive", return_value=archive) as archive_class,
        patch("Module.arca_bot.ArcaliveCrawler") as crawler_class,
        patch("Module.arca_bot.ImageHandler") as handler_class,
        patch("Module.arca_bot.MessageSender"),
    ):
        ArcaBot(
            token="fake-token",
            base_url=base_url,
            channel_ids=[],
            intents=discord.Intents.default(),
            gallery_name="genshin",
        )

    archive_class.assert_called_once()
    crawler_class.assert_called_once_with(
        base_url,
        gallery_name="genshin",
        delivery_archive=archive,
    )
    handler_class.assert_called_once_with(
        source="arcalive",
        gallery_name="genshin",
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
    """process_post extracts images, downloads them, and sends embeds.

    We mock extract_all_images and _download_and_process to verify the flow.
    """
    crawler_mock, _, _ = mock_dependencies
    crawler_mock.extract_all_images.return_value = [
        MediaCandidate("https://ac-o.namu.la/1.jpg", filename_hint="1.jpg"),
        MediaCandidate("https://ac-o.namu.la/2.jpg", filename_hint="2.jpg"),
    ]
    # Mock _download_and_process to return processed items
    bot._download_and_process = AsyncMock(
        return_value=([
            {"discord_buffer": MagicMock(), "telegram_buffer": MagicMock(),
             "filename": "1.jpg", "is_gif": False, "content_hash": "h1"},
            {"discord_buffer": MagicMock(), "telegram_buffer": MagicMock(),
             "filename": "2.jpg", "is_gif": False, "content_hash": "h2"},
        ], True)
    )
    bot._send_image_batch = AsyncMock(return_value=DeliveryResult((
        ChannelDelivery(
            transport="discord",
            destination_id="123456789",
            outcome=DeliveryOutcome.SUCCEEDED,
            requested_media=("h1", "h2"),
            delivered_media=("h1", "h2"),
            ack_eligible=True,
        ),
    )))

    post = {"title": "Arca Post", "link": "https://arca.live/b/test/1"}
    await bot.process_post(post)

    crawler_mock.extract_all_images.assert_called_once_with(post["link"])
    bot._download_and_process.assert_called_once()
    bot._send_image_batch.assert_called_once()


@pytest.mark.asyncio
async def test_process_post_no_images(mock_dependencies, bot):
    """process_post returns early when no images are extracted."""
    crawler_mock, _, _ = mock_dependencies
    crawler_mock.extract_all_images.return_value = []

    post = {"title": "No Img", "link": "https://arca.live/b/test/2"}
    result = await bot.process_post(post)

    crawler_mock.extract_all_images.assert_called_once()
    assert result is True


@pytest.mark.asyncio
async def test_process_post_retries_when_post_detail_fetch_fails(mock_dependencies, bot):
    crawler_mock, _, _ = mock_dependencies
    crawler_mock.extract_all_images.return_value = None

    post = {"title": "Unavailable", "link": "https://arca.live/b/test/3"}
    result = await bot.process_post(post)

    crawler_mock.extract_all_images.assert_called_once_with(post["link"])
    assert result is False


@pytest.mark.asyncio
async def test_concurrent_download_results_are_deduplicated_within_post(bot):
    first = {
        "discord_buffer": MagicMock(),
        "telegram_buffer": MagicMock(),
        "filename": "first.jpg",
        "is_gif": False,
        "content_hash": "same-hash",
    }
    duplicate = {**first, "filename": "duplicate.jpg"}
    bot._download_and_process_one = AsyncMock(
        side_effect=[(first, True), (duplicate, True)]
    )

    downloaded, all_resolved = await bot._download_and_process(
        [
            MediaCandidate(
                "https://ac-o.namu.la/1.jpg",
                filename_hint="first.jpg",
            ),
            MediaCandidate(
                "https://ac-o.namu.la/2.jpg",
                filename_hint="duplicate.jpg",
            ),
        ],
        "https://arca.live/b/test/1",
    )

    assert downloaded == [first]
    assert all_resolved is True


@pytest.mark.asyncio
async def test_start_crawling_retries_on_error(mock_dependencies, bot):
    """start_crawling does not crash when crawler.get_latest_posts raises.

    Uses asyncio.wait_for with a timeout because the loop is infinite.
    """
    crawler_mock, _, _ = mock_dependencies
    crawler_mock.get_latest_posts.side_effect = Exception("Transient error")

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bot.start_crawling(), timeout=0.1)

    assert crawler_mock.get_latest_posts.call_count >= 1


@pytest.mark.asyncio
async def test_start_crawling_processes_posts(mock_dependencies, bot):
    """start_crawling calls process_post for each new post returned."""
    crawler_mock, _, _ = mock_dependencies
    posts = [
        {"title": "Post 1", "link": "https://arca.live/b/test/10", "post_id": "10"},
        {"title": "Post 2", "link": "https://arca.live/b/test/11", "post_id": "11"},
    ]
    crawler_mock.get_latest_posts.side_effect = [posts, posts, posts]

    bot.process_post = AsyncMock()

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bot.start_crawling(), timeout=0.1)

    assert bot.process_post.call_count >= 2
    bot.process_post.assert_any_call(posts[0])
    bot.process_post.assert_any_call(posts[1])


@pytest.mark.asyncio
async def test_download_single_image_success(mock_dependencies, bot):
    """_download_single_image downloads bytes via requests and returns them."""

    output = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(output, format="PNG")
    resp = MagicMock()
    resp.content = output.getvalue()
    resp.headers = {"content-length": str(len(resp.content))}
    resp.iter_content.return_value = [resp.content]
    resp.raise_for_status.return_value = None

    with patch("Module.arca_bot.requests.get", return_value=resp) as mock_get:
        result = bot._download_single_image(
            MediaCandidate("https://ac-o.namu.la/1.jpg"),
            "https://arca.live/b/test/1",
        )

    assert result.data == resp.content
    assert result.filename == "1.png"
    mock_get.assert_called_once_with(
        "https://ac-o.namu.la/1.jpg",
        headers={"Referer": "https://arca.live/b/test/1"},
        timeout=15,
        stream=True,
    )


@pytest.mark.asyncio
async def test_download_single_image_failure(mock_dependencies, bot):
    """_download_single_image returns None on request failure."""
    import requests

    with patch(
        "Module.arca_bot.requests.get",
        side_effect=requests.RequestException("timeout"),
    ):
        result = bot._download_single_image(
            MediaCandidate("https://ac-o.namu.la/fail.jpg"),
            "https://arca.live/",
        )

    assert result is None


@pytest.mark.asyncio
async def test_download_attempt_waits_after_failure(bot):
    """A fast CDN failure still occupies its rate-limited download slot."""
    bot._download_single_image = MagicMock(return_value=None)

    with patch("Module.arca_bot.asyncio.sleep", AsyncMock()) as mock_sleep:
        result = await bot._download_and_process_one(
            MediaCandidate(
                "https://ac-o.namu.la/fail.jpg",
                filename_hint="fail.jpg",
            ),
            "https://arca.live/b/test/1",
        )

    assert result == (None, False)
    mock_sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_permanently_rejected_download_is_resolved(bot):
    bot._download_single_image = MagicMock(
        side_effect=MediaDownloadTooLarge("too large")
    )

    with patch("Module.arca_bot.asyncio.sleep", AsyncMock()):
        result = await bot._download_and_process_one(
            MediaCandidate(
                "https://ac-o.namu.la/large.jpg",
                filename_hint="large.jpg",
            ),
            "https://arca.live/b/test/1",
        )

    assert result == (None, True)


@pytest.mark.asyncio
async def test_failed_post_delivery_is_not_acknowledged(mock_dependencies, bot):
    crawler_mock, _, _ = mock_dependencies
    post = {"title": "retry", "link": "https://arca.live/b/test/12", "post_id": "12"}
    crawler_mock.get_latest_posts.return_value = [post]
    bot.process_post = AsyncMock(return_value=False)

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bot.start_crawling(), timeout=0.05)

    crawler_mock.mark_sent.assert_not_called()


@pytest.mark.asyncio
async def test_unresolved_media_prevents_post_ack_after_successful_delivery(mock_dependencies, bot):
    crawler_mock, image_handler_mock, _ = mock_dependencies
    crawler_mock.extract_all_images.return_value = [
        MediaCandidate("https://ac-o.namu.la/1.jpg", filename_hint="1.jpg"),
        MediaCandidate("https://ac-o.namu.la/2.jpg", filename_hint="2.jpg"),
    ]
    item = {
        "discord_buffer": io.BytesIO(b"image"),
        "telegram_buffer": io.BytesIO(b"image"),
        "filename": "1.jpg",
        "is_gif": False,
        "content_hash": "h1",
    }
    bot._download_and_process = AsyncMock(return_value=([item], False))
    bot._send_image_batch = AsyncMock(return_value=DeliveryResult((
        ChannelDelivery(
            transport="discord",
            destination_id="123456789",
            outcome=DeliveryOutcome.SUCCEEDED,
            requested_media=("h1",),
            delivered_media=("h1",),
            ack_eligible=True,
        ),
    )))

    acknowledged = await bot.process_post({"title": "x", "link": "https://arca.live/b/test/13"})

    assert acknowledged is False
    image_handler_mock.mark_hash_sent.assert_called_once_with("h1")


@pytest.mark.asyncio
async def test_missing_arca_channel_records_reason_without_send(bot):
    bot.get_channel.return_value = None
    bot.message_sender.send_discord_payload = AsyncMock()
    batch = [{
        "discord_buffer": io.BytesIO(b"image"),
        "filename": "1.jpg",
        "content_hash": "h1",
    }]

    result = await bot._send_image_batch(batch, "title", "https://arca.live/b/test/14", 0)

    assert result.deliveries == (
        ChannelDelivery(
            transport="discord",
            destination_id="123456789",
            outcome=DeliveryOutcome.FAILED,
            requested_media=("h1",),
            delivered_media=(),
            ack_eligible=True,
            reason="channel_not_found",
        ),
    )
    assert result.acknowledged is False
    bot.message_sender.send_discord_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_arca_batch_delegates_to_shared_fanout_and_preserves_delay(bot):
    batch = [{
        "discord_buffer": io.BytesIO(b"first"),
        "filename": "1.jpg",
        "content_hash": "h1",
    }]
    delivery_result = DeliveryResult((
        ChannelDelivery(
            transport="discord",
            destination_id="123456789",
            outcome=DeliveryOutcome.SUCCEEDED,
            requested_media=("h1",),
            delivered_media=("h1",),
            ack_eligible=True,
        ),
    ))
    bot.media_pipeline.send_discord_batch = AsyncMock(return_value=delivery_result)

    with patch("Module.arca_bot.asyncio.sleep", AsyncMock()) as sleep:
        result = await bot._send_image_batch(
            batch,
            "title",
            "https://arca.live/b/test/15",
            10,
        )

    assert result is delivery_result
    bot.media_pipeline.send_discord_batch.assert_awaited_once_with(
        batch,
        title="title",
        link="https://arca.live/b/test/15",
        start_index=10,
    )
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_only_media_from_successful_arca_batch_are_hash_marked(
    mock_dependencies,
    bot,
    monkeypatch,
):
    crawler_mock, image_handler_mock, _ = mock_dependencies
    crawler_mock.extract_all_images.return_value = [
        MediaCandidate("https://ac-o.namu.la/1.jpg", filename_hint="1.jpg"),
        MediaCandidate("https://ac-o.namu.la/2.jpg", filename_hint="2.jpg"),
    ]
    downloaded = [
        {
            "discord_buffer": io.BytesIO(b"first"),
            "filename": "1.jpg",
            "content_hash": "h1",
        },
        {
            "discord_buffer": io.BytesIO(b"second"),
            "filename": "2.jpg",
            "content_hash": "h2",
        },
        {
            "discord_buffer": io.BytesIO(b"third"),
            "filename": "3.jpg",
            "content_hash": "h3",
        },
    ]
    bot._download_and_process = AsyncMock(return_value=(downloaded, True))
    successful_batch = DeliveryResult((
        ChannelDelivery(
            transport="discord",
            destination_id="123456789",
            outcome=DeliveryOutcome.SUCCEEDED,
            requested_media=("h1",),
            delivered_media=("h1",),
            ack_eligible=True,
        ),
    ))
    partial_batch = DeliveryResult((
        ChannelDelivery(
            transport="discord",
            destination_id="123456789",
            outcome=DeliveryOutcome.PARTIAL,
            requested_media=("h2",),
            delivered_media=("h2",),
            ack_eligible=True,
            reason="send_failed",
        ),
    ))
    failed_batch = DeliveryResult((
        ChannelDelivery(
            transport="discord",
            destination_id="123456789",
            outcome=DeliveryOutcome.FAILED,
            requested_media=("h3",),
            delivered_media=(),
            ack_eligible=True,
            reason="send_failed",
        ),
    ))
    bot._send_image_batch = AsyncMock(side_effect=[successful_batch, partial_batch, failed_batch])
    monkeypatch.setattr("Module.arca_bot.MAX_EMBEDS_PER_MSG", 1)

    acknowledged = await bot.process_post({
        "title": "title",
        "link": "https://arca.live/b/test/16",
    })

    assert acknowledged is True
    assert bot._send_image_batch.await_count == 3
    # 성공 확정된 media(h1: 전체 성공, h2: PARTIAL fallback에서 전달됨)만 hash 표시.
    # h3는 FAILED로 전달되지 않았으므로 표시하지 않는다.
    bot.image_handler.mark_hash_sent.assert_any_call("h1")
    bot.image_handler.mark_hash_sent.assert_any_call("h2")
    assert bot.image_handler.mark_hash_sent.call_count == 2
