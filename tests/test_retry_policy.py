from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

from Module.retry_policy import (
    BlockedByChallenge,
    RetryDecision,
    RetryPolicy,
    backoff_delay,
    classify_exception,
    classify_response,
    classify_status,
    parse_retry_after,
    raise_for_cloudflare_challenge,
    request_with_policy,
    sleep_async,
    sleep_sync,
)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 599])
def test_transient_statuses_are_retry_candidates(status: int) -> None:
    assert classify_status(status, RetryPolicy()) is RetryDecision.RETRY


@pytest.mark.parametrize("status", [400, 401, 404, 413])
def test_permanent_client_errors_are_not_retried(status: int) -> None:
    assert classify_status(status, RetryPolicy()) is RetryDecision.PERMANENT_FAILURE


def test_connection_errors_and_timeouts_are_retry_candidates() -> None:
    policy = RetryPolicy()

    assert classify_exception(requests.ConnectionError(), policy) is RetryDecision.RETRY
    assert classify_exception(requests.Timeout(), policy) is RetryDecision.RETRY


def test_http_error_uses_response_status_classification() -> None:
    response = MagicMock(status_code=404)
    error = requests.HTTPError(response=response)

    assert classify_exception(error, RetryPolicy()) is RetryDecision.PERMANENT_FAILURE


def test_retry_after_parses_seconds_and_http_date() -> None:
    now = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)

    assert parse_retry_after({"Retry-After": "12"}, now=now) == 12
    assert (
        parse_retry_after(
            {"retry-after": "Fri, 31 Jul 2026 00:00:30 GMT"},
            now=now,
        )
        == 30
    )


@pytest.mark.parametrize("value", ["", "-1", "not-a-date"])
def test_retry_after_rejects_invalid_values(value: str) -> None:
    assert parse_retry_after({"Retry-After": value}) is None


def test_backoff_supports_linear_and_exponential_modes() -> None:
    linear = RetryPolicy(backoff="linear", base_delay=0.5, max_delay=10)
    exponential = RetryPolicy(backoff="exponential", base_delay=0.5, max_delay=10)

    assert backoff_delay(3, linear) == 1.5
    assert backoff_delay(3, exponential) == 2.0


def test_backoff_jitter_stays_in_range_and_obeys_max_delay(monkeypatch) -> None:
    policy = RetryPolicy(base_delay=4, max_delay=5, jitter=0.25)

    monkeypatch.setattr("Module.retry_policy.random.uniform", lambda low, high: low)
    assert backoff_delay(1, policy) == 3

    monkeypatch.setattr("Module.retry_policy.random.uniform", lambda low, high: high)
    assert backoff_delay(1, policy) == 5


def test_cloudflare_challenge_has_explicit_decision_and_exception() -> None:
    response = MagicMock()
    response.status_code = 503
    response.headers = {"Content-Type": "text/html; charset=UTF-8"}
    response.text = "<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>"

    assert classify_response(response, RetryPolicy()) is RetryDecision.BLOCKED
    with pytest.raises(BlockedByChallenge):
        raise_for_cloudflare_challenge(response)


def test_request_with_policy_prefers_retry_after_for_429() -> None:
    limited = MagicMock()
    limited.status_code = 429
    limited.headers = {"Retry-After": "7"}
    limited.text = ""
    limited.raise_for_status.side_effect = requests.HTTPError(response=limited)
    success = MagicMock()
    success.status_code = 200
    success.headers = {}
    success.text = ""
    request = MagicMock(side_effect=[limited, success])
    sleeper = MagicMock()

    assert request_with_policy(request, RetryPolicy(), sleeper=sleeper) is success
    sleeper.assert_called_once_with(7)
    limited.close.assert_called_once()


def test_request_with_policy_does_not_retry_permanent_failure() -> None:
    response = MagicMock()
    response.status_code = 404
    response.headers = {}
    response.text = ""
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    request = MagicMock(return_value=response)
    sleeper = MagicMock()

    with pytest.raises(requests.HTTPError):
        request_with_policy(request, RetryPolicy(), sleeper=sleeper)

    request.assert_called_once()
    sleeper.assert_not_called()
    response.close.assert_called_once()


def test_request_with_policy_applies_normal_request_interval() -> None:
    response = MagicMock(status_code=200, headers={}, text="")
    sleeper = MagicMock()

    assert (
        request_with_policy(
            MagicMock(return_value=response),
            RetryPolicy(request_interval=0.75),
            sleeper=sleeper,
        )
        is response
    )
    sleeper.assert_called_once_with(0.75)


@pytest.mark.asyncio
async def test_sync_and_async_sleep_strategies_are_separate() -> None:
    sync_sleeper = MagicMock()
    async_sleeper = AsyncMock()

    sleep_sync(0.25, sleeper=sync_sleeper)
    await sleep_async(0.5, sleeper=async_sleeper)

    sync_sleeper.assert_called_once_with(0.25)
    async_sleeper.assert_awaited_once_with(0.5)
