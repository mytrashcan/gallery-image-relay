"""Regression contracts for production failures; no external service calls."""
import asyncio
import io
import json
import logging
import plistlib
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests
from PIL import Image

from Module.config import AppConfig, app_config, load_gallery_configs
from Module.delivery_archive import DeliveryArchive, destination_key
from Module.delivery_result import ChannelDelivery, DeliveryOutcome
from Module.image_handler import ImageHandler
from Module.lifecycle import run_blocking
from Module.logging_policy import PrivacyFilter
from Module.media_download import MediaDownloadRejected, MediaDownloadTooLarge, download_limited
from Module.media_pipeline import MediaPipeline, PreparedMedia
from Module.url_policy import source_page
from scripts.write_launchd_plist import write_plist


def media(key="hash"):
    return PreparedMedia(io.BytesIO(b"data"), io.BytesIO(b"data"), "x.png", key, False, b"data", True)


def receipt(destination, requested, delivered):
    return ChannelDelivery("discord", destination,
        DeliveryOutcome.SUCCEEDED if requested == delivered else DeliveryOutcome.PARTIAL,
        requested, delivered, True)


@pytest.mark.asyncio
async def test_restart_retries_only_failed_destination(tmp_path):
    path = tmp_path / "archive.sqlite3"
    sender = SimpleNamespace(telegram_chat_id="t", send_to_telegram=AsyncMock(return_value=False))
    sender.send_discord_payload = AsyncMock(return_value=receipt("1", ("hash",), ("hash",)))
    with DeliveryArchive(path) as archive:
        pipeline = MediaPipeline(sender, MagicMock(), [1], delivery_archive=archive, source="dcinside", gallery_name="test")
        result = await pipeline.distribute([media()])
        assert not result.acknowledged
        assert not result.media_acknowledged("hash")
        assert archive.check("dcinside", "test", destination_key("discord", "1", "hash"))
    sender.send_to_telegram.return_value = True
    sender.send_discord_payload.reset_mock()
    with DeliveryArchive(path) as archive:
        restarted = MediaPipeline(sender, MagicMock(), [1], delivery_archive=archive, source="dcinside", gallery_name="test")
        assert (await restarted.distribute([media()])).acknowledged
        sender.send_discord_payload.assert_not_awaited()
        assert sender.send_to_telegram.await_count == 2
        assert (await restarted.distribute([media()])).acknowledged
        assert sender.send_to_telegram.await_count == 2


@pytest.mark.asyncio
async def test_partial_payload_only_retries_unacknowledged_media():
    sender = SimpleNamespace(send_discord_payload=AsyncMock(side_effect=[
        receipt("1", ("a", "b"), ("a",)), receipt("1", ("b",), ("b",))]))
    pipeline = MediaPipeline(sender, MagicMock(), [1], telegram_enabled=False)
    first = await pipeline.send_discord_batch([media("a"), media("b")], title="", link=None)
    assert not first.acknowledged
    assert first.media_acknowledged("a")
    assert not first.media_acknowledged("b")
    assert (await pipeline.send_discord_batch([media("a"), media("b")], title="", link=None)).acknowledged
    assert sender.send_discord_payload.call_args.kwargs["requested_media"] == ("b",)
    assert not (await pipeline.send_discord_batch([], title="", link=None)).acknowledged


def test_archive_receipts_are_atomic_and_corruption_is_not_overwritten(tmp_path):
    path = tmp_path / "archive.sqlite3"
    with DeliveryArchive(path) as archive:
        archive._connection.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON delivery_archive
            WHEN NEW.delivery_key = 'bad' BEGIN SELECT RAISE(ABORT, 'test fault'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            archive.add_many("dcinside", "test", ["good", "bad"])
        assert not archive.check("dcinside", "test", "good")
        archive.add_many("dcinside", "test", ["good"])
        assert archive.check("dcinside", "test", "good")
    broken = tmp_path / "broken.sqlite3"
    original = b"not a database: preserve operator evidence"
    broken.write_bytes(original)
    with pytest.raises(sqlite3.DatabaseError):
        DeliveryArchive(broken)
    assert broken.read_bytes() == original


@pytest.mark.asyncio
async def test_repeated_cancellation_joins_thread_and_preserves_cancelled_error():
    started, finish = threading.Event(), threading.Event()
    def blocking():
        started.set()
        finish.wait(2)
        raise OSError("worker failed during shutdown")
    task = asyncio.create_task(run_blocking(blocking))
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_queue_byte_budget_includes_inflight_and_is_released(monkeypatch):
    from Module import config
    monkeypatch.setattr(config.app_config, "web_upload_queue_max_mb", 1)
    pipeline = MediaPipeline(MagicMock(), MagicMock(), [], web_gallery_enabled=True)
    started, finish = asyncio.Event(), asyncio.Event()
    async def publish(*args, **kwargs):
        started.set()
        await finish.wait()
    pipeline.gallery_client.publish_async = publish
    try:
        first = await pipeline.attach_to_web_gallery(b"x" * 700000, "x", 0, "", "")
        assert first.deliveries[0].outcome is DeliveryOutcome.QUEUED
        await started.wait()
        second = await pipeline.attach_to_web_gallery(b"y" * 700000, "y", 0, "", "")
        assert second.deliveries[0].reason == "queue_full"
        assert pipeline._web_pending_bytes == 700000
    finally:
        finish.set()
        await pipeline.close()
    assert pipeline._web_pending_bytes == 0
    assert pipeline._web_queue.empty()


@pytest.mark.parametrize("url", [
    "https://[", "https://user@arca.live/b/test", "https://arca.live:443/b/test",
    "https://arca.live.evil.example/b/test", "http://arca.live/b/test",
    "https://arca.live\n.evil/b/test", "https://arca.live/b/test\t", "https://127.0.0.1/b/test",
])
def test_source_and_media_policies_reject_malformed_destinations(url):
    from Module.arca_crawler import _is_allowed_image_url
    assert not source_page(url, "arcalive")
    assert not _is_allowed_image_url(url)
    assert not ImageHandler._is_allowed_dc_image_url(url)


def test_redirect_is_checked_before_second_request():
    response = MagicMock(status_code=302, headers={"location": "http://169.254.169.254/metadata"})
    client = SimpleNamespace(get=MagicMock(return_value=response))
    with pytest.raises(MediaDownloadRejected):
        download_limited(client, "https://arca.live/b/test", headers=None, timeout=1,
            max_bytes=100, is_allowed_url=lambda url: source_page(url, "arcalive"))
    client.get.assert_called_once()
    response.close.assert_called_once()
    assert client.get.call_args.kwargs["allow_redirects"] is False


def test_stream_limit_without_content_length_closes_response():
    response = MagicMock(status_code=200, headers={})
    response.iter_content.return_value = [b"abcd", b"efgh"]
    client = SimpleNamespace(get=MagicMock(return_value=response))
    with pytest.raises(MediaDownloadTooLarge):
        download_limited(client, "https://arca.live/b/test", headers=None, timeout=1, max_bytes=5)
    response.close.assert_called_once()


@pytest.mark.parametrize("kwargs", [{"web_memory_max_mb": 0}, {"web_port": 65536},
    {"web_upload_queue_max_mb": -1}, {"arca_download_concurrency": 5}, {"media_max_frames": 0},
    {"arca_socks_proxy": "http://proxy"}, {"web_gallery_url": "http://user:password@localhost"}])
def test_configuration_fails_fast(kwargs):
    with pytest.raises(ValueError):
        AppConfig(**kwargs)


def test_gallery_configuration_validation_and_cwd_independence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_gallery_configs()
    config = {"test": {"type": "arca", "base_url": "https://arca.live/b/test", "channel_ids": ["1"]}}
    path = tmp_path / "galleries.json"
    for invalid in (["1", "1"], ["-1"], [1], [], ["١"]):
        config["test"]["channel_ids"] = invalid
        path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(ValueError):
            load_gallery_configs(path)


def gif_bytes():
    output = io.BytesIO()
    frames = [Image.new("RGB", (16, 16), color) for color in ("red", "green", "blue")]
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=50)
    return output.getvalue()


def test_animation_decoded_budget_is_aggregate(monkeypatch):
    data = gif_bytes()
    monkeypatch.setattr(app_config, "media_max_animation_pixels", 600)
    with pytest.raises(ValueError, match="frame/pixel budget"):
        ImageHandler.validate_image_data(data)
    monkeypatch.setattr(app_config, "media_max_animation_pixels", 1000)
    monkeypatch.setattr(app_config, "media_max_frames", 2)
    with pytest.raises(ValueError, match="frame/pixel budget"):
        ImageHandler.validate_image_data(data)
    monkeypatch.setattr(app_config, "media_max_frames", 3)
    ImageHandler.validate_image_data(data)


def test_log_filter_removes_transport_urls_credentials_and_traceback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "secret-token")
    record = logging.LogRecord("httpx", logging.WARNING, "", 1,
        "POST %s secret-token", ("https://api.telegram.org/botsecret-token/sendPhoto",),
        (OSError, OSError("private URL"), None))
    PrivacyFilter().filter(record)
    assert "secret-token" not in record.getMessage()
    assert "api.telegram" not in record.getMessage()
    assert "OSError" in record.getMessage()
    assert record.exc_info is None


def test_launchd_serializes_xml_metacharacters(tmp_path):
    path = tmp_path / "service.plist"
    root = '/Users/a & b/<project>"'
    args = [root + "/venv/bin/python", root + "/launcher.py"]
    write_plist(path, "test", root, root + "/log", args, {"NAME": "<&>"})
    with path.open("rb") as stream:
        value = plistlib.load(stream)
    assert value["ProgramArguments"] == args
    assert value["WorkingDirectory"] == root
    assert value["EnvironmentVariables"] == {"NAME": "<&>"}
    assert value["ExitTimeOut"] >= 90


@pytest.mark.asyncio
async def test_combined_runner_stops_web_after_bot_failure():
    from run_web_gallery import supervise
    class Server:
        started = False
        should_exit = False
        stopped = False
        async def serve(self):
            self.started = True
            try:
                while not self.should_exit:
                    await asyncio.sleep(0)
            finally:
                self.stopped = True
    server = Server()
    bot = SimpleNamespace(run_bot=AsyncMock(side_effect=RuntimeError("bot failure")))
    with pytest.raises(RuntimeError, match="bot failure"):
        await supervise(server, bot)
    assert server.stopped


@pytest.mark.parametrize("status", [404, 410, 403])
def test_deleted_page_is_distinct_from_temporary_or_access_failure(status):
    from Module.page_fetch import SourcePageGone, fetch_page
    response = MagicMock(status_code=status, headers={})
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    client = SimpleNamespace(get=MagicMock(return_value=response))
    expected = SourcePageGone if status in (404, 410) else requests.RequestException
    with pytest.raises(expected):
        fetch_page(client, "https://arca.live/b/test/1", "arcalive")


def test_honeypot_is_ram_only_by_default_and_disk_opt_in_rotates(monkeypatch, tmp_path):
    from honeypot_trap import HoneypotRecorder
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HONEYPOT_LOG_PATH", raising=False)
    recorder = HoneypotRecorder()
    kwargs = dict(source_ip="1.2.3.4", user_agent="x" * 1000, method="GET", path="/.env",
                  query_keys=[], category="scanner", matched_signature="test", status_code=404, response_shape="empty")
    for _ in range(101):
        recorder.record(**kwargs)
    assert len(recorder.events) == 100
    assert len(recorder.events[0]["user_agent"]) == 256
    assert not list(tmp_path.iterdir())
    recorder = HoneypotRecorder(tmp_path / "log.jsonl")
    recorder.max_log_bytes = 1000
    for _ in range(5):
        recorder.record(**kwargs)
    assert recorder.path.stat().st_size <= 1000
    assert recorder.path.with_suffix(".jsonl.1").stat().st_size <= 1000


@pytest.mark.asyncio
async def test_413_fallback_receipt_survives_cancellation_of_later_item(tmp_path):
    import discord

    from Module.message_sender import MessageSender
    calls = 0
    async def send(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise discord.HTTPException(SimpleNamespace(status=413, reason="large"), "large")
        if calls == 3:
            raise asyncio.CancelledError
    channel = SimpleNamespace(send=send)
    client = SimpleNamespace(get_channel=lambda _: channel)
    with DeliveryArchive(tmp_path / "receipt.sqlite3") as archive:
        pipeline = MediaPipeline(MessageSender(None, None), client, [1], telegram_enabled=False,
                                 delivery_archive=archive, source="arcalive", gallery_name="test")
        with pytest.raises(asyncio.CancelledError):
            await pipeline.send_discord_batch([media("a"), media("b")], title="", link=None)
        assert archive.check("arcalive", "test", destination_key("discord", "1", "a"))
        assert not archive.check("arcalive", "test", destination_key("discord", "1", "b"))


def test_process_leader_excludes_an_actual_second_process(tmp_path):
    import subprocess
    import sys

    from Module.process_leader import ProcessLeaderLock
    path = tmp_path / "leader.lock"
    code = "from Module.process_leader import ProcessLeaderLock; import sys; lock=ProcessLeaderLock(sys.argv[1]); print(int(lock.is_leader)); lock.close()"
    with_leader = ProcessLeaderLock(str(path))
    try:
        result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=10, check=True)
        assert result.stdout.strip() == "0"
    finally:
        with_leader.close()
    result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == "1"



def test_compressed_gif_reports_real_size_and_rewound_buffer():
    handler = ImageHandler()
    try:
        data = gif_bytes()
        output, size = handler.compress_gif(data, len(data) * 2, "x.gif")
        assert size == len(output.getvalue()) > 0
        assert output.tell() == 0
    finally:
        handler.session.close()


def test_dotenv_precedence_in_a_fresh_process(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    from Module.config import PROJECT_ROOT
    package = tmp_path / "Module"
    package.mkdir()
    (package / "__init__.py").touch()
    for name in ("config.py", "logging_policy.py", "url_policy.py"):
        shutil.copyfile(PROJECT_ROOT / "Module" / name, package / name)
    root_env = tmp_path / ".env"
    root_env.write_text("WEB_PORT=9002\n", encoding="utf-8")
    (package / ".env").write_text("WEB_PORT=9003\n", encoding="utf-8")
    code = "from Module.config import app_config; print(app_config.web_port)"
    environment = dict(os.environ, WEB_PORT="9001")
    def port():
        return subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=environment,
            capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    assert port() == "9001"
    del environment["WEB_PORT"]
    assert port() == "9002"
    root_env.unlink()
    assert port() == "9003"



def test_real_httpx_request_log_redacts_bot_url(monkeypatch, caplog):
    import httpx

    from Module.logging_policy import install_logging_privacy
    monkeypatch.setenv("TELEGRAM_TOKEN", "dummy-token-for-test")
    install_logging_privacy()
    with caplog.at_level(logging.INFO, logger="httpx"):
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            client.post("https://api.telegram.org/botdummy-token-for-test/sendPhoto")
    assert "HTTP Request" in caplog.text
    assert "dummy-token-for-test" not in caplog.text
    assert "api.telegram.org" not in caplog.text
