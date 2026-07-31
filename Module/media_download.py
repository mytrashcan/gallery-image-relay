"""Bounded streaming downloads for untrusted source media."""

from __future__ import annotations

from collections.abc import Mapping

import requests

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
) -> bytes:
    """Stream a response into memory while enforcing a hard byte limit."""
    policy = retry_policy or DEFAULT_MEDIA_RETRY_POLICY
    for attempt in range(1, policy.max_attempts + 1):
        if attempt == 1:
            sleep_sync(policy.request_interval)
        response = None
        try:
            response = client.get(url, headers=headers, timeout=timeout, stream=True)
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
                if declared_size > max_bytes:
                    raise MediaDownloadTooLarge("media exceeds download limit")

            data = bytearray()
            challenge_candidate = "html" in content_type.casefold()
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                data.extend(chunk)
                if challenge_candidate and len(data) <= 128 * 1024:
                    raise_for_cloudflare_challenge(response, body=data)
                if len(data) > max_bytes:
                    raise MediaDownloadTooLarge("media exceeds download limit")
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
