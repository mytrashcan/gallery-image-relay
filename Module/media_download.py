"""Bounded streaming downloads for untrusted source media."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import requests

from Module.media_candidate import MediaCandidate
from Module.retry_policy import (
    BlockedByChallenge,
    RetryDecision,
    RetryPolicy,
    classify_exception,
    raise_for_cloudflare_challenge,
    retry_delay,
    sleep_sync,
)


class MediaDownloadRejected(ValueError):
    """The source media cannot become deliverable by retrying later."""


class MediaDownloadTooLarge(MediaDownloadRejected):
    pass


class MediaAttemptBudgetExceeded(requests.RequestException):
    """The fallback chain exhausted its aggregate request-attempt budget."""


class MediaCandidatesExhausted(requests.RequestException):
    """Every allowed candidate failed, including at least one transient failure."""


@dataclass(frozen=True, slots=True)
class VerifiedMedia:
    """Downloaded media that passed signature and caller-provided validation."""

    data: bytes
    url: str
    filename: str
    content_hash: str
    media_type: str


MAX_MEDIA_CANDIDATES = 4
MAX_MEDIA_ATTEMPTS = 6

DEFAULT_MEDIA_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    backoff="exponential",
    base_delay=1.0,
    max_delay=4.0,
)


def download_limited(
    client: object,
    url: str,
    *,
    headers: Mapping[str, str] | None,
    timeout: float,
    max_bytes: int,
    chunk_size: int = 64 * 1024,
    retry_policy: RetryPolicy | None = None,
    on_attempt: Callable[[], None] | None = None,
    is_allowed_url: Callable[[str], bool] | None = None,
) -> bytes:
    """Stream a response into memory while enforcing a hard byte limit."""
    policy = retry_policy or DEFAULT_MEDIA_RETRY_POLICY
    deadline = time.monotonic() + max(30.0, timeout * 4)
    for attempt in range(1, policy.max_attempts + 1):
        if on_attempt is not None:
            on_attempt()
        if attempt == 1:
            sleep_sync(policy.request_interval)
        response = None
        try:
            current_url = url
            for redirect in range(4):
                if time.monotonic() >= deadline:
                    raise requests.Timeout("download deadline exceeded")
                if is_allowed_url is not None and not is_allowed_url(current_url):
                    raise MediaDownloadRejected("request destination rejected")
                if redirect and on_attempt is not None:
                    on_attempt()
                response = client.get(current_url, headers=headers, timeout=timeout, stream=True, allow_redirects=False)
                if getattr(response, "status_code", None) not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location or redirect == 3 or is_allowed_url is None:
                    raise MediaDownloadRejected("redirect rejected")
                current_url = urljoin(current_url, location)
                response.close()
                response = None
            status = getattr(response, "status_code", None)
            content_type = response.headers.get("content-type", "")
            if status in {403, 503} and "html" in content_type.casefold():
                challenge_body = next(
                    iter(response.iter_content(chunk_size=min(chunk_size, 128 * 1024))),
                    b"",
                )
                raise_for_cloudflare_challenge(response, body=challenge_body)
            response.raise_for_status()

            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise MediaDownloadRejected("invalid content-length") from exc
                if declared_size < 0:
                    raise MediaDownloadRejected("invalid content-length")
                if declared_size > max_bytes:
                    raise MediaDownloadTooLarge("media exceeds download limit")

            data = bytearray()
            challenge_candidate = "html" in content_type.casefold()
            for chunk in response.iter_content(chunk_size=chunk_size):
                if time.monotonic() >= deadline:
                    raise requests.Timeout("download deadline exceeded")
                if not chunk:
                    continue
                if len(data) + len(chunk) > max_bytes:
                    raise MediaDownloadTooLarge("media exceeds download limit")
                data.extend(chunk)
                if challenge_candidate and len(data) <= 128 * 1024:
                    raise_for_cloudflare_challenge(response, body=bytes(data))
            return bytes(data)
        except (MediaDownloadRejected, BlockedByChallenge):
            raise
        except requests.RequestException as exc:
            decision = classify_exception(exc, policy)
            if decision is RetryDecision.PERMANENT_FAILURE:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                raise MediaDownloadRejected(
                    f"media request rejected with status {status}"
                ) from exc
            if decision is not RetryDecision.RETRY or attempt == policy.max_attempts:
                raise
            retry_response = getattr(exc, "response", None)
            if retry_response is None:
                retry_response = response
            delay = retry_delay(attempt, policy, retry_response)
        finally:
            if response is not None:
                response.close()
        sleep_sync(delay)

    raise RuntimeError("unreachable retry loop")


def image_extension_from_data(image_data: bytes) -> str:
    """Return a normalized extension only for recognized image signatures."""
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if (
        len(image_data) >= 12
        and image_data.startswith(b"RIFF")
        and image_data[8:12] == b"WEBP"
    ):
        return "webp"
    if image_data.startswith(b"BM"):
        return "bmp"
    raise MediaDownloadRejected("unrecognized image signature")


def ensure_image_extension(filename: str, image_data: bytes) -> str:
    """Make the filename extension agree with the downloaded signature."""
    extension = image_extension_from_data(image_data)
    safe_name = os.path.basename(filename)[:255] or "image"
    stem, current_extension = os.path.splitext(safe_name)
    if current_extension.casefold() == f".{extension}":
        return safe_name
    if extension == "jpg" and current_extension.casefold() == ".jpeg":
        return safe_name
    return f"{stem or 'image'}.{extension}"


def download_media_candidate(
    client: object,
    candidate: MediaCandidate,
    *,
    is_allowed_url: Callable[[str], bool],
    validate: Callable[[bytes], object],
    timeout: float,
    max_bytes: int,
    retry_policy: RetryPolicy | None = None,
    max_candidates: int = MAX_MEDIA_CANDIDATES,
    max_attempts: int = MAX_MEDIA_ATTEMPTS,
) -> VerifiedMedia:
    """Download the first allowed URL whose bytes pass image validation."""
    if max_candidates < 1:
        raise ValueError("max_candidates must be at least 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    attempts = 0
    transient_failure = False

    def reserve_attempt() -> None:
        nonlocal attempts
        if attempts >= max_attempts:
            raise MediaAttemptBudgetExceeded(
                f"media attempt budget exhausted after {attempts} requests"
            )
        attempts += 1

    urls = tuple(dict.fromkeys((candidate.primary_url, *candidate.fallback_urls)))
    for url in urls[:max_candidates]:
        if not is_allowed_url(url):
            continue
        try:
            image_data = download_limited(
                client,
                url,
                headers=candidate.headers,
                timeout=timeout,
                max_bytes=max_bytes,
                retry_policy=retry_policy,
                on_attempt=reserve_attempt,
                is_allowed_url=is_allowed_url,
            )
            extension = image_extension_from_data(image_data)
            _validate_expected_type(candidate.expected_media_type, extension)
            validate(image_data)
        except MediaAttemptBudgetExceeded:
            raise
        except (MediaDownloadRejected, ValueError):
            continue
        except requests.RequestException:
            transient_failure = True
            continue

        filename_hint = candidate.filename_hint or _filename_from_url(url)
        return VerifiedMedia(
            data=image_data,
            url=url,
            filename=ensure_image_extension(filename_hint, image_data),
            content_hash=hashlib.sha256(image_data).hexdigest(),
            media_type=f"image/{'jpeg' if extension == 'jpg' else extension}",
        )

    if transient_failure:
        raise MediaCandidatesExhausted("all media candidates failed")
    raise MediaDownloadRejected("all media candidates were rejected")


def _filename_from_url(url: str) -> str:
    return os.path.basename(unquote(urlsplit(url).path)) or "image"


def _validate_expected_type(expected_media_type: str | None, extension: str) -> None:
    if not expected_media_type:
        return
    normalized = expected_media_type.casefold().removeprefix("image/")
    if normalized == "jpeg":
        normalized = "jpg"
    if normalized not in {"image", extension}:
        raise MediaDownloadRejected("media signature does not match expected type")
