from __future__ import annotations

import io
from unittest.mock import MagicMock

import requests
from PIL import Image

from Module.delivery_archive import DeliveryArchive, image_key
from Module.image_handler import (
    MAX_HASH_CACHE_SIZE,
    ImageHandler,
)


def make_png_bytes(size: object=(64, 64), color: object=(255, 0, 0)) -> object:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_gif_bytes(frames: object=3, size: object=(64, 64)) -> object:
    images = [Image.new("RGB", size, (i * 40, 0, 0)) for i in range(frames)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:], duration=100, loop=0)
    return buffer.getvalue()


class TestHashCache:
    def test_hash_is_unseen_until_marked_sent(self) -> None:
        handler = ImageHandler()
        assert handler.has_seen_hash("abc") is False
        handler.mark_hash_sent("abc")
        assert handler.has_seen_hash("abc") is True

    def test_sent_hash_cache_is_bounded(self) -> None:
        handler = ImageHandler()
        for i in range(MAX_HASH_CACHE_SIZE + 10):
            handler.mark_hash_sent(str(i))
        assert len(handler._seen_hashes) <= MAX_HASH_CACHE_SIZE

    def test_clear_removes_in_memory_sent_hashes(self) -> None:
        handler = ImageHandler()
        handler.mark_hash_sent("abc")
        handler.clear_seen_hashes()
        assert handler.has_seen_hash("abc") is False

    def test_archive_prevents_redelivery_after_restart(self, tmp_path) -> None:
        archive_path = tmp_path / "delivery.sqlite3"

        with DeliveryArchive(archive_path) as archive:
            first = ImageHandler(
                source="dcinside",
                gallery_name="cats",
                delivery_archive=archive,
            )
            assert first.has_seen_hash("abc") is False
            first.mark_hash_sent("abc")

        with DeliveryArchive(archive_path) as archive:
            restarted = ImageHandler(
                source="dcinside",
                gallery_name="cats",
                delivery_archive=archive,
            )

            assert restarted.has_seen_hash("abc") is True
            assert "abc" in restarted._seen_hashes
            assert archive.check("dcinside", "cats", image_key("abc")) is True


class TestProcessImage:
    def test_small_image_passes_through_unchanged(self) -> None:
        handler = ImageHandler()
        data = make_png_bytes()

        discord_buffer, telegram_buffer, is_gif = handler.process_image(data, "test.png")

        assert discord_buffer.read() == data
        assert telegram_buffer.read() == data
        assert is_gif is False

    def test_gif_detected_by_extension(self) -> None:
        handler = ImageHandler()
        data = make_gif_bytes()
        _, _, is_gif = handler.process_image(data, "test.gif")
        assert is_gif is True

    def test_gif_detected_by_magic_bytes(self) -> None:
        handler = ImageHandler()
        data = make_gif_bytes()
        _, _, is_gif = handler.process_image(data, "no_extension")
        assert is_gif is True

    def test_buffers_are_independent(self) -> None:
        handler = ImageHandler()
        data = make_png_bytes()
        discord_buffer, telegram_buffer, _ = handler.process_image(data, "test.png")

        discord_buffer.read()
        assert telegram_buffer.tell() == 0

    def test_telegram_reuses_discord_compression(self, monkeypatch: object) -> None:
        """두 제한이 같으면 압축은 한 번만 수행하고 결과를 재사용해야 함"""
        handler = ImageHandler()
        data = make_png_bytes(size=(800, 800))
        target = len(data) // 2
        monkeypatch.setattr("Module.image_handler.DISCORD_MAX_SIZE", target)
        monkeypatch.setattr("Module.image_handler.TELEGRAM_MAX_SIZE", target)

        calls = []
        original_compress = handler.compress_image

        def counting_compress(*args: object, **kwargs: object) -> object:
            calls.append(args)
            return original_compress(*args, **kwargs)

        monkeypatch.setattr(handler, "compress_image", counting_compress)

        discord_buffer, telegram_buffer, _ = handler.process_image(data, "test.png")

        assert len(calls) == 1
        assert discord_buffer.getvalue() == telegram_buffer.getvalue()
        # 재사용하더라도 버퍼는 서로 독립적이어야 함
        discord_buffer.read()
        assert telegram_buffer.tell() == 0

    def test_telegram_compresses_separately_when_discord_result_too_large(self, monkeypatch: object) -> None:
        """Discord 압축 결과가 Telegram 제한을 넘으면(부스트 서버) Telegram은 따로 압축해야 함"""
        handler = ImageHandler()
        data = b"x" * 1000
        monkeypatch.setattr("Module.image_handler.DISCORD_MAX_SIZE", 800)
        monkeypatch.setattr("Module.image_handler.TELEGRAM_MAX_SIZE", 200)

        calls = []

        def fake_compress(data_arg: object, target_arg: object, filename: object) -> object:
            calls.append(target_arg)
            result = b"y" * (target_arg - 10)
            return io.BytesIO(result), len(result)

        monkeypatch.setattr(handler, "compress_image", fake_compress)

        discord_buffer, telegram_buffer, _ = handler.process_image(data, "test.png")

        assert calls == [800, 200]
        assert len(discord_buffer.getvalue()) == 790
        assert len(telegram_buffer.getvalue()) == 190


class TestDownloadImages:
    def make_handler(self, html: str, image_data: bytes) -> ImageHandler:
        handler = ImageHandler()
        page_response = MagicMock(text=html)
        image_response = MagicMock(content=image_data)
        image_response.headers = {"content-length": str(len(image_data))}
        image_response.iter_content.return_value = [image_data]
        handler.session = MagicMock()
        handler.session.get.side_effect = [page_response, image_response]
        return handler

    def test_prefers_original_attachment_and_uses_label_as_filename(self) -> None:
        data = make_png_bytes()
        html = (
            '<div class="writing_view_box"><img src="https://dcimg8.dcinside.co.kr/inline.jpg"></div>'
            '<div class="appending_file_box"><ul><li>'
            '<a href="https://dcimg2.dcinside.com/viewimage.php?no=original">photo.png</a>'
            "</li></ul></div>"
        )
        handler = self.make_handler(html, data)

        images = handler.download_images(
            "https://gall.dcinside.com/mgallery/board/view/?id=test&no=1"
        )

        assert images[0][2] == "photo.png"
        assert handler.session.get.call_args_list[1].args[0].endswith("no=original")

    def test_falls_back_to_inline_image_when_attachment_is_missing(self) -> None:
        data = make_png_bytes()
        html = '<div class="writing_view_box"><img data-original="https://dcimg8.dcinside.co.kr/original.png"></div>'
        handler = self.make_handler(html, data)

        images = handler.download_images(
            "https://gall.dcinside.com/mgallery/board/view/?id=test&no=1"
        )

        assert images[0][2] == "original.png"
        assert handler.session.get.call_args_list[1].args[0].endswith("original.png")

    def test_falls_back_when_original_attribute_download_fails(self) -> None:
        data = make_png_bytes()
        html = (
            '<div class="writing_view_box"><img '
            'data-original="https://dcimg8.dcinside.co.kr/missing.png" '
            'src="https://dcimg8.dcinside.co.kr/fallback.jpg"></div>'
        )
        handler = ImageHandler()
        page_response = MagicMock(text=html)
        missing_response = MagicMock()
        missing_response.headers = {}
        missing_response.status_code = 404
        missing_response.iter_content.return_value = []
        missing_response.raise_for_status.side_effect = requests.HTTPError(
            "not found",
            response=missing_response,
        )
        image_response = MagicMock()
        image_response.headers = {"content-length": str(len(data))}
        image_response.iter_content.return_value = [data]
        handler.session = MagicMock()
        handler.session.get.side_effect = [
            page_response,
            missing_response,
            image_response,
        ]

        images = handler.download_images(
            "https://gall.dcinside.com/mgallery/board/view/?id=test&no=1"
        )

        assert images[0][4] == data
        assert handler.session.get.call_args_list[1].args[0].endswith("missing.png")
        assert handler.session.get.call_args_list[2].args[0].endswith("fallback.jpg")

    def test_external_attachment_falls_back_to_inline_image(self) -> None:
        html = (
            '<div class="appending_file_box"><ul><li>'
            '<a href="https://evil.example/image.png">image.png</a>'
            "</li></ul></div>"
            '<div class="writing_view_box">'
            '<img src="https://dcimg8.dcinside.co.kr/fallback.png"></div>'
        )
        handler = self.make_handler(html, make_png_bytes())

        images = handler.download_images(
            "https://gall.dcinside.com/mgallery/board/view/?id=test&no=1"
        )

        assert images[0][2] == "fallback.png"
        assert handler.session.get.call_args_list[1].args[0].endswith("fallback.png")

    def test_rejects_lookalike_host_and_custom_port(self) -> None:
        assert ImageHandler._is_allowed_dc_image_url(
            "https://dcimgevil.dcinside.com/image.png"
        ) is False
        assert ImageHandler._is_allowed_dc_image_url(
            "https://dcimg8.dcinside.co.kr:444/image.png"
        ) is False

    def test_permanently_rejected_image_is_terminal(self, monkeypatch) -> None:
        html = (
            '<div class="writing_view_box">'
            '<img src="https://dcimg8.dcinside.co.kr/large.png"></div>'
        )
        handler = self.make_handler(html, b"x")
        monkeypatch.setattr("Module.config.app_config.media_download_max_mb", 0)

        assert handler.download_images(
            "https://gall.dcinside.com/mgallery/board/view/?id=test&no=1"
        ) == []


class TestCompress:
    def test_compress_image_reaches_target(self) -> None:
        handler = ImageHandler()
        data = make_png_bytes(size=(800, 800))
        target = len(data) // 2

        output, size = handler.compress_image(data, target, "test.png")

        assert size <= target
        assert output.read(2) == b"\xff\xd8"  # JPEG 매직 바이트

    def test_compress_gif_reaches_target(self) -> None:
        handler = ImageHandler()
        data = make_gif_bytes(frames=12, size=(400, 400))
        target = int(len(data) * 0.8)

        output, size = handler.compress_gif(data, target, "test.gif")

        assert size <= target
        assert output.read(6) in (b"GIF87a", b"GIF89a")

    def test_compress_image_invalid_data_returns_original(self) -> None:
        handler = ImageHandler()
        data = b"not an image"
        output, size = handler.compress_image(data, 10, "broken.png")
        assert output.read() == data
        assert size == len(data)
