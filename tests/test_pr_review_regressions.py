"""Boundary and lifecycle regressions from PR #121 review."""
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from honeypot_trap import HoneypotRecorder


@pytest.mark.parametrize("cancel", [False, True])
def test_stop_iteration_worker_finishes_even_during_cancellation(cancel):
    # A process timeout also detects a broken asyncio Future: wait_for cannot
    # bound run_blocking's intentional cancellation join on Python 3.11.
    code = '''
import asyncio
import threading
from Module.lifecycle import run_blocking

async def main(cancel):
    started, finish = threading.Event(), threading.Event()
    def worker():
        started.set()
        assert finish.wait(2)
        return next(iter([]))
    task = asyncio.create_task(run_blocking(worker))
    while not started.is_set():
        await asyncio.sleep(0)
    if cancel:
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finish.set()
    try:
        await task
    except asyncio.CancelledError:
        assert cancel
    except RuntimeError as exc:
        assert not cancel
        assert isinstance(exc.__cause__, StopIteration)
    else:
        raise AssertionError("worker failure was swallowed")
    assert await run_blocking(lambda value, *, extra: value + extra, 2, extra=3) == 5

asyncio.run(main(CANCEL))
'''.replace("CANCEL", repr(cancel))
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10,
                   capture_output=True, text=True)


@pytest.mark.parametrize("destination_kind", ["constructor", "environment"])
@pytest.mark.parametrize("request_path", [
    "../../escaped.jsonl", "/tmp/escaped.jsonl", r"C:\escaped.jsonl",
    "/%2e%2e/%2e%2e/escaped.jsonl", '/.env\n{"path":"../../escaped.jsonl"}',
])
def test_request_path_is_only_log_data(tmp_path, monkeypatch, destination_kind, request_path):
    working = tmp_path / "app"
    working.mkdir()
    monkeypatch.chdir(working)
    # Operators may select a destination outside the working directory.
    log = tmp_path / "logs" / "events.jsonl"
    monkeypatch.delenv("HONEYPOT_LOG_PATH", raising=False)
    if destination_kind == "environment":
        monkeypatch.setenv("HONEYPOT_LOG_PATH", str(Path("..") / "logs" / log.name))
        recorder = HoneypotRecorder()
    else:
        recorder = HoneypotRecorder(log)
    recorder.max_log_bytes = 1  # Exercise rotation as well as the initial open.
    for _ in range(2):
        recorder.record(source_ip="127.0.0.1", user_agent="GPTBot", method="GET",
                        path=request_path, query_keys=[], category="scanner",
                        matched_signature="test", status_code=404, response_shape="empty")
    backup = log.with_name(log.name + ".1")
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == {log, backup}
    for path in (log, backup):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["path"] == request_path


def test_launchd_cli_keeps_payload_in_operator_selected_plist(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "write_launchd_plist.py"
    destination = tmp_path / "LaunchAgents"
    destination.mkdir()
    path = destination / "service.plist"
    payload = '../../escaped & <tag> "value"'
    subprocess.run([sys.executable, str(script), str(path), "test.web", payload,
                    payload, "python", payload], cwd=tmp_path, check=True, timeout=10)
    assert list(destination.iterdir()) == [path]
    assert not (tmp_path / "escaped").exists()
    with path.open("rb") as stream:
        value = plistlib.load(stream)
    assert value["WorkingDirectory"] == payload
    assert value["ProgramArguments"] == ["python", payload]
    assert value["StandardOutPath"] == payload
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
