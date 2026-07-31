import io
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

from Module.gallery_client import GalleryClient, attach_web_gallery


def test_publish_sends_bytes_to_authenticated_internal_endpoint(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"id": "abc.jpg"}
    post = MagicMock(return_value=response)
    client = GalleryClient("http://127.0.0.1:8000", "secret")
    client.session.post = post

    result = client.publish(
        b"image-bytes",
        "sample.jpg",
        title="title",
        link="https://example.com",
        gallery="test",
    )

    assert result == {"id": "abc.jpg"}
    post.assert_called_once()
    kwargs = post.call_args.kwargs
    assert kwargs["data"] == b"image-bytes"
    assert kwargs["headers"]["X-Ingest-Token"] == "secret"
    assert kwargs["params"]["gallery"] == "test"


def test_publish_retries_when_web_process_is_starting(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"id": "abc.jpg"}
    post = MagicMock(side_effect=[requests.ConnectionError("not ready"), response])
    sleep = MagicMock()
    monkeypatch.setattr("Module.gallery_client.time.sleep", sleep)
    client = GalleryClient("http://127.0.0.1:8000", "secret", retry_delay_seconds=0.25)
    client.session.post = post

    assert client.publish(b"image-bytes", "sample.jpg") == {"id": "abc.jpg"}
    assert post.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_publish_failure_log_does_not_include_image_metadata(monkeypatch, caplog):
    response = MagicMock()
    response.status_code = 401
    error = requests.HTTPError(
        "401 for http://127.0.0.1/internal/images?title=private-title&link=private-link",
        response=response,
    )
    response.raise_for_status.side_effect = error
    client = GalleryClient("http://127.0.0.1:8000", "secret", max_attempts=1)
    client.session.post = MagicMock(return_value=response)

    with caplog.at_level(logging.WARNING, logger="Module.gallery_client"):
        result = client.publish(
            b"image-bytes",
            "private-filename.jpg",
            title="private-title",
            link="private-link",
            gallery="private-gallery",
        )

    assert result == {}
    assert "HTTPError(status=401)" in caplog.text
    for private_value in (
        "private-filename.jpg",
        "private-title",
        "private-link",
        "private-gallery",
    ):
        assert private_value not in caplog.text


def test_publish_does_not_retry_permanent_client_error(monkeypatch):
    response = MagicMock(status_code=413)
    error = requests.HTTPError("too large", response=response)
    response.raise_for_status.side_effect = error
    sleep = MagicMock()
    monkeypatch.setattr("Module.gallery_client.time.sleep", sleep)
    client = GalleryClient("http://127.0.0.1:8000", "secret", max_attempts=3)
    client.session.post = MagicMock(return_value=response)

    assert client.publish(b"image", "large.jpg") == {}
    assert client.session.post.call_count == 1
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_publish_async_uses_async_sleep_between_attempts(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"id": "abc.jpg"}
    client = GalleryClient(
        "http://127.0.0.1:8000",
        "secret",
        retry_delay_seconds=0.25,
    )
    client.session.post = MagicMock(
        side_effect=[requests.ConnectionError("not ready"), response]
    )
    async_sleep = AsyncMock()
    monkeypatch.setattr("Module.gallery_client.sleep_async", async_sleep)
    monkeypatch.setattr(
        "Module.gallery_client.time.sleep",
        MagicMock(side_effect=AssertionError("time.sleep called from async retry")),
    )

    assert await client.publish_async(b"image", "sample.jpg") == {"id": "abc.jpg"}
    async_sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_sender_wrapper_publishes_only_after_successful_delivery():
    sender = MagicMock()
    sender.send_to_discord = AsyncMock(side_effect=[False, True])
    sender.send_to_telegram = AsyncMock(return_value=False)
    client = MagicMock()
    client.publish_async = AsyncMock(return_value={"id": "abc.jpg"})
    attach_web_gallery(sender, "test", client)
    buffer = io.BytesIO(b"image")

    await sender.send_to_discord(MagicMock(), "title", buffer, "sample.jpg", "https://example.com")
    await sender.send_to_discord(MagicMock(), "title", buffer, "sample.jpg", "https://example.com")

    client.publish_async.assert_awaited_once_with(
        b"image",
        "sample.jpg",
        title="title",
        link="https://example.com",
        gallery="test",
    )


@pytest.mark.asyncio
async def test_sender_wrapper_forwards_prevalidated_media():
    sender = MagicMock()
    original_discord = AsyncMock(return_value=True)
    original_telegram = AsyncMock(return_value=True)
    sender.send_to_discord = original_discord
    sender.send_to_telegram = original_telegram
    client = MagicMock()
    client.publish_async = AsyncMock(return_value={"id": "abc.jpg"})
    attach_web_gallery(sender, "test", client)
    buffer = io.BytesIO(b"image")

    await sender.send_to_discord(
        MagicMock(), "title", buffer, "sample.jpg", validated=True
    )
    await sender.send_to_telegram(
        buffer, "sample.jpg", False, validated=True
    )

    original_discord.assert_awaited_once()
    original_telegram.assert_awaited_once()
    assert original_discord.await_args.kwargs["validated"] is True
    assert original_telegram.await_args.kwargs["validated"] is True
