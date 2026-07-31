"""Shared retry classification, backoff, and sleep strategies."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Literal

import requests


class RetryDecision(Enum):
    SUCCESS = "success"
    RETRY = "retry"
    PERMANENT_FAILURE = "permanent_failure"
    BLOCKED = "blocked"


class BlockedByChallenge(requests.RequestException):
    """The upstream returned a Cloudflare browser challenge instead of content."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    retry_statuses: frozenset[int] = field(
        default_factory=lambda: frozenset({408, 429})
    )
    backoff: Literal["linear", "exponential"] = "exponential"
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.0
    request_interval: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff not in {"linear", "exponential"}:
            raise ValueError("backoff must be 'linear' or 'exponential'")
        if self.base_delay < 0 or self.max_delay < 0 or self.request_interval < 0:
            raise ValueError("retry delays and request_interval cannot be negative")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")
        statuses = frozenset(self.retry_statuses)
        if any(not isinstance(status, int) or not 100 <= status <= 599 for status in statuses):
            raise ValueError("retry_statuses must contain valid HTTP status codes")
        object.__setattr__(self, "retry_statuses", statuses)


def classify_status(status_code: int | None, policy: RetryPolicy) -> RetryDecision:
    """Classify an HTTP status without treating every 4xx as transient."""
    if status_code is None:
        return RetryDecision.RETRY
    if status_code in policy.retry_statuses or status_code >= 500:
        return RetryDecision.RETRY
    if 400 <= status_code < 500:
        return RetryDecision.PERMANENT_FAILURE
    return RetryDecision.SUCCESS


def classify_exception(exc: Exception, policy: RetryPolicy) -> RetryDecision:
    """Classify requests transport and HTTP errors for retry handling."""
    if isinstance(exc, BlockedByChallenge):
        return RetryDecision.BLOCKED
    if isinstance(exc, requests.RequestException):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return classify_status(status, policy)
    return RetryDecision.PERMANENT_FAILURE


def classify_response(response: object, policy: RetryPolicy) -> RetryDecision:
    if is_cloudflare_challenge(response):
        return RetryDecision.BLOCKED
    return classify_status(getattr(response, "status_code", None), policy)


def backoff_delay(attempt: int, policy: RetryPolicy) -> float:
    """Return the delay before retry ``attempt`` (one-based)."""
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    if policy.backoff == "linear":
        raw_delay = policy.base_delay * attempt
    else:
        raw_delay = policy.base_delay * (2 ** (attempt - 1))

    if policy.jitter:
        raw_delay = random.uniform(
            raw_delay * (1 - policy.jitter),
            raw_delay * (1 + policy.jitter),
        )
    return min(policy.max_delay, raw_delay)


def _header_value(headers: Mapping[str, object], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value).strip()
    return None


def parse_retry_after(
    headers: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse standard Retry-After delta-seconds or an HTTP-date."""
    value = _header_value(headers, "Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        seconds = (retry_at - current).total_seconds()
    return seconds if seconds >= 0 else None


def retry_delay(
    attempt: int,
    policy: RetryPolicy,
    response: object | None = None,
) -> float:
    """Choose Retry-After for 429, otherwise the configured backoff."""
    delay = None
    if getattr(response, "status_code", None) == 429:
        headers = getattr(response, "headers", {})
        if isinstance(headers, Mapping):
            delay = parse_retry_after(headers)
    if delay is None:
        delay = backoff_delay(attempt, policy)
    return max(policy.request_interval, min(policy.max_delay, delay))


def is_cloudflare_challenge(
    response: object,
    *,
    body: str | bytes | None = None,
) -> bool:
    """Detect common Cloudflare challenge HTML markers."""
    headers = getattr(response, "headers", {})
    content_type = (
        _header_value(headers, "Content-Type")
        if isinstance(headers, Mapping)
        else None
    )
    if body is None:
        try:
            body = getattr(response, "text", "")
        except (AttributeError, requests.RequestException, UnicodeError):
            body = ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="ignore")
    if not isinstance(body, str):
        return False

    lowered = body[:128 * 1024].casefold()
    html_response = "html" in (content_type or "").casefold() or "<html" in lowered
    markers = (
        "/cdn-cgi/challenge-platform/",
        "__cf_chl_",
        "cf-chl-",
        "cloudflare ray id",
        "attention required! | cloudflare",
    )
    return html_response and (
        any(marker in lowered for marker in markers)
        or ("just a moment" in lowered and "cloudflare" in lowered)
    )


def raise_for_cloudflare_challenge(
    response: object,
    *,
    body: str | bytes | None = None,
) -> None:
    if is_cloudflare_challenge(response, body=body):
        raise BlockedByChallenge(
            "upstream returned a Cloudflare browser challenge",
            response=response,
        )


def request_with_policy(
    request: Callable[[], object],
    policy: RetryPolicy,
    *,
    sleeper: Callable[[float], object] | None = None,
) -> object:
    """Execute one blocking requests call with shared retry classification."""
    for attempt in range(1, policy.max_attempts + 1):
        if attempt == 1:
            sleep_sync(policy.request_interval, sleeper=sleeper)
        response = None
        try:
            response = request()
            raise_for_cloudflare_challenge(response)
            response.raise_for_status()
            return response
        except BlockedByChallenge:
            _close_response(response)
            raise
        except requests.RequestException as exc:
            decision = classify_exception(exc, policy)
            if decision is not RetryDecision.RETRY or attempt == policy.max_attempts:
                _close_response(response)
                raise
            retry_response = getattr(exc, "response", None)
            if retry_response is None:
                retry_response = response
            delay = retry_delay(attempt, policy, retry_response)
            _close_response(response)
            sleep_sync(delay, sleeper=sleeper)
    raise RuntimeError("unreachable retry loop")


def _close_response(response: object | None) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def sleep_sync(
    delay: float,
    *,
    sleeper: Callable[[float], object] | None = None,
) -> None:
    if delay > 0:
        (sleeper or time.sleep)(delay)


async def sleep_async(
    delay: float,
    *,
    sleeper: Callable[[float], Awaitable[object]] | None = None,
) -> None:
    if delay > 0:
        await (sleeper or asyncio.sleep)(delay)
