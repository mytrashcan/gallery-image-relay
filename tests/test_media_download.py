import hashlib
import io
from unittest.mock import MagicMock

import pytest
import requests
from PIL import Image

from Module.media_candidate import MediaCandidate
from Module.media_download import (
    MediaAttemptBudgetExceeded,
    MediaDownloadRejected,
    MediaDownloadTooLarge,
    download_limited,
    download_media_candidate,
)
from Module.retry_policy import RetryPolicy


def make_response(chunks: list[bytes], content_length: int | None = None) -> MagicMock:
    response = MagicMock()
    response.headers = {}
    if content_length is not None:
        response.headers["content-length"] = str(content_length)
    response.iter_content.return_value = chunks
    return response


def make_png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(output, format="PNG")
    return output.getvalue()


def download_candidate(
    client: object,
    candidate: MediaCandidate,
    **kwargs: object,
):
    return download_media_candidate(
        client,
        candidate,
        is_allowed_url=lambda url: url.startswith("https://media.example/"),
        validate=lambda data: Image.open(io.BytesIO(data)).verify(),
        timeout=1,
        max_bytes=1024,
        retry_policy=RetryPolicy(max_attempts=1, base_delay=0),
        **kwargs,
    )


def test_download_limited_streams_within_limit() -> None:
    client = MagicMock()
    response = make_response([b"abc", b"def"], 6)
    client.get.return_value = response

    assert download_limited(
        client, "https://example.com/image", headers=None, timeout=1, max_bytes=6
    ) == b"abcdef"
    response.close.assert_called_once()


def test_download_limited_rejects_content_length_early() -> None:
    client = MagicMock()
    response = make_response([], 7)
    client.get.return_value = response

    with pytest.raises(MediaDownloadTooLarge):
        download_limited(
            client, "https://example.com/image", headers=None, timeout=1, max_bytes=6
        )


def test_download_limited_rejects_chunked_overflow() -> None:
    client = MagicMock()
    response = make_response([b"abcd", b"efgh"])
    client.get.return_value = response

    with pytest.raises(MediaDownloadTooLarge):
        download_limited(
            client, "https://example.com/image", headers=None, timeout=1, max_bytes=6
        )


def test_download_limited_classifies_permanent_client_error() -> None:
    client = MagicMock()
    response = make_response([])
    response.status_code = 404
    response.raise_for_status.side_effect = requests.HTTPError(
        "not found", response=response
    )
    client.get.return_value = response

    with pytest.raises(MediaDownloadRejected):
        download_limited(
            client, "https://example.com/missing", headers=None, timeout=1, max_bytes=6
        )

    response.close.assert_called_once()


def test_download_limited_retries_transient_server_error() -> None:
    client = MagicMock()
    unavailable = make_response([])
    unavailable.status_code = 503
    unavailable.raise_for_status.side_effect = requests.HTTPError(
        "unavailable",
        response=unavailable,
    )
    success = make_response([b"image"], 5)
    client.get.side_effect = [unavailable, success]

    assert (
        download_limited(
            client,
            "https://example.com/image",
            headers=None,
            timeout=1,
            max_bytes=6,
            retry_policy=RetryPolicy(base_delay=0),
        )
        == b"image"
    )

    assert client.get.call_count == 2
    unavailable.close.assert_called_once()
    success.close.assert_called_once()


def test_media_candidate_falls_back_and_uses_verified_bytes_metadata() -> None:
    client = MagicMock()
    missing = make_response([])
    missing.status_code = 404
    missing.raise_for_status.side_effect = requests.HTTPError(
        "not found",
        response=missing,
    )
    image_data = make_png_bytes()
    success = make_response([image_data], len(image_data))
    client.get.side_effect = [missing, success]
    candidate = MediaCandidate(
        primary_url="https://media.example/missing.jpg",
        fallback_urls=("https://media.example/fallback.jpg",),
        filename_hint="claimed.jpg",
    )

    result = download_candidate(client, candidate)

    assert result.url == candidate.fallback_urls[0]
    assert result.data == image_data
    assert result.filename == "claimed.png"
    assert result.content_hash == hashlib.sha256(image_data).hexdigest()
    assert client.get.call_count == 2


def test_media_candidate_falls_back_after_transient_primary_failure() -> None:
    client = MagicMock()
    image_data = make_png_bytes()
    success = make_response([image_data], len(image_data))
    client.get.side_effect = [requests.Timeout("timed out"), success]
    candidate = MediaCandidate(
        primary_url="https://media.example/primary.png",
        fallback_urls=("https://media.example/fallback.png",),
    )

    result = download_candidate(client, candidate)

    assert result.url == candidate.fallback_urls[0]
    assert client.get.call_count == 2


def test_media_candidate_revalidates_and_rejects_disallowed_fallback() -> None:
    client = MagicMock()
    missing = make_response([])
    missing.status_code = 404
    missing.raise_for_status.side_effect = requests.HTTPError(
        "not found",
        response=missing,
    )
    client.get.return_value = missing
    candidate = MediaCandidate(
        primary_url="https://media.example/missing.png",
        fallback_urls=("https://evil.example/fallback.png",),
    )

    with pytest.raises(MediaDownloadRejected):
        download_candidate(client, candidate)

    client.get.assert_called_once()


def test_media_candidate_enforces_aggregate_attempt_budget() -> None:
    client = MagicMock()
    client.get.side_effect = requests.Timeout("timed out")
    candidate = MediaCandidate(
        primary_url="https://media.example/primary.png",
        fallback_urls=("https://media.example/fallback.png",),
    )

    with pytest.raises(MediaAttemptBudgetExceeded):
        download_media_candidate(
            client,
            candidate,
            is_allowed_url=lambda url: True,
            validate=lambda data: None,
            timeout=1,
            max_bytes=1024,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0),
            max_attempts=2,
        )

    assert client.get.call_count == 2


def test_media_candidate_raises_when_all_fallbacks_fail() -> None:
    client = MagicMock()
    responses = []
    for _ in range(2):
        response = make_response([])
        response.status_code = 404
        response.raise_for_status.side_effect = requests.HTTPError(
            "not found",
            response=response,
        )
        responses.append(response)
    client.get.side_effect = responses
    candidate = MediaCandidate(
        primary_url="https://media.example/primary.png",
        fallback_urls=("https://media.example/fallback.png",),
    )

    with pytest.raises(MediaDownloadRejected):
        download_candidate(client, candidate)

    assert client.get.call_count == 2


def test_media_candidate_limits_candidate_count() -> None:
    client = MagicMock()
    response = make_response([])
    response.status_code = 404
    response.raise_for_status.side_effect = requests.HTTPError(
        "not found",
        response=response,
    )
    client.get.return_value = response
    candidate = MediaCandidate(
        primary_url="https://media.example/one.png",
        fallback_urls=(
            "https://media.example/two.png",
            "https://media.example/three.png",
        ),
    )

    with pytest.raises(MediaDownloadRejected):
        download_candidate(client, candidate, max_candidates=2)

    assert client.get.call_count == 2
