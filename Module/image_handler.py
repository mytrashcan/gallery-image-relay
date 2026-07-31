from __future__ import annotations

import io
import logging
import math
import os
import warnings
from math import ceil
from urllib.parse import unquote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image

from Module.config import BS_PARSER, DISCORD_MAX_SIZE, HEADERS, REQUEST_TIMEOUT, app_config
from Module.lru_cache import LRUCache
from Module.media_candidate import MediaCandidate
from Module.media_download import (
    MediaDownloadRejected,
    download_media_candidate,
    ensure_image_extension,
    image_extension_from_data,
)

logger = logging.getLogger(__name__)

# Telegram: 10MB (photo), 50MB (document)
# Discord 제한은 서버 부스트 레벨에 따라 다르므로 config.DISCORD_MAX_SIZE(.env로 조정 가능) 사용
TELEGRAM_MAX_SIZE = 10 * 1024 * 1024

MAX_HASH_CACHE_SIZE = 1000
MAX_GIF_FRAMES = 20


class ImageHandler:
    def __init__(self) -> None:
        self._seen_hashes = LRUCache(MAX_HASH_CACHE_SIZE)
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _is_duplicate(self, content_hash: object) -> bool:
        return self._seen_hashes.add_if_absent(content_hash)

    def is_duplicate(self, content_hash: object) -> bool:
        """Check and record a hash using the deprecated combined operation."""
        warnings.warn(
            "is_duplicate() is deprecated; use has_seen_hash() and "
            "mark_hash_sent() so hashes are recorded only after delivery",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._is_duplicate(content_hash)

    def has_seen_hash(self, content_hash: object) -> bool:
        return content_hash in self._seen_hashes

    def mark_hash_sent(self, content_hash: object) -> None:
        self._seen_hashes.add(content_hash)

    def clear_seen_hashes(self) -> object:
        """중복 체크용 해시 캐시 초기화"""
        self._seen_hashes.clear()
        logger.info("이미지 해시 캐시가 초기화되었습니다.")

    def compress_gif(self, image_data: object, target_size: object, filename: object) -> object:
        """GIF 압축 (프레임 수 줄이기 + 크기 조절)"""
        try:
            original_size = len(image_data)
            buffer = io.BytesIO(image_data)
            img = Image.open(buffer)

            if not getattr(img, 'is_animated', False):
                buffer.seek(0)
                return buffer, original_size

            frame_count = int(getattr(img, "n_frames", 1))
            step = max(1, ceil(frame_count / MAX_GIF_FRAMES))
            frames = []
            durations = []
            for frame_index in range(frame_count):
                img.seek(frame_index)
                if frame_index % step == 0:
                    frames.append(img.copy())
                    durations.append(0)
                # Multiplying a sampled frame's duration by ``step`` assumes
                # uniform timing and skews variable-duration animations. Sum
                # each skipped frame's duration into its sampled frame instead.
                durations[-1] += img.info.get("duration", 100)

            # 2단계: 크기 조절 (비율 유지)
            # 파일 크기는 대략 면적(scale^2)에 비례하므로 sqrt(목표/원본)을 시작점으로 추정
            scale = min(1.0, round(math.sqrt(target_size / original_size), 1) + 0.1)
            while scale > 0.3:
                new_width = int(frames[0].width * scale)
                new_height = int(frames[0].height * scale)

                resized_frames = [f.resize((new_width, new_height), Image.Resampling.LANCZOS)
                                  for f in frames]

                output = io.BytesIO()
                resized_frames[0].save(
                    output,
                    format='GIF',
                    save_all=True,
                    append_images=resized_frames[1:],
                    duration=durations,
                    loop=0,
                    optimize=True
                )

                if output.tell() <= target_size:
                    output.seek(0)
                    logger.info(f"[GIF 압축] {filename}: {original_size} -> {output.tell()} bytes (scale: {scale:.1f})")
                    return output, output.tell()

                scale -= 0.1

            logger.warning(f"[GIF 압축 실패] {filename}: 목표 크기 달성 불가")
            buffer.seek(0)
            return buffer, original_size

        except (OSError, ValueError) as e:
            # PIL 디코딩/저장 오류는 대부분 OSError(UnidentifiedImageError 포함)·ValueError
            logger.error(f"[GIF 압축 에러] {filename}: {e}")
            buffer = io.BytesIO(image_data)
            return buffer, len(image_data)

    def compress_image(self, image_data: object, target_size: object, filename: object) -> object:
        """일반 이미지(JPG/PNG) 압축"""
        try:
            original_size = len(image_data)
            buffer = io.BytesIO(image_data)
            img = Image.open(buffer)

            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            # Cap web-delivery JPEGs at 95: quality 100 greatly increases size
            # while providing little visible benefit, even when it would fit.
            low, high = 20, 95
            best_output = None
            best_quality = None
            for _ in range(6):
                quality = (low + high) // 2
                output = io.BytesIO()
                img.save(output, format='JPEG', quality=quality, optimize=True)
                if output.tell() <= target_size:
                    best_output = output
                    best_quality = quality
                    low = quality + 1
                else:
                    high = quality - 1

            if best_output is not None:
                best_output.seek(0)
                logger.info(
                    f"[이미지 압축] {filename}: {original_size} -> {best_output.getbuffer().nbytes} "
                    f"bytes (quality: {best_quality})"
                )
                return best_output, best_output.getbuffer().nbytes

            scale = min(0.8, round(math.sqrt(target_size / original_size), 1) + 0.1)
            for _ in range(4):
                if scale <= 0.3:
                    break
                new_size = (int(img.width * scale), int(img.height * scale))
                resized = img.resize(new_size, Image.Resampling.LANCZOS)

                output = io.BytesIO()
                resized.save(output, format='JPEG', quality=70, optimize=True)

                if output.tell() <= target_size:
                    output.seek(0)
                    logger.info(f"[이미지 압축] {filename}: {original_size} -> {output.tell()} bytes (scale: {scale:.1f})")
                    return output, output.tell()

                scale -= 0.15

            buffer.seek(0)
            return buffer, original_size

        except (OSError, ValueError) as e:
            logger.error(f"[이미지 압축 에러] {filename}: {e}")
            buffer = io.BytesIO(image_data)
            return buffer, len(image_data)

    def process_image(self, image_data: object, filename: object) -> object:
        """이미지 처리 (필요시 압축) - Discord/Telegram용 두 버전 반환"""
        file_ext = filename.split('.')[-1].lower() if '.' in filename else ''
        is_gif = file_ext == 'gif' or image_data[:6] in (b'GIF87a', b'GIF89a')

        original_size = len(image_data)
        discord_compressed = False
        telegram_compressed = False

        # Discord용 (기본 10MB 제한 — DISCORD_MAX_SIZE_MB 환경변수로 조정)
        if original_size > DISCORD_MAX_SIZE:
            if is_gif:
                discord_buffer, discord_size = self.compress_gif(image_data, DISCORD_MAX_SIZE, filename)
            else:
                discord_buffer, discord_size = self.compress_image(image_data, DISCORD_MAX_SIZE, filename)
            discord_compressed = True
            logger.info(f"[Discord 압축] {filename}: {original_size:,} -> {discord_size:,} bytes ({(1 - discord_size / original_size) * 100:.1f}% 감소)")
        else:
            discord_buffer = io.BytesIO(image_data)
            discord_size = original_size

        # Telegram용 (10MB 제한)
        if original_size > TELEGRAM_MAX_SIZE:
            if discord_compressed and discord_size <= TELEGRAM_MAX_SIZE:
                # Discord용 압축 결과가 Telegram 제한도 만족하면 재압축 생략
                # (기본 설정에서는 두 제한이 모두 10MB라 항상 이 경로를 탐)
                telegram_buffer = io.BytesIO(discord_buffer.getvalue())
                telegram_size = discord_size
            elif is_gif:
                telegram_buffer, telegram_size = self.compress_gif(image_data, TELEGRAM_MAX_SIZE, filename)
            else:
                telegram_buffer, telegram_size = self.compress_image(image_data, TELEGRAM_MAX_SIZE, filename)
            telegram_compressed = True
            logger.info(f"[Telegram 압축] {filename}: {original_size:,} -> {telegram_size:,} bytes ({(1 - telegram_size / original_size) * 100:.1f}% 감소)")
        else:
            telegram_buffer = io.BytesIO(image_data)

        if not discord_compressed and not telegram_compressed:
            logger.debug(f"[압축 불필요] {filename}: {original_size:,} bytes (제한 이내)")

        discord_buffer.seek(0)
        telegram_buffer.seek(0)

        return discord_buffer, telegram_buffer, is_gif

    def prepare_image(self, image_data: bytes, filename: str) -> tuple[object, object, bool]:
        """Validate dimensions once, then build Discord and Telegram buffers."""
        self.validate_image_data(image_data)
        return self.process_image(image_data, filename)

    @staticmethod
    def validate_image_data(image_data: bytes) -> None:
        """Reject malformed or excessively large decoded images."""
        try:
            with Image.open(io.BytesIO(image_data)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > app_config.media_max_pixels:
                    raise ValueError("image dimensions exceed safety limit")
                image.verify()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError("invalid image data") from exc

    @staticmethod
    def _is_allowed_dc_image_url(url: str) -> bool:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").lower()
        host_label = hostname.split(".", 1)[0]
        try:
            has_custom_port = parts.port is not None
        except ValueError:
            return False
        return (
            parts.scheme == "https"
            and parts.username is None
            and parts.password is None
            and not has_custom_port
            and (host_label == "dcimg" or host_label.removeprefix("dcimg").isdigit())
            and (hostname.endswith(".dcinside.com") or hostname.endswith(".dcinside.co.kr"))
        )

    @staticmethod
    def _image_extension_from_data(image_data: bytes) -> str:
        """Determine the correct image extension from magic bytes."""
        return image_extension_from_data(image_data)

    @staticmethod
    def _image_filename(element: object, image_url: str) -> str:
        label = element.get_text(strip=True) if getattr(element, "get_text", None) else ""
        filename = os.path.basename(unquote(urlsplit(image_url).path))
        return (label or filename or "dcinside.jpg")[:255]

    @staticmethod
    def _ensure_image_extension(filename: str, image_data: bytes) -> str:
        """Make the filename extension agree with the downloaded signature."""
        return ensure_image_extension(filename, image_data)

    def _media_candidate(
        self,
        element: object,
        post_url: str,
        headers: dict[str, str],
    ) -> MediaCandidate | None:
        sources = []
        for attribute in ("href", "data-original", "src"):
            source = element.get(attribute)
            if source:
                resolved = urljoin(post_url, source)
                if resolved not in sources:
                    sources.append(resolved)
        if not sources:
            return None
        return MediaCandidate(
            primary_url=sources[0],
            fallback_urls=tuple(sources[1:]),
            filename_hint=self._image_filename(element, sources[0]),
            headers=headers,
            expected_media_type="image",
        )

    def download_images(self, url: object) -> list | None:
        """Download the first eligible image from a post.

        Returns ``None`` when no image can be downloaded or processing fails,
        ``[]`` when the image hash has already been seen, and a one-item list
        containing the processed image buffers and metadata on success.
        """
        try:
            post_url = str(url)
            headers = {"Referer": post_url}
            res = self.session.get(post_url, headers=headers, timeout=REQUEST_TIMEOUT)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, BS_PARSER)

            attachment_links = soup.select("div.appending_file_box ul li a[href]")
            image_elements = [
                element
                for element in attachment_links
                if self._is_allowed_dc_image_url(
                    urljoin(post_url, element.get("href", ""))
                )
            ]
            if not image_elements:
                image_elements = soup.select(".writing_view_box img, .write_div img")
            for element in image_elements:
                candidate = self._media_candidate(element, post_url, headers)
                if candidate is None:
                    continue
                verified = download_media_candidate(
                    self.session,
                    candidate,
                    is_allowed_url=self._is_allowed_dc_image_url,
                    validate=self.validate_image_data,
                    timeout=REQUEST_TIMEOUT,
                    max_bytes=app_config.media_download_max_mb * 1024 * 1024,
                )

                if self.has_seen_hash(verified.content_hash):
                    logger.info(f"동일한 파일이 존재합니다. PASS: {verified.filename}")
                    return []

                discord_buffer, telegram_buffer, is_gif = self.process_image(
                    verified.data,
                    verified.filename,
                )

                logger.info(
                    "[메모리 버퍼] 파일명: %s, 원본 크기: %s bytes, GIF: %s",
                    verified.filename,
                    len(verified.data),
                    is_gif,
                )

                return [(
                    discord_buffer,
                    telegram_buffer,
                    verified.filename,
                    is_gif,
                    verified.data,
                    verified.content_hash,
                )]

            return None

        except requests.Timeout:
            logger.warning(f"이미지 다운로드 타임아웃: {url}")
            return None
        except requests.RequestException as e:
            logger.error(f"이미지 다운로드 실패: {e}")
            return None
        except MediaDownloadRejected as exc:
            logger.warning("이미지가 영구적으로 거절되어 건너뜁니다: %s", type(exc).__name__)
            return []
