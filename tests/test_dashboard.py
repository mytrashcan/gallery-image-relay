from __future__ import annotations

import io

from rich.console import Console

import dashboard


def test_scan_procs_matches_gallery_argument_exactly(monkeypatch) -> None:
    from types import SimpleNamespace

    def proc(*argv: str) -> SimpleNamespace:
        return SimpleNamespace(info={"pid": 1, "cmdline": list(argv), "create_time": 0.0})

    genshin = proc("python3", "/srv/app/run_gallery.py", "arca_genshin")
    zzz = proc("python3", "/srv/app/run_gallery.py", "zzz")
    other = proc("python3", "/srv/app/run_gallery.py", "unknown")
    monkeypatch.setattr(dashboard.psutil, "process_iter", lambda attrs: [genshin, zzz, other])
    monkeypatch.setattr(dashboard, "_configs", lambda: {"arca": {}, "arca_genshin": {}, "zzz": {}})

    crawlers, _ = dashboard._scan_procs()

    assert crawlers == {"arca_genshin": genshin, "zzz": zzz}


def test_services_panel_handles_stale_empty_feed() -> None:
    panel = dashboard._services_panel(
        {
            "ok": True,
            "items": 0,
            "ttl": 3600,
            "memory_bytes": 0,
            "memory_limit_bytes": 64 * 1024 * 1024,
            "fresh": False,
            "latest_age_seconds": None,
        },
        {},
    )
    output = io.StringIO()

    Console(file=output, color_system=None, width=100).print(panel)

    assert "수집 없음" in output.getvalue()
