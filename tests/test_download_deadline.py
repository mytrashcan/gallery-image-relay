"""The download deadline must bound a body that trickles bytes faster than the read timeout."""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests

from Module.lifecycle import run_blocking
from Module.media_download import DownloadDeadlineExceeded, download_limited
from Module.retry_policy import RetryPolicy

BODY_BYTES = 40
BYTE_INTERVAL = 0.1  # 4 s for the whole body, each read well inside the 2 s read timeout


@contextmanager
def trickle_server(*, send_length: bool, interval: float = BYTE_INTERVAL):
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if send_length:
                self.send_header("Content-Length", str(BODY_BYTES))
            self.end_headers()
            try:
                for _ in range(BODY_BYTES):
                    if stop.is_set():
                        return
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(interval)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def download(url: str, deadline_seconds: float) -> bytes:
    with requests.Session() as session:
        session.trust_env = False
        return download_limited(
            session, url, headers=None, timeout=2, max_bytes=1024,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0), deadline_seconds=deadline_seconds,
        )


@pytest.mark.parametrize("send_length", [True, False])
def test_trickling_body_stops_at_deadline(send_length: bool) -> None:
    with trickle_server(send_length=send_length) as url:
        started = time.monotonic()
        with pytest.raises(DownloadDeadlineExceeded):
            download(url, deadline_seconds=1.0)
        elapsed = time.monotonic() - started

    assert elapsed < 2.5  # the full body would take 4 s


def test_trickled_body_within_deadline_is_returned() -> None:
    with trickle_server(send_length=True, interval=0.01) as url:
        assert download(url, deadline_seconds=10.0) == b"x" * BODY_BYTES


@pytest.mark.asyncio
async def test_cancelled_download_joins_within_deadline() -> None:
    """Cancellation still waits for the worker thread, which the deadline now bounds."""
    with trickle_server(send_length=True) as url:
        task = asyncio.create_task(run_blocking(download, url, 1.0))
        await asyncio.sleep(0.3)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        joined_after = time.monotonic() - started

    assert joined_after < 2.0


def test_retry_wait_past_deadline_is_not_slept() -> None:
    unavailable = MagicMock(status_code=503, headers={})
    unavailable.raise_for_status.side_effect = requests.HTTPError(response=unavailable)
    client = MagicMock()
    client.get.return_value = unavailable
    started = time.monotonic()

    with pytest.raises(DownloadDeadlineExceeded):
        download_limited(
            client, "https://example.com/image", headers=None, timeout=1, max_bytes=10,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=5, max_delay=5), deadline_seconds=1.0,
        )

    assert time.monotonic() - started < 1.0
    assert client.get.call_count == 1


def test_expired_deadline_sends_no_request() -> None:
    client = MagicMock()
    response = MagicMock(status_code=200, headers={})
    response.iter_content.return_value = [b"late"]
    client.get.return_value = response

    with pytest.raises(DownloadDeadlineExceeded):
        download_limited(
            client, "https://example.com/image", headers=None, timeout=1, max_bytes=10,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0), deadline_seconds=0.0,
        )

    client.get.assert_not_called()
