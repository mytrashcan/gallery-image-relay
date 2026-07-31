"""Typed source-media candidates shared by crawler download paths."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MediaCandidate:
    """An ordered set of URLs that may resolve to the same source media."""

    primary_url: str
    fallback_urls: tuple[str, ...] = ()
    filename_hint: str | None = None
    headers: dict[str, str] | None = None
    source_post_id: str | None = None
    expected_media_type: str | None = None
