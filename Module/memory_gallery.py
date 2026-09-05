"""Bounded, process-local image storage for the ephemeral web gallery."""

from __future__ import annotations

import hashlib
import io
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image

_FORMAT_INFO = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "GIF": (".gif", "image/gif"),
    "WEBP": (".webp", "image/webp"),
}
# 디코드 시 픽셀당 3~4바이트가 필요하고 압축/썸네일 과정에서 풀사이즈 사본이 한 번 더
# 생기므로, 이 값이 web 프로세스의 순간 메모리 스파이크를 좌우한다 (12M픽셀 RGB ≈ 36MB,
# 사본 포함 ~72MB/장 × 동시 2슬롯). 카드 피드 용도로는 12M(≈4000×3000)이면 충분하다.
MAX_IMAGE_PIXELS = 12_000_000


class InvalidImage(ValueError):
    pass


class ImageTooLarge(ValueError):
    pass


@dataclass
class StoredImage:
    image_id: str
    data: bytes
    media_type: str
    thumbnail: bytes | None
    thumbnail_media_type: str | None
    filename: str
    title: str
    link: str
    gallery: str
    created_at: float
    width: int
    height: int
    likes: int = 0

    @property
    def memory_bytes(self) -> int:
        return len(self.data) + len(self.thumbnail or b"")

    def feed_item(self) -> dict[str, object]:
        url = f"/images/{self.image_id}"
        return {
            "id": self.image_id,
            "url": url,
            "thumb": f"{url}?thumbnail=1" if self.thumbnail else url,
            "filename": self.filename,
            "title": self.title,
            "link": self.link,
            "gallery": self.gallery,
            "created_at": self.created_at,
            "likes": self.likes,
            "w": self.width,
            "h": self.height,
        }


class MemoryGalleryStore:
    """Thread-safe TTL/LRU store with hard item and byte limits."""

    def __init__(
        self,
        *,
        max_items: int,
        max_bytes: int,
        max_image_bytes: int,
        ttl_seconds: int,
        thumbnail_width: int = 480,
        clock=time.time,
    ):
        if min(max_items, max_bytes, max_image_bytes, ttl_seconds) <= 0:
            raise ValueError("memory gallery limits must be positive")
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.max_image_bytes = min(max_image_bytes, max_bytes)
        self.ttl_seconds = ttl_seconds
        self.thumbnail_width = max(0, thumbnail_width)
        self._clock = clock
        self._items: OrderedDict[str, StoredImage] = OrderedDict()
        self._recent_hashes: dict[str, float] = {}
        self._bytes = 0
        self._lock = threading.RLock()
        self.started_at = clock()

    def put(self, data: bytes, filename: str, title: str = "", link: str = "", gallery: str = "") -> dict:
        prepared, ext, media_type, width, height, thumbnail, thumb_type = self._prepare(data)
        digest = hashlib.sha256(prepared).hexdigest()
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            recent = self._recent_hashes.get(digest)
            if recent is not None and now - recent < 300:
                return {}

            image_id = f"{uuid.uuid4().hex}{ext}"
            item = StoredImage(
                image_id=image_id,
                data=prepared,
                media_type=media_type,
                thumbnail=thumbnail,
                thumbnail_media_type=thumb_type,
                filename=(filename or image_id)[:255],
                title=(title or "")[:500],
                link=(link or "")[:2048],
                gallery=(gallery or "")[:100],
                created_at=now,
                width=width,
                height=height,
            )
            if item.memory_bytes > self.max_bytes:
                raise ImageTooLarge("image exceeds total memory capacity")
            while self._items and (
                len(self._items) >= self.max_items
                or self._bytes + item.memory_bytes > self.max_bytes
            ):
                self._evict_oldest_locked()
            self._items[image_id] = item
            self._bytes += item.memory_bytes
            self._recent_hashes[digest] = now
            while len(self._recent_hashes) > self.max_items * 2:
                del self._recent_hashes[next(iter(self._recent_hashes))]
            return item.feed_item()

    def get(self, image_id: str, *, thumbnail: bool = False) -> tuple[bytes, str] | None:
        with self._lock:
            self._purge_locked(self._clock())
            item = self._items.get(image_id)
            if item is None:
                return None
            if thumbnail and item.thumbnail:
                return item.thumbnail, item.thumbnail_media_type or item.media_type
            return item.data, item.media_type

    def snapshot(self, limit: int) -> list[dict[str, object]]:
        with self._lock:
            self._purge_locked(self._clock())
            items = list(reversed(self._items.values()))
            return [item.feed_item() for item in items[:limit]]

    def likes(self, image_id: str) -> int | None:
        with self._lock:
            self._purge_locked(self._clock())
            item = self._items.get(image_id)
            return item.likes if item else None

    def increment_likes(self, image_id: str) -> int | None:
        with self._lock:
            self._purge_locked(self._clock())
            item = self._items.get(image_id)
            if item is None:
                return None
            item.likes += 1
            return item.likes

    def stats(self) -> dict[str, object]:
        with self._lock:
            now = self._clock()
            self._purge_locked(now)
            latest = next(reversed(self._items.values()), None) if self._items else None
            return {
                "items": len(self._items),
                "memory_bytes": self._bytes,
                "memory_limit_bytes": self.max_bytes,
                "latest_created_at": latest.created_at if latest else None,
                "latest_age_seconds": round(now - latest.created_at, 1) if latest else None,
                "uptime_seconds": round(now - self.started_at, 1),
            }

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._recent_hashes.clear()
            self._bytes = 0

    def _prepare(self, data: bytes) -> tuple[bytes, str, str, int, int, bytes | None, str | None]:
        try:
            with Image.open(io.BytesIO(data)) as source:
                image_format = (source.format or "").upper()
                if image_format not in _FORMAT_INFO:
                    raise InvalidImage(f"unsupported image format: {image_format or 'unknown'}")
                width, height = source.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise InvalidImage("image dimensions exceed safety limit")
                source.load()
                animated = bool(getattr(source, "is_animated", False))
                prepared = data
                ext, media_type = _FORMAT_INFO[image_format]
                if len(prepared) > self.max_image_bytes:
                    if animated or image_format == "GIF":
                        raise ImageTooLarge("animated image exceeds per-image memory limit")
                    prepared = self._compress(source)
                    ext, media_type = ".jpg", "image/jpeg"
                if len(prepared) > self.max_image_bytes:
                    raise ImageTooLarge("image exceeds per-image memory limit")
                thumbnail, thumb_type = self._thumbnail(source, animated)
                return prepared, ext, media_type, width, height, thumbnail, thumb_type
        except ImageTooLarge:
            raise
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise InvalidImage("invalid image data") from exc

    def _compress(self, source: Image.Image) -> bytes:
        image = source.convert("RGB")
        image.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
        for quality in (82, 72, 62, 52, 42):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            if output.tell() <= self.max_image_bytes:
                return output.getvalue()
        return output.getvalue()

    def _thumbnail(self, source: Image.Image, animated: bool) -> tuple[bytes | None, str | None]:
        if not self.thumbnail_width or animated or source.width <= self.thumbnail_width:
            return None, None
        image = source.copy()
        image.thumbnail((self.thumbnail_width, self.thumbnail_width * 4), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=78, optimize=True)
        return output.getvalue(), "image/jpeg"

    def _purge_locked(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        while self._items:
            oldest = next(iter(self._items.values()))
            if oldest.created_at > cutoff:
                break
            self._evict_oldest_locked()
        self._recent_hashes = {
            digest: seen_at for digest, seen_at in self._recent_hashes.items()
            if now - seen_at < 300
        }

    def _evict_oldest_locked(self) -> None:
        _, item = self._items.popitem(last=False)
        self._bytes -= item.memory_bytes
