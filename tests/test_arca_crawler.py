"""ArcaliveCrawler characterization 테스트.

파싱 로직(목록/이미지 추출)을 HTML fixture + 세션 mock으로 고정한다.
아카라이브 HTML 구조가 바뀌면 CI에서 즉시 감지된다.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bs4 import BeautifulSoup

from Module.arca_crawler import (
    ArcaliveCrawler,
    _create_session,
    _fixed_arca_url,
    _is_allowed_image_url,
    _mask_proxy,
)
from Module.retry_policy import RetryPolicy


class FakeResponse:
    def __init__(self, text: object) -> None:
        self.text = text

    def raise_for_status(self) -> object:
        pass


class FakeSession:
    """crawler.session 을 대체 — 미리 준비한 HTML을 그대로 돌려준다."""

    def __init__(self, html_by_url: object=None, default_html: object="") -> None:
        self.html_by_url = html_by_url or {}
        self.default_html = default_html
        self.requested = []

    def get(self, url: object, **kwargs: object) -> object:
        self.requested.append(url)
        return FakeResponse(self.html_by_url.get(url, self.default_html))


def make_crawler(session: object=None) -> object:
    c = ArcaliveCrawler("https://arca.live/b/genshin")
    if session is not None:
        c.session = session
    return c


def soup_one(html: object, selector: object) -> object:
    return BeautifulSoup(html, "lxml").select_one(selector)


# ---------- 순수 파싱 ----------

def test_extract_post_id() -> None:
    assert ArcaliveCrawler._extract_post_id("/b/genshin/12345?p=2") == "12345"
    assert ArcaliveCrawler._extract_post_id("/b/genshin/") == ""
    assert ArcaliveCrawler._extract_post_id("garbage") == ""


def test_mask_proxy_hides_credentials() -> None:
    assert _mask_proxy("socks5://user:pass@p.webshare.io:1080") == "socks5://***:***@p.webshare.io:1080"
    # 자격증명 없는 URL은 그대로
    assert _mask_proxy("socks5h://p.proxy.net:9000") == "socks5h://p.proxy.net:9000"


def test_parse_column_row() -> None:
    c = make_crawler(FakeSession())
    html = (
        '<a class="vrow column" href="/b/genshin/777?p=1">'
        '<span class="title"> 제목입니다 </span>'
        '<span class="media-icon"></span></a>'
    )
    row = soup_one(html, "a.vrow.column")
    post = c._parse_column_row(row)
    # link 은 href 를 그대로 절대경로화(쿼리 유지), post_id 는 숫자만 추출
    assert post == {
        "link": "https://arca.live/b/genshin/777?p=1",
        "title": "제목입니다",
        "post_id": "777",
    }


def test_parse_column_row_without_image_returns_none() -> None:
    c = make_crawler(FakeSession())
    html = '<a class="vrow column" href="/b/genshin/1"><span class="title">t</span></a>'
    assert c._parse_column_row(soup_one(html, "a.vrow.column")) is None


def test_parse_hybrid_row() -> None:
    c = make_crawler(FakeSession())
    html = (
        '<div class="vrow hybrid">'
        '<a class="title hybrid-title" href="/b/hotdeal/42">핫딜</a>'
        '<span class="media-icon"></span></div>'
    )
    post = c._parse_hybrid_row(soup_one(html, "div.vrow.hybrid"))
    assert post["title"] == "핫딜"
    assert post["post_id"] == "42"
    assert post["link"] == "https://arca.live/b/hotdeal/42"


def test_parse_rows_reject_notices_and_external_links() -> None:
    c = make_crawler(FakeSession())
    notice = soup_one(
        '<a class="vrow column notice notice-board" href="/b/genshin/1">'
        '<span class="title">공지</span><span class="media-icon"></span></a>',
        "a.vrow.column",
    )
    external = soup_one(
        '<a class="vrow column" href="https://evil.example/b/genshin/2">'
        '<span class="title">외부</span><span class="media-icon"></span></a>',
        "a.vrow.column",
    )

    assert c._parse_column_row(notice) is None
    assert c._parse_column_row(external) is None


def test_fixed_arca_url_strips_fragment_and_rejects_unsafe_urls() -> None:
    assert _fixed_arca_url("https://arca.live", "/b/test/1?x=1#fragment") == (
        "https://arca.live/b/test/1?x=1"
    )
    assert _fixed_arca_url("https://arca.live", "https://evil.example/b/test/1") is None
    assert _fixed_arca_url("https://arca.live", "https://arca.live:444/b/test/1") is None


# ---------- 이미지 수집 ----------

def _collect(img_html: object) -> object:
    c = make_crawler(FakeSession())
    img = soup_one(img_html, "img")
    images, seen = [], set()
    c._collect_image(img, images, seen, "https://arca.live/b/genshin/1")
    return images


def test_collect_namu_image() -> None:
    images = _collect('<img src="//ac-p1.namu.la/2026/abc123.png">')
    assert len(images) == 1
    assert images[0].primary_url == (
        "https://ac-o.namu.la/2026/abc123.png?type=orig"
    )
    assert images[0].fallback_urls == (
        "https://ac-p1.namu.la/2026/abc123.png",
    )
    assert images[0].filename_hint == "abc123.png"


def test_collect_prefers_original_url() -> None:
    images = _collect(
        '<img src="//ac-p1.namu.la/thumb/x.webp" data-originalurl="https://ac-o.namu.la/orig/x.png">'
    )
    assert images[0].primary_url == "https://ac-o.namu.la/orig/x.png?type=orig"
    assert images[0].fallback_urls[0] == "https://ac-o.namu.la/orig/x.png"


def test_collect_uses_data_orig_extension_with_original_as_fallback() -> None:
    images = _collect(
        '<img src="//ac-p1.namu.la/2026/clip.webp?token=x" data-orig="gif">'
    )

    assert images[0].primary_url == (
        "https://ac-o.namu.la/2026/clip.gif?type=orig&token=x"
    )
    assert images[0].fallback_urls[:2] == (
        "https://ac-o.namu.la/2026/clip.webp?type=orig&token=x",
        "https://ac-p1.namu.la/2026/clip.webp?token=x",
    )


def test_collect_skips_emoticon() -> None:
    assert _collect('<img class="arca-emoticon" src="//ac-p1.namu.la/emo/e.png">') == []
    assert _collect('<img data-type="emoticon" src="//ac-p1.namu.la/emo/e.png">') == []


def test_collect_skips_non_namu() -> None:
    assert _collect('<img src="//cdn.other.com/x.png">') == []


def test_image_url_validation_accepts_supported_hosts_only() -> None:
    assert _is_allowed_image_url("https://ac-p1.namu.la/x.png") is True
    assert _is_allowed_image_url("https://arca.live/x.png") is True
    assert _is_allowed_image_url("http://ac-p1.namu.la/x.png") is False
    assert _is_allowed_image_url("https://ac-p1.namu.la:444/x.png") is False


def test_collect_dedup_by_clean_url() -> None:
    c = make_crawler(FakeSession())
    images, seen = [], set()
    for _ in range(2):
        img = soup_one('<img src="//ac-p1.namu.la/2026/dup.png?type=orig">', "img")
        c._collect_image(img, images, seen, "")
    assert len(images) == 1


def test_extract_all_images_from_article_body() -> None:
    post_url = "https://arca.live/b/genshin/500"
    html = (
        '<html><body><div class="article-body">'
        '<img src="//ac-p1.namu.la/2026/one.png">'
        '<img class="arca-emoticon" src="//ac-p1.namu.la/emo/e.png">'
        '<img src="//ac-p1.namu.la/2026/two.jpg">'
        '</div></body></html>'
    )
    c = make_crawler(FakeSession({post_url: html}))
    images = c.extract_all_images(post_url)
    assert [i.filename_hint for i in images] == ["one.png", "two.jpg"]
    assert all(i.headers == {"Referer": post_url} for i in images)
    assert all(i.source_post_id == "500" for i in images)


# ---------- 목록 + dedup ----------

def test_get_latest_posts_dedups_across_calls(monkeypatch: object) -> None:
    monkeypatch.setattr("Module.arca_crawler.POST_SKIP_COUNT", 0)
    rows = "".join(
        f'<a class="vrow column" href="/b/genshin/{i}">'
        f'<span class="title">글{i}</span><span class="media-icon"></span></a>'
        for i in range(3)
    )
    base_url = "https://arca.live/b/genshin"
    html = f"<html><body>{rows}</body></html>"
    c = make_crawler(FakeSession({base_url: html}))

    first = c.get_latest_posts(max_posts=10)
    assert {p["post_id"] for p in first} == {"0", "1", "2"}
    for post in first:
        c.mark_sent(post["post_id"])
    # 같은 목록을 다시 크롤 → 이미 본 글이라 없음
    assert c.get_latest_posts(max_posts=10) == []


def test_get_latest_posts_allows_same_title_for_different_post_ids(monkeypatch: object) -> None:
    monkeypatch.setattr("Module.arca_crawler.POST_SKIP_COUNT", 0)
    rows = "".join(
        f'<a class="vrow column" href="/b/genshin/{i}">'
        '<span class="title">같은 제목</span><span class="media-icon"></span></a>'
        for i in range(2)
    )
    base_url = "https://arca.live/b/genshin"
    c = make_crawler(FakeSession({base_url: rows}))

    assert [post["post_id"] for post in c.get_latest_posts(max_posts=10)] == ["0", "1"]


def test_get_latest_posts_reports_cloudflare_challenge_without_retry() -> None:
    response = MagicMock()
    response.status_code = 503
    response.headers = {"Content-Type": "text/html"}
    response.text = (
        "<html><title>Just a moment...</title>"
        "<script src='/cdn-cgi/challenge-platform/x'></script></html>"
    )
    session = MagicMock()
    session.get.return_value = response
    crawler = ArcaliveCrawler(
        "https://arca.live/b/genshin",
        session,
        retry_policy=RetryPolicy(base_delay=0),
    )

    assert crawler.get_latest_posts() == []
    session.get.assert_called_once()


def test_create_session_has_browser_headers(monkeypatch: object) -> None:
    monkeypatch.delenv("ARCA_SOCKS_PROXY", raising=False)
    s = _create_session()
    assert "Mozilla" in s.headers.get("User-Agent", "")
    assert not s.proxies  # 프록시 미설정 시 비어 있어야
