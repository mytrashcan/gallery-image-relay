import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from PIL import Image

from Module.delivery_result import DeliveryOutcome, DeliveryResult
from Module.image_handler import ImageHandler
from Module.media_pipeline import PreparedMedia
from Module.message_sender import MessageSender


def make_large_png_buffer(size=(800, 800)):
    """JPEG 변환/축소로 확실히 줄어드는 노이즈 PNG 버퍼 생성"""
    img = Image.effect_noise(size, 100).convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def make_413_exception():
    response = SimpleNamespace(status=413, reason="Payload Too Large")
    return discord.HTTPException(response, "Request entity too large")


class FakeChannel:
    """첫 send에서 지정된 예외를 던지고 이후 성공하는 채널 목"""

    def __init__(self, fail_times=1, exception=None, filesize_limit=None):
        self.sent_files = []
        self._fail_times = fail_times
        self._exception = exception
        if filesize_limit is not None:
            self.guild = SimpleNamespace(filesize_limit=filesize_limit)

    async def send(self, file=None, embed=None):
        if self._fail_times > 0 and self._exception is not None:
            self._fail_times -= 1
            raise self._exception
        self.sent_files.append(file.fp.read())


class SingleBatch413Channel:
    def __init__(self):
        self.send_calls = 0
        self.sent_files = []

    async def send(self, *, file=None, embed=None, files=None, embeds=None):
        self.send_calls += 1
        if files is not None:
            raise make_413_exception()
        self.sent_files.append(file.fp.read())


def make_sender(with_handler=True):
    handler = ImageHandler() if with_handler else None
    return MessageSender("123456:TEST-TOKEN", "0", image_handler=handler)


def prepared_media(buffer: io.BytesIO, filename: str, *, validated: bool = True) -> PreparedMedia:
    return PreparedMedia(
        discord_buffer=buffer,
        telegram_buffer=io.BytesIO(buffer.getvalue()),
        filename=filename,
        content_hash=filename,
        is_gif=False,
        original_data=b"",
        validated=validated,
    )


def make_batch_payload():
    items = [
        prepared_media(io.BytesIO(b"first"), "first.jpg"),
        prepared_media(io.BytesIO(b"second"), "second.jpg"),
    ]
    files = [discord.File(item.discord_buffer, filename=item.filename) for item in items]
    embeds = [discord.Embed(), discord.Embed()]
    return items, files, embeds


def make_http_exception(status, reason):
    response = SimpleNamespace(status=status, reason=reason)
    return discord.HTTPException(response, reason)


class TestDiscord413Fallback:
    def test_413_recompresses_and_retries(self):
        sender = make_sender()
        channel = FakeChannel(fail_times=1, exception=make_413_exception())
        buffer = make_large_png_buffer()
        original_size = len(buffer.getvalue())

        result = asyncio.run(sender.send_to_discord(channel, "title", buffer, "test.png"))

        assert result is True
        assert len(channel.sent_files) == 1
        assert len(channel.sent_files[0]) < original_size

    def test_413_uses_guild_filesize_limit_as_target(self):
        sender = make_sender()
        buffer = make_large_png_buffer()
        original_size = len(buffer.getvalue())
        limit = original_size // 4
        channel = FakeChannel(fail_times=1, exception=make_413_exception(), filesize_limit=limit)

        result = asyncio.run(sender.send_to_discord(channel, "title", buffer, "test.png"))

        assert result is True
        assert len(channel.sent_files[0]) <= limit

    def test_413_without_image_handler_fails_without_retry(self):
        sender = make_sender(with_handler=False)
        channel = FakeChannel(fail_times=99, exception=make_413_exception())
        buffer = make_large_png_buffer()

        result = asyncio.run(sender.send_to_discord(channel, "title", buffer, "test.png"))

        assert result is False
        assert channel.sent_files == []

    def test_single_item_batch_413_recompresses_without_resending_original(self):
        sender = make_sender()
        channel = SingleBatch413Channel()
        buffer = make_large_png_buffer()
        original_size = len(buffer.getvalue())
        items = [prepared_media(buffer, "test.png")]
        files = [discord.File(buffer, filename="test.png")]

        delivery = asyncio.run(sender.send_discord_payload(
            channel,
            items,
            files=files,
            embeds=[discord.Embed()],
            destination_id="123",
            requested_media=("hash",),
        ))

        assert delivery.outcome is DeliveryOutcome.SUCCEEDED
        assert channel.send_calls == 2
        assert len(channel.sent_files) == 1
        assert len(channel.sent_files[0]) < original_size

    def test_batch_413_fallback_complete_success(self):
        sender = make_sender()
        channel = SimpleNamespace(
            send=AsyncMock(side_effect=[make_413_exception(), None, None])
        )
        items, files, embeds = make_batch_payload()

        with patch("Module.message_sender.asyncio.sleep", AsyncMock()):
            delivery = asyncio.run(sender.send_discord_payload(
                channel,
                items,
                files=files,
                embeds=embeds,
                destination_id="123",
                requested_media=("h1", "h2"),
            ))

        assert delivery.outcome is DeliveryOutcome.SUCCEEDED
        assert delivery.delivered_media == ("h1", "h2")
        assert DeliveryResult((delivery,)).acknowledged is True
        assert channel.send.await_count == 3

    def test_batch_413_fallback_recompresses_an_oversized_item(self):
        sender = make_sender()
        channel = SimpleNamespace(
            send=AsyncMock(side_effect=[
                make_413_exception(),
                make_413_exception(),
                None,
                None,
            ])
        )
        large_buffer = make_large_png_buffer()
        original_size = len(large_buffer.getvalue())
        items = [
            prepared_media(large_buffer, "large.png"),
            prepared_media(io.BytesIO(b"second"), "second.jpg"),
        ]
        files = [discord.File(item.discord_buffer, filename=item.filename) for item in items]
        embeds = [discord.Embed(), discord.Embed()]

        with patch("Module.message_sender.asyncio.sleep", AsyncMock()):
            delivery = asyncio.run(sender.send_discord_payload(
                channel,
                items,
                files=files,
                embeds=embeds,
                destination_id="123",
                requested_media=("h1", "h2"),
            ))

        recompressed_file = channel.send.await_args_list[2].kwargs["file"]
        assert delivery.outcome is DeliveryOutcome.SUCCEEDED
        assert len(recompressed_file.fp.getvalue()) < original_size
        assert channel.send.await_count == 4

    def test_batch_413_fallback_partial_failure_does_not_ack(self):
        sender = make_sender()
        channel = SimpleNamespace(
            send=AsyncMock(side_effect=[
                make_413_exception(),
                None,
                make_http_exception(500, "send failed"),
            ])
        )
        items, files, embeds = make_batch_payload()

        with patch("Module.message_sender.asyncio.sleep", AsyncMock()):
            delivery = asyncio.run(sender.send_discord_payload(
                channel,
                items,
                files=files,
                embeds=embeds,
                destination_id="123",
                requested_media=("h1", "h2"),
            ))

        assert delivery.outcome is DeliveryOutcome.PARTIAL
        assert delivery.delivered_media == ("h1",)
        assert DeliveryResult((delivery,)).acknowledged is False
        assert channel.send.await_count == 3

    def test_batch_413_fallback_total_failure(self):
        sender = make_sender()
        channel = SimpleNamespace(
            send=AsyncMock(side_effect=[
                make_413_exception(),
                make_http_exception(500, "first failed"),
                make_http_exception(500, "second failed"),
            ])
        )
        items, files, embeds = make_batch_payload()

        with patch("Module.message_sender.asyncio.sleep", AsyncMock()):
            delivery = asyncio.run(sender.send_discord_payload(
                channel,
                items,
                files=files,
                embeds=embeds,
                destination_id="123",
                requested_media=("h1", "h2"),
            ))

        assert delivery.outcome is DeliveryOutcome.FAILED
        assert delivery.delivered_media == ()
        assert DeliveryResult((delivery,)).acknowledged is False
        assert channel.send.await_count == 3

    def test_non_413_http_error_is_not_retried(self):
        response = SimpleNamespace(status=403, reason="Forbidden")
        exc = discord.HTTPException(response, "Missing permissions")
        sender = make_sender()
        channel = FakeChannel(fail_times=99, exception=exc)
        buffer = make_large_png_buffer()

        result = asyncio.run(sender.send_to_discord(channel, "title", buffer, "test.png"))

        assert result is False
        assert channel.sent_files == []


class TestConfigDiscordMaxSize:
    def test_default_is_10mb(self):
        from Module.config import DISCORD_MAX_SIZE

        assert DISCORD_MAX_SIZE == 10 * 1024 * 1024

    def test_env_override(self, monkeypatch):
        import importlib

        from Module import config

        monkeypatch.setenv("DISCORD_MAX_SIZE_MB", "50")
        importlib.reload(config)
        try:
            assert config.DISCORD_MAX_SIZE == 50 * 1024 * 1024
        finally:
            # 다른 테스트에 영향 없도록 env 복원 후 재로드
            monkeypatch.delenv("DISCORD_MAX_SIZE_MB", raising=False)
            importlib.reload(config)
