"""Loopback adapter used by crawler processes to publish ephemeral images."""

from __future__ import annotations

import logging
import time

import requests

from Module.config import app_config
from Module.lifecycle import run_blocking
from Module.retry_policy import (
    RetryDecision,
    RetryPolicy,
    classify_exception,
    raise_for_cloudflare_challenge,
    retry_delay,
    sleep_async,
    sleep_sync,
)

logger = logging.getLogger(__name__)


def _safe_error_label(exc: Exception) -> str:
    """Describe an upload failure without including its URL or response body."""
    response = getattr(exc, "response", None)
    if response is not None:
        return f"{type(exc).__name__}(status={response.status_code})"
    return type(exc).__name__


class GalleryClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        retry_policy: RetryPolicy | None = None,
    ):
        self.base_url = (base_url or app_config.web_gallery_url).rstrip("/")
        self.token = token if token is not None else app_config.web_ingest_token
        retry_delay_seconds = max(0.0, retry_delay_seconds)
        self.retry_policy = retry_policy or RetryPolicy(
            max_attempts=max(1, max_attempts),
            backoff="linear",
            base_delay=retry_delay_seconds,
            max_delay=retry_delay_seconds * max(1, max_attempts),
        )
        self.max_attempts = self.retry_policy.max_attempts
        self.retry_delay_seconds = self.retry_policy.base_delay
        self.session = requests.Session()
        self.session.trust_env = False

    def _publish_once(
        self,
        data: bytes,
        filename: str,
        *,
        title: str = "",
        link: str = "",
        gallery: str = "",
    ) -> dict:
        response = self.session.post(
            f"{self.base_url}/internal/images",
            params={
                "filename": filename or "",
                "title": title or "",
                "link": link or "",
                "gallery": gallery or "",
            },
            data=data,
            headers={
                "X-Ingest-Token": self.token,
                "Content-Type": "application/octet-stream",
            },
            timeout=10,
            allow_redirects=False,
        )
        try:
            raise_for_cloudflare_challenge(response)
            if 300 <= response.status_code < 400:
                raise requests.HTTPError("ingest redirect rejected", response=response)
            response.raise_for_status()
            return response.json()
        finally:
            response.close()

    def _retry_failure(self, exc: Exception, attempt: int) -> float | None:
        error_label = _safe_error_label(exc)
        if isinstance(exc, ValueError):
            decision = RetryDecision.RETRY
        else:
            decision = classify_exception(exc, self.retry_policy)

        if decision is RetryDecision.BLOCKED:
            logger.warning("웹 갤러리 Cloudflare challenge 감지 (error=%s)", error_label)
            return None
        if decision is not RetryDecision.RETRY:
            logger.warning("웹 갤러리 전송 거절 (error=%s)", error_label)
            return None
        if attempt == self.retry_policy.max_attempts:
            logger.warning(
                "웹 갤러리 전송 실패 (attempts=%s, error=%s)",
                self.retry_policy.max_attempts,
                error_label,
            )
            return None

        logger.warning(
            "웹 갤러리 전송 재시도 %s/%s (error=%s)",
            attempt,
            self.retry_policy.max_attempts,
            error_label,
        )
        response = getattr(exc, "response", None)
        return retry_delay(attempt, self.retry_policy, response)

    def publish(
        self,
        data: bytes,
        filename: str,
        *,
        title: str = "",
        link: str = "",
        gallery: str = "",
    ) -> dict:
        if not data or not self.token:
            if not self.token:
                logger.error("WEB_INGEST_TOKEN이 없어 웹 갤러리 전송을 건너뜁니다.")
            return {}
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            if attempt == 1 and self.retry_policy.request_interval > 0:
                sleep_sync(self.retry_policy.request_interval, sleeper=time.sleep)
            try:
                return self._publish_once(
                    data,
                    filename,
                    title=title,
                    link=link,
                    gallery=gallery,
                )
            except (requests.RequestException, ValueError) as exc:
                delay = self._retry_failure(exc, attempt)
                if delay is None:
                    return {}
                sleep_sync(delay, sleeper=time.sleep)
        return {}

    async def publish_async(
        self,
        data: bytes,
        filename: str,
        *,
        title: str = "",
        link: str = "",
        gallery: str = "",
    ) -> dict:
        if not data or not self.token:
            if not self.token:
                logger.error("WEB_INGEST_TOKEN이 없어 웹 갤러리 전송을 건너뜁니다.")
            return {}
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            if attempt == 1 and self.retry_policy.request_interval > 0:
                await sleep_async(self.retry_policy.request_interval)
            try:
                return await run_blocking(
                    self._publish_once,
                    data,
                    filename,
                    title=title,
                    link=link,
                    gallery=gallery,
                )
            except (requests.RequestException, ValueError) as exc:
                delay = self._retry_failure(exc, attempt)
                if delay is None:
                    return {}
                await sleep_async(delay)
        return {}

    def close(self) -> None:
        self.session.close()


def attach_web_gallery(message_sender, gallery: str = "", client: GalleryClient | None = None) -> None:
    """Publish successfully sent Discord/Telegram buffers to the web process."""
    gallery_client = client or GalleryClient()
    original_discord = message_sender.send_to_discord
    original_telegram = message_sender.send_to_telegram

    async def discord_with_web(
        channel, title, image_buffer, filename, url=None, *, validated: bool = False
    ):
        sent = await original_discord(
            channel, title, image_buffer, filename, url, validated=validated
        )
        if sent:
            try:
                data = image_buffer.getvalue()
            except (OSError, ValueError, AttributeError):
                data = b""
            await gallery_client.publish_async(
                data, filename or "", title=title or "", link=url or "", gallery=gallery
            )
        return sent

    async def telegram_with_web(
        image_buffer,
        filename=None,
        is_gif=False,
        max_retries=3,
        *,
        validated: bool = False,
    ):
        sent = await original_telegram(
            image_buffer,
            filename,
            is_gif,
            max_retries,
            validated=validated,
        )
        if sent:
            try:
                data = image_buffer.getvalue()
            except (OSError, ValueError, AttributeError):
                data = b""
            await gallery_client.publish_async(data, filename or "", gallery=gallery)
        return sent

    message_sender.send_to_discord = discord_with_web
    message_sender.send_to_telegram = telegram_with_web
