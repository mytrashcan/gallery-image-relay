"""Validate destinations before a request, including each redirect hop."""
from urllib.parse import parse_qs, urlsplit


def https_host(url: str, hosts: set[str]) -> bool:
    try:
        p = urlsplit(url)
        return (not any(ord(c) < 33 for c in url) and p.scheme == "https"
                and p.hostname in hosts and p.username is None
                and p.password is None and p.port is None)
    except (ValueError, TypeError):
        return False


def source_page(url: str, source: str) -> bool:
    hosts = {"arca.live"} if source == "arcalive" else {"gall.dcinside.com"}
    if not https_host(url, hosts):
        return False
    p = urlsplit(url)
    if source == "arcalive":
        return p.path.startswith("/b/")
    return "/board/" in p.path and bool(parse_qs(p.query).get("id"))
