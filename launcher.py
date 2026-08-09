from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from urllib import error, request

import psutil
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 1) 프로젝트 루트 .env 우선 로드 (DISCORD_TOKEN 등 주요 환경변수)
load_dotenv()
# 2) Module/.env 로드 (ARCA_SOCKS_PROXY 등 Module 전용 변수, 기존값 덮어쓰기 허용)
env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "Module", ".env"))
load_dotenv(env_path, override=True)

# galleries.json에서 갤러리 목록 로드 (arca 갤러리 포함)
with open(os.path.join(os.path.dirname(__file__), "galleries.json"), encoding="utf-8") as f:
    gallery_configs = json.load(f)
gallery_names = list(gallery_configs.keys())

# DC/Arca 분리
dc_galleries = deque(g for g in gallery_names if gallery_configs[g].get("type") != "arca")
arca_galleries = deque(g for g in gallery_names if gallery_configs[g].get("type") == "arca")

processes = {}  # {gallery_name: (process, start_time)}
restart_failures: dict[str, int] = {}

MAX_DC = min(5, max(1, int(os.getenv("MAX_DC_CRAWLERS", "5"))))
MAX_ARCA = min(5, max(1, int(os.getenv("MAX_ARCA_CRAWLERS", "5"))))
BATCH_LIFETIME = max(60, int(os.getenv("CRAWLER_BATCH_SECONDS", "3600")))
RESTART_BACKOFF_MAX = 60

shutdown_requested = False


def _probe_ingest_auth(base_url: str) -> int | None:
    """빈 바디 + 토큰으로 ingest 인증만 검사한다. 415면 토큰 일치(빈 바디라 이미지 검증에서
    거절), 401이면 토큰 불일치. 연결 실패 등은 None."""
    req = request.Request(
        f"{base_url}/internal/images",
        data=b"",
        headers={
            "X-Ingest-Token": os.getenv("WEB_INGEST_TOKEN", ""),
            "Content-Type": "application/octet-stream",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=2) as response:
            return response.status
    except error.HTTPError as exc:
        return exc.code
    except (OSError, ValueError, error.URLError):
        return None


def wait_for_web_gallery() -> bool:
    """Wait until the in-memory web process can accept crawler uploads."""
    if os.getenv("WEB_GALLERY") != "1":
        return True

    base_url = os.getenv("WEB_GALLERY_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        timeout_seconds = max(1, int(os.getenv("WEB_READY_TIMEOUT_SECONDS", "60")))
    except ValueError:
        timeout_seconds = 60
        logger.warning("WEB_READY_TIMEOUT_SECONDS 값이 올바르지 않아 60초를 사용합니다.")

    health_url = f"{base_url}/healthz"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with request.urlopen(health_url, timeout=2) as response:
                health = json.load(response)
            if health.get("ingest_configured") is True:
                # healthz는 웹 쪽에 토큰이 "있다"는 것만 알려준다. 웹이 옛 .env로 기동 중이면
                # (토큰 회전 후 web 미재시작 등) 크롤러 업로드가 전부 401로 조용히 버려지므로,
                # 실제 토큰이 일치하는지까지 확인한 뒤에 크롤러를 시작한다.
                status = _probe_ingest_auth(base_url)
                if status in (200, 415):
                    logger.info("웹 갤러리 준비 완료 (ingest 토큰 인증 확인): %s", health_url)
                    return True
                if status == 401:
                    logger.error(
                        "WEB_INGEST_TOKEN 불일치: 웹 프로세스가 다른 토큰으로 기동 중입니다. "
                        "(.env 변경 후 dcselfie-web 미재시작이 흔한 원인 — web 재시작 필요)"
                    )
                else:
                    logger.warning("ingest 인증 프로브 응답 대기 중 (status=%s)", status)
            else:
                logger.warning("웹 갤러리 ingest가 아직 준비되지 않았습니다: %s", health_url)
        except (OSError, ValueError, error.URLError) as exc:
            logger.info("웹 갤러리 준비 대기 중 (%s): %s", health_url, exc)

        if time.monotonic() >= deadline:
            logger.error("웹 갤러리가 %s초 안에 준비되지 않았습니다: %s", timeout_seconds, health_url)
            return False
        time.sleep(1)

def signal_handler(sig: object, frame: object) -> object:
    global shutdown_requested
    logger.info("종료 신호 수신. 프로세스를 정리합니다...")
    shutdown_requested = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def is_already_running(gallery_name: object) -> object:
    """해당 갤러리가 이미 실행 중인지 확인"""
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"]
            if not cmdline:
                continue
            joined = " ".join(cmdline)
            if "run_gallery.py" in joined and gallery_name in joined:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False

def run_script(gallery_name: object) -> object:
    """run_gallery.py를 통해 갤러리 크롤러 실행 (DCInside/Arcalive 모두)"""
    python_executable = sys.executable
    process = subprocess.Popen(
        [python_executable, "run_gallery.py", gallery_name],
    )
    logger.info(f"{gallery_name} 크롤러 실행됨 (PID: {process.pid})")
    return process

def stop_processes(gallery_names: set[str], timeout: float = 10.0) -> None:
    """Terminate selected processes concurrently within one shared deadline."""
    running = [
        (gallery_name, entry[0])
        for gallery_name, entry in processes.items()
        if gallery_name in gallery_names and entry and entry[0] is not None
    ]
    for gallery_name, process in running:
        logger.info(f"{gallery_name} 크롤러 종료 중...")
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + timeout
    remaining = {name: process for name, process in running if process.poll() is None}
    while remaining and time.monotonic() < deadline:
        remaining = {name: process for name, process in remaining.items() if process.poll() is None}
        if remaining:
            time.sleep(0.1)

    for gallery_name, process in remaining.items():
        logger.warning("%s 크롤러가 제때 종료되지 않아 강제 종료합니다.", gallery_name)
        if process.poll() is None:
            process.kill()
    for process in remaining.values():
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logger.error("강제 종료 후에도 프로세스가 남아 있습니다: pid=%s", process.pid)
    for gallery_name in gallery_names:
        processes.pop(gallery_name, None)
        restart_failures.pop(gallery_name, None)


def stop_running_processes(timeout: float = 10.0) -> object:
    """Terminate the whole batch concurrently within one shared deadline."""
    stop_processes(set(processes), timeout)


def _restart_delay(failures: int) -> float:
    return min(RESTART_BACKOFF_MAX, 2 ** min(failures, 6))


def _pick_from_queue(queue: object, max_count: object, source_label: object) -> object:
    """큐에서 최대 max_count개의 갤러리를 선택 (실행 중이면 건너뜀)"""
    selected = 0
    launched = 0
    scheduled = 0
    skipped = 0
    for _ in range(len(queue)):
        if selected >= max_count:
            break
        if not queue:
            break
        gallery_name = queue.popleft()
        if not is_already_running(gallery_name):
            try:
                process = run_script(gallery_name)
            except (OSError, subprocess.SubprocessError) as exc:
                failures = restart_failures.get(gallery_name, 0) + 1
                restart_failures[gallery_name] = failures
                delay = _restart_delay(failures)
                processes[gallery_name] = (None, time.monotonic() + delay, failures)
                scheduled += 1
                logger.error(
                    "%s 크롤러 시작 실패(%s). %.1f초 후 다시 시도합니다.",
                    gallery_name,
                    exc,
                    delay,
                )
            else:
                processes[gallery_name] = (process, time.time())
                launched += 1
            selected += 1
        else:
            skipped += 1
        queue.append(gallery_name)

    if selected > 0:
        summary = [f"{launched}개 시작됨"]
        if scheduled:
            summary.append(f"{scheduled}개 재시작 대기")
        if skipped:
            summary.append(f"{skipped}개 이미 실행 중")
        logger.info("%s: %s", source_label, ", ".join(summary))
    return selected


def monitor_batch(expected_galleries: set[str]) -> None:
    """Restart only crashed members of the active platform-limited batch."""
    now = time.monotonic()
    for gallery_name in expected_galleries:
        entry = processes.get(gallery_name)
        if entry is None or len(entry) != 2:
            continue
        process, started_at = entry
        return_code = process.poll()
        if return_code is None:
            continue
        failures = restart_failures.get(gallery_name, 0) + 1
        restart_failures[gallery_name] = failures
        delay = _restart_delay(failures)
        logger.warning(
            "%s 크롤러 종료(code=%s, uptime=%.1fs). %.1f초 후 재시작합니다.",
            gallery_name,
            return_code,
            max(0.0, time.time() - started_at),
            delay,
        )
        processes[gallery_name] = (None, now + delay, failures)

    for gallery_name in expected_galleries:
        entry = processes.get(gallery_name)
        if not entry or len(entry) != 3:
            continue
        _, restart_at, failures = entry
        if now < restart_at:
            continue
        try:
            process = run_script(gallery_name)
        except (OSError, subprocess.SubprocessError) as exc:
            failures += 1
            restart_failures[gallery_name] = failures
            delay = _restart_delay(failures)
            processes[gallery_name] = (None, now + delay, failures)
            logger.error(
                "%s 크롤러 재시작 실패(%s). %.1f초 후 다시 시도합니다.",
                gallery_name,
                exc,
                delay,
            )
            continue
        processes[gallery_name] = (process, time.time())

def manage_crawlers() -> object:
    """Run independent DC and Arca batches, each capped at five crawlers."""
    logger.info(f"DC 갤러리: {len(dc_galleries)}개, Arca 갤러리: {len(arca_galleries)}개")
    _pick_from_queue(dc_galleries, MAX_DC, "DC")
    active_dc = {name for name in processes if gallery_configs[name].get("type") != "arca"}
    _pick_from_queue(arca_galleries, MAX_ARCA, "Arca")
    active_arca = {name for name in processes if gallery_configs[name].get("type") == "arca"}

    now = time.monotonic()
    next_dc_rotation = now + BATCH_LIFETIME if len(dc_galleries) > MAX_DC else float("inf")
    next_arca_rotation = now + BATCH_LIFETIME if len(arca_galleries) > MAX_ARCA else float("inf")

    while not shutdown_requested:
        now = time.monotonic()
        monitor_batch(active_dc | active_arca)

        if now >= next_dc_rotation:
            logger.info("DC 배치만 다음 갤러리로 교체합니다.")
            stop_processes(active_dc)
            _pick_from_queue(dc_galleries, MAX_DC, "DC")
            active_dc = {name for name in processes if gallery_configs[name].get("type") != "arca"}
            next_dc_rotation = now + BATCH_LIFETIME

        if now >= next_arca_rotation:
            logger.info("Arca 배치만 다음 갤러리로 교체합니다.")
            stop_processes(active_arca)
            _pick_from_queue(arca_galleries, MAX_ARCA, "Arca")
            active_arca = {name for name in processes if gallery_configs[name].get("type") == "arca"}
            next_arca_rotation = now + BATCH_LIFETIME

        time.sleep(1)

def main() -> object:
    """메인 실행 함수"""
    logger.info("크롤러 관리 프로세스를 시작합니다.")
    if not wait_for_web_gallery():
        raise SystemExit("웹 갤러리 준비 실패")
    try:
        manage_crawlers()
    finally:
        stop_running_processes()
        logger.info("크롤러 관리 프로세스가 종료되었습니다.")

if __name__ == "__main__":
    main()
