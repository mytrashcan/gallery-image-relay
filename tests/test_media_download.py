from unittest.mock import MagicMock

import pytest
import requests

from Module.media_download import (
    MediaDownloadRejected,
    MediaDownloadTooLarge,
    download_limited,
)
from Module.retry_policy import RetryPolicy


def make_response(chunks: list[bytes], content_length: int | None = None) -> MagicMock:
    response = MagicMock()
    response.headers = {}
    if content_length is not None:
        response.headers["content-length"] = str(content_length)
    response.iter_content.return_value = chunks
    return response


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
