"""Source HTML uses the same bounded, redirect-checked transport as media."""
import requests

from Module.media_download import MediaDownloadRejected, download_limited
from Module.url_policy import source_page


class SourcePageGone(Exception):
    """An upstream 404/410 confirms a removed post."""


def fetch_page(session, url, source, retry_policy=None):
    try:
        return download_limited(
            session, url, headers=None, timeout=15, max_bytes=4 * 1024 * 1024,
            retry_policy=retry_policy,
            is_allowed_url=lambda candidate: source_page(candidate, source),
        ).decode("utf-8", errors="replace")
    except MediaDownloadRejected as exc:
        response = getattr(exc.__cause__, "response", None)
        if getattr(response, "status_code", None) in {404, 410}:
            raise SourcePageGone from exc
        # An unavailable/changed HTML page is not a permanently rejected image.
        raise requests.RequestException("source page rejected") from exc
