"""터미널 모니터 대시보드.

웹 갤러리(/healthz, /feed)와 크롤러 프로세스(psutil) 상태를 한 화면에서 라이브로 본다.

  python dashboard.py            # 2초마다 갱신 (Ctrl+C 종료)
  python dashboard.py --once     # 한 프레임만 출력 (스크립트/점검용)
  python dashboard.py -i 1       # 갱신 주기 1초

연결 대상 포트는 WEB_PORT(기본 8000), 호스트는 DASH_HOST(기본 127.0.0.1).

원격(다른 기기, 예: 맥에서 OCI 서버) 모니터링:
  DASH_BASE_URL=https://dcselfie.win python dashboard.py
서비스 상태/최근 피드는 공개 API로 그대로 보이지만, 크롤러 프로세스(PID/메모리)는
psutil이 로컬 프로세스만 읽을 수 있어 원격에서는 볼 수 없다 — 그건 SSH로 서버에
들어가 이 스크립트를 직접 돌리거나 `./dcselfie.sh status`로 확인해야 한다.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime

import psutil
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from Module.config import app_config

BASE = app_config.dash_base_url or f"http://{app_config.dash_host}:{app_config.web_port}"
REMOTE = bool(app_config.dash_base_url)  # 원격(공개 API)이면 크롤러 프로세스는 이 기기에서 안 보임

BANNER = r"""
 ___  ___ ___ __  __ ___    ___   _   _    _    ___ _____   __
|   \/ __|_ _|  \/  / __|  / __| /_\ | |  | |  | __| _ \ \ / /
| |) \__ \| || |\/| \__ \ | (_ |/ _ \| |__| |__| _||   /\ V /
|___/|___/___|_|  |_|___/  \___/_/ \_\____|____|___|_|_\_|_|
        e p h e m e r a l   i m a g e   f e e d   m o n i t o r
"""

ROSE = "#e60023"
ARCA_BLUE = "#00A3FF"

def _configs() -> object:
    try:
        with open("galleries.json", encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def _gallery_type(name: object) -> object:
    """갤러리 소스 타입 (dc/arca)"""
    return "arca" if _configs().get(name, {}).get("type") == "arca" else "dc"


_UA = "Mozilla/5.0 (compatible; dcselfie-dashboard/1.0)"


def _fetch(path: object, timeout: object=2.0 if not REMOTE else 5.0) -> object:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _scan_procs() -> object:
    """run_gallery.py / launcher.py / run_web_server.py 프로세스 수집."""
    crawlers, services = {}, {}
    for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "run_gallery.py" in cmd:
            for g in _configs():
                if f" {g}" in cmd or cmd.endswith(g):
                    crawlers[g] = p
        elif "launcher.py" in cmd:
            services["launcher"] = p
        elif "run_web_server.py" in cmd or "run_web_gallery.py" in cmd:
            services["web"] = p
    return crawlers, services


def _uptime(p: object) -> object:
    try:
        secs = int(time.time() - p.info["create_time"])
    except (KeyError, psutil.NoSuchProcess):
        return "-"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _rss_mb(p: object) -> object:
    try:
        return p.memory_info().rss / 1024 / 1024
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0


def _time_ago(epoch: object) -> object:
    diff = int(time.time() - epoch)
    if diff < 60:
        return "방금"
    if diff < 3600:
        return f"{diff // 60}분 전"
    if diff < 86400:
        return f"{diff // 3600}시간 전"
    return f"{diff // 86400}일 전"


def _services_panel(health: object, services: object) -> object:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="bold")
    t.add_column()

    if health and health.get("ok"):
        web = Text("● UP", style="bold green")
        items = f"[bold]{health.get('items', 0)}[/] / {app_config.web_feed_max_items}"
        ttl_h = health.get("ttl", 0) / 3600
        ttl = f"{ttl_h:.1f}h"
        used_mb = health.get("memory_bytes", 0) / 1024 / 1024
        limit_mb = health.get("memory_limit_bytes", 0) / 1024 / 1024
        memory = f"{used_mb:.1f} / {limit_mb:.0f} MB"
        latest_age = health.get("latest_age_seconds")
        if health.get("fresh", True):
            freshness = Text("정상", style="green")
        elif latest_age is None:
            freshness = Text("지연 (수집 없음)", style="bold yellow")
        else:
            freshness = Text(f"지연 ({latest_age:.0f}s)", style="bold yellow")
    else:
        web = Text("● DOWN", style="bold red")
        items, ttl, memory, freshness = "-", "-", "-", Text("-", style="dim")

    if health and health.get("maintenance"):
        maint = Text("🛠 점검중 (down)", style="bold yellow")
    elif health and health.get("ok"):
        maint = Text("정상 운영", style="green")
    else:
        maint = Text("-", style="dim")

    t.add_row("웹 서버", web)
    t.add_row("주소", f"[link={BASE}]{BASE}[/]")
    t.add_row("피드 이미지", items)
    t.add_row("메모리", memory)
    t.add_row("수집 상태", freshness)
    t.add_row("TTL", ttl)
    t.add_row("운영 상태", maint)

    if REMOTE:
        t.add_row("모드", Text("🌐 원격 (공개 API)", style="cyan"))
    else:
        launcher = Text("● 실행중", style="green") if "launcher" in services else Text("○ 꺼짐", style="dim")
        websvc = Text("● 실행중", style="green") if "web" in services else Text("○ 꺼짐", style="dim")
        t.add_row("launcher", launcher)
        t.add_row("web process", websvc)
    return Panel(t, title="[bold]서비스 상태", border_style=ROSE, width=42)


def _crawlers_panel(crawlers: object) -> object:
    configs = _configs()
    galleries = list(configs.keys())

    # DC / Arca 분리
    dc_galleries = [g for g in galleries if configs[g].get("type") != "arca"]
    arca_galleries = [g for g in galleries if configs[g].get("type") == "arca"]

    def _crawler_table(g_list: object, title: object, border_style: object) -> object:
        table = Table(expand=True, border_style="grey39")
        table.add_column("갤러리", style="bold")
        table.add_column("상태", justify="center")
        table.add_column("PID", justify="right", style="dim")
        table.add_column("메모리", justify="right")
        table.add_column("업타임", justify="right", style="cyan")

        running = 0
        for g in g_list:
            p = crawlers.get(g)
            if p:
                running += 1
                table.add_row(g, Text("● 크롤중", style="green"), str(p.info["pid"]),
                              f"{_rss_mb(p):.0f}MB", _uptime(p))
            else:
                table.add_row(g, Text("○ 대기", style="dim"), "-", "-", "-")

        title_text = f"[bold]{title}[/]  ([green]{running}[/]/{len(g_list)} 실행중)"
        return Panel(table, title=title_text, border_style=border_style)

    top = Table.grid(expand=True)
    top.add_column(ratio=1)
    top.add_column(ratio=1)
    top.add_row(
        _crawler_table(dc_galleries, "DCInside", ROSE),
        _crawler_table(arca_galleries, "Arcalive", ARCA_BLUE),
    )
    return top


def _remote_note_panel() -> object:
    msg = Text.from_markup(
        "🌐 원격 모드입니다 — 크롤러 프로세스(PID/메모리/업타임)는 psutil이 "
        "로컬 프로세스만 읽을 수 있어 여기서는 볼 수 없습니다.\n"
        "서버에 직접 들어가서 확인하세요:\n\n"
        "  [bold]ssh <서버> \"cd dcinsideImageCrawler && ./dcselfie.sh status\"[/]\n"
        "  [bold]ssh <서버> \"cd dcinsideImageCrawler && python3 dashboard.py\"[/]",
        style="grey62",
    )
    return Panel(msg, title="[bold]크롤러 (원격에서는 미지원)", border_style="grey39")


def _recent_panel(feed: object) -> object:
    table = Table(expand=True, border_style="grey39")
    table.add_column("최근 자짤", style="bold", ratio=3, no_wrap=True)
    table.add_column("시간", justify="right", ratio=1, style="grey62")

    if not feed:
        table.add_row(Text("아직 수집된 이미지가 없습니다", style="dim"), "")
    else:
        for it in feed[:12]:
            label = it.get("title") or "자짤"
            table.add_row(label, _time_ago(it.get("created_at", time.time())))
    return Panel(table, title="[bold]실시간 피드", border_style=ROSE)


def render() -> object:
    health = _fetch("/healthz")
    feed = _fetch("/feed?limit=12") or []
    crawlers, services = ({}, {}) if REMOTE else _scan_procs()

    banner = Align.center(Text(BANNER, style=f"bold {ROSE}"))
    clock_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + (f"   [{BASE}]" if REMOTE else "")
    clock = Align.center(Text(clock_label, style="grey62"))

    top = Table.grid(expand=True)
    top.add_column(width=42)
    top.add_column(ratio=1)
    top.add_row(_services_panel(health, services), _recent_panel(feed))

    bottom = _remote_note_panel() if REMOTE else _crawlers_panel(crawlers)
    return Group(banner, clock, Text(), top, Text(), bottom)


def main() -> object:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="한 프레임만 출력")
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="갱신 주기(초)")
    args = ap.parse_args()

    console = Console()
    if args.once:
        console.print(render())
        return

    try:
        with Live(render(), console=console, refresh_per_second=4, screen=True) as live:
            while True:
                time.sleep(args.interval)
                live.update(render())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
