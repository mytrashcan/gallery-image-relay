import io
from collections import deque
from contextlib import nullcontext
from unittest.mock import MagicMock

import launcher

HEALTH_OK = b'{"ingest_configured": true}'


def fake_urlopen(*, probe_status: int):
    """healthz GET은 정상 응답, ingest 프로브 POST는 지정한 HTTP 상태를 돌려주는 가짜."""

    def _urlopen(url_or_request, timeout):
        if isinstance(url_or_request, launcher.request.Request):
            raise launcher.error.HTTPError(
                url_or_request.full_url, probe_status, "probe", None, None
            )
        return nullcontext(io.BytesIO(HEALTH_OK))

    return _urlopen


def test_wait_for_web_gallery_skips_when_gallery_disabled(monkeypatch):
    monkeypatch.delenv("WEB_GALLERY", raising=False)

    assert launcher.wait_for_web_gallery() is True


def test_wait_for_web_gallery_requires_ready_ingest_and_matching_token(monkeypatch):
    monkeypatch.setenv("WEB_GALLERY", "1")
    monkeypatch.setenv("WEB_GALLERY_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WEB_INGEST_TOKEN", "tok")
    # 빈 바디 프로브는 인증 통과 시 이미지 검증 단계에서 415로 거절된다 = 토큰 일치.
    monkeypatch.setattr("launcher.request.urlopen", fake_urlopen(probe_status=415))

    assert launcher.wait_for_web_gallery() is True


def test_wait_for_web_gallery_fails_on_token_mismatch(monkeypatch):
    monkeypatch.setenv("WEB_GALLERY", "1")
    monkeypatch.setenv("WEB_GALLERY_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WEB_INGEST_TOKEN", "rotated-token")
    monkeypatch.setenv("WEB_READY_TIMEOUT_SECONDS", "1")
    # 웹 프로세스가 옛 토큰으로 기동 중 → 프로브가 계속 401 → 시간 내 준비 실패로 종료해야 한다.
    monkeypatch.setattr("launcher.request.urlopen", fake_urlopen(probe_status=401))
    monkeypatch.setattr("launcher.time.sleep", lambda seconds: None)
    ticks = iter([0.0, 0.5, 1.5, 2.5, 3.5])
    monkeypatch.setattr("launcher.time.monotonic", lambda: next(ticks))

    assert launcher.wait_for_web_gallery() is False


def test_platform_limits_are_independent_and_capped_at_five():
    assert 1 <= launcher.MAX_DC <= 5
    assert 1 <= launcher.MAX_ARCA <= 5


def test_pick_from_queue_schedules_spawn_failure_for_retry(monkeypatch):
    launcher.processes.clear()
    monkeypatch.setattr("launcher.is_already_running", lambda gallery_name: False)
    monkeypatch.setattr("launcher.time.monotonic", lambda: 50.0)
    monkeypatch.setattr("launcher.run_script", MagicMock(side_effect=OSError("fork failed")))

    selected = launcher._pick_from_queue(deque(["dc"]), 1, "DC")

    assert selected == 1
    assert launcher.processes["dc"] == launcher.CrawlerProcessState(
        process=None,
        started_at=None,
        restart_at=52.0,
        failures=1,
    )
    launcher.processes.clear()


def test_monitor_batch_restarts_only_crashed_process(monkeypatch):
    healthy = MagicMock()
    healthy.poll.return_value = None
    crashed = MagicMock()
    crashed.poll.return_value = 1
    restarted = MagicMock()
    restarted.poll.return_value = None
    launcher.processes.clear()
    launcher.processes.update({
        "dc-a": launcher.CrawlerProcessState(healthy, 1.0, None, 0),
        "dc-b": launcher.CrawlerProcessState(crashed, 1.0, None, 0),
    })
    monkeypatch.setattr("launcher.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("launcher.time.time", lambda: 100.0)
    monkeypatch.setattr("launcher.run_script", MagicMock(return_value=restarted))

    launcher.monitor_batch({"dc-a", "dc-b"})

    assert launcher.processes["dc-a"].process is healthy
    assert launcher.processes["dc-b"].process is None
    assert launcher.processes["dc-b"].failures == 1

    monkeypatch.setattr("launcher.time.monotonic", lambda: 103.0)
    launcher.monitor_batch({"dc-a", "dc-b"})
    assert launcher.processes["dc-b"].process is restarted
    launcher.processes.clear()


def test_monitor_batch_reschedules_when_restart_spawn_fails(monkeypatch):
    launcher.processes.clear()
    launcher.processes["dc"] = launcher.CrawlerProcessState(None, None, 99.0, 1)
    monkeypatch.setattr("launcher.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("launcher.run_script", MagicMock(side_effect=OSError("fork failed")))

    launcher.monitor_batch({"dc"})

    assert launcher.processes["dc"] == launcher.CrawlerProcessState(
        process=None,
        started_at=None,
        restart_at=104.0,
        failures=2,
    )
    launcher.processes.clear()


def test_stopping_dc_batch_does_not_stop_arca(monkeypatch):
    dc = MagicMock()
    dc.poll.side_effect = [None, 0]
    arca = MagicMock()
    arca.poll.return_value = None
    launcher.processes.clear()
    launcher.processes.update({
        "dc": launcher.CrawlerProcessState(dc, 1.0, None, 0),
        "arca": launcher.CrawlerProcessState(arca, 1.0, None, 0),
    })
    monkeypatch.setattr("launcher.time.monotonic", lambda: 1.0)

    launcher.stop_processes({"dc"}, timeout=0)

    dc.terminate.assert_called_once()
    arca.terminate.assert_not_called()
    assert "dc" not in launcher.processes
    assert "arca" in launcher.processes
    launcher.processes.clear()
