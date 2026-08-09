from __future__ import annotations

import io

from rich.console import Console

import dashboard


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
