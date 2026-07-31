"""아카라이브(Arcalive) 전용 크롤러.

Gallery Image Relay의 Module/crawler.py와 동일한 인터페이스를 제공하지만:
- cloudscraper로 요청 (맥 IP 경유 시 Cloudflare 챌린지 미발생)
- 게시글 내 모든 이미지를 추출 (DCInside는 최상단 1개만)
- 아카라이브 전용 HTML 셀렉터 사용
"""
import logging
import os
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import cloudscraper
import requests
from bs4 import BeautifulSoup, SoupStrainer

from Module.config import app_config
from Module.lru_cache import LRUCache
from Module.media_candidate import MediaCandidate
from Module.retry_policy import (
    BlockedByChallenge,
    RetryPolicy,
    request_with_policy,
)

logger = logging.getLogger(__name__)

ARCA_BASE = "https://arca.live"
_VROW_STRAINER = SoupStrainer(attrs={"class": re.compile(r"\bvrow\b")})
POST_SKIP_COUNT = 10
_ORIGINAL_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "bmp"})

_PAGE_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    backoff="exponential",
    base_delay=1.0,
    max_delay=4.0,
)


def _mask_proxy(url: str) -> str:
    """프록시 URL의 자격증명(user:pass@)을 로그에 노출하지 않도록 가린다."""
    return re.sub(r"//[^/@]+@", "//***:***@", url)


def _fixed_arca_url(base_url: str, href: str) -> str | None:
    candidate = urlsplit(urljoin(base_url, href))
    try:
        has_custom_port = candidate.port is not None
    except ValueError:
        return None
    if (
        candidate.scheme != "https"
        or candidate.hostname != "arca.live"
        or candidate.username
        or candidate.password
        or has_custom_port
    ):
        return None
    return urlunsplit((candidate.scheme, candidate.netloc, candidate.path, candidate.query, ""))


def _is_allowed_image_url(url: str) -> bool:
    candidate = urlsplit(url)
    hostname = (candidate.hostname or "").lower()
    try:
        has_custom_port = candidate.port is not None
    except ValueError:
        return False
    return (
        candidate.scheme == "https"
        and not candidate.username
        and not candidate.password
        and not has_custom_port
        and (hostname == "arca.live" or hostname.endswith(".namu.la"))
    )


def _original_image_variant(url: str) -> str:
    """Translate Arcalive's thumbnail CDN URL into its original-file variant."""
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    netloc = parts.netloc
    if re.fullmatch(r"ac-p\d*\.namu\.la", hostname):
        netloc = "ac-o.namu.la"
    query = parts.query
    if not re.search(r"(?:^|&)type=orig(?:&|$)", query):
        query = f"type=orig&{query}" if query else "type=orig"
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _extension_variant(url: str, extension: str) -> str:
    parts = urlsplit(url)
    stem, separator, _ = parts.path.rpartition(".")
    if not separator or "/" in parts.path[len(stem) :]:
        return url
    return urlunsplit(
        (parts.scheme, parts.netloc, f"{stem}.{extension}", parts.query, "")
    )


def _ordered_media_urls(img_tag) -> tuple[str, ...]:
    """Build gallery-dl-compatible original and fallback URL variants."""
    sources = (
        img_tag.get("data-originalurl", ""),
        img_tag.get("src", ""),
    )
    original_extension = str(img_tag.get("data-orig", "")).casefold().lstrip(".")
    if original_extension not in _ORIGINAL_EXTENSIONS:
        original_extension = ""

    urls = []
    for source in sources:
        if not source:
            continue
        resolved = urljoin(ARCA_BASE, source)
        original = _original_image_variant(resolved)
        variants = (
            (_extension_variant(original, original_extension), original)
            if original_extension
            else (original,)
        )
        for variant in (*variants, resolved):
            if variant not in urls and _is_allowed_image_url(variant):
                urls.append(variant)
    return tuple(urls)


def _create_session():
    """cloudscraper 세션 생성. app_config.arca_socks_proxy가 설정돼 있으면 SOCKS 경유."""
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True, "mobile": False},
    )
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    proxy = app_config.arca_socks_proxy
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
        logger.info(f"아카라이브 SOCKS 프록시 사용: {_mask_proxy(proxy)}")
    return s


class ArcaliveCrawler:
    """아카라이브 게시글 크롤러."""

    def __init__(self, base_url, session=None, *, retry_policy: RetryPolicy | None = None):
        self.base_url = base_url
        self.sent_items = LRUCache()
        self.session = session or _create_session()
        self.retry_policy = retry_policy or _PAGE_RETRY_POLICY

    # ---------- 포스트 목록 파싱 ----------

    def get_latest_posts(self, max_posts=5):
        try:
            res = request_with_policy(
                lambda: self.session.get(self.base_url, timeout=15),
                self.retry_policy,
            )
        except BlockedByChallenge:
            logger.warning("아카라이브 목록 Cloudflare challenge 감지")
            return []
        except requests.RequestException as e:
            logger.warning("아카라이브 목록 요청 실패: %s", type(e).__name__)
            return []
        except Exception as e:
            logger.warning(f"아카라이브 목록 요청 실패: {e}")
            return []

        soup = BeautifulSoup(res.text, "lxml", parse_only=_VROW_STRAINER)
        posts = []

        for vrow in soup.select("div.vrow.hybrid"):
            post = self._parse_hybrid_row(vrow)
            if post:
                posts.append(post)

        if not posts:
            for vrow in soup.select("a.vrow.column"):
                post = self._parse_column_row(vrow)
                if post:
                    posts.append(post)

        posts = posts[POST_SKIP_COUNT:]
        new_posts = []
        for post in posts:
            if post["post_id"] not in self.sent_items:
                new_posts.append(post)

        return new_posts[:max_posts]

    def mark_sent(self, post_id: str) -> None:
        """Acknowledge a post only after delivery succeeds."""
        self.sent_items.add(post_id)

    def _parse_hybrid_row(self, vrow):
        if self._is_notice_row(vrow):
            return None
        title_el = vrow.select_one("a.title.hybrid-title")
        if not title_el:
            return None
        href = title_el.get("href", "")
        if not href:
            return None
        if vrow.select_one(".media-icon") is None:
            return None
        return self._build_post(title_el.get_text(strip=True), href)

    def _parse_column_row(self, vrow):
        if self._is_notice_row(vrow):
            return None
        href = vrow.get("href", "")
        if not href:
            return None
        title_el = vrow.select_one("span.title")
        if not title_el:
            return None
        if vrow.select_one(".media-icon") is None:
            return None
        return self._build_post(title_el.get_text(strip=True), href)

    @staticmethod
    def _is_notice_row(vrow) -> bool:
        classes = set(vrow.get("class", []))
        return "notice" in classes or any(value.startswith("notice-") for value in classes)

    @classmethod
    def _build_post(cls, title: str, href: str) -> dict | None:
        link = _fixed_arca_url(ARCA_BASE, href)
        post_id = cls._extract_post_id(link or "")
        if link is None or not post_id:
            return None
        return {"link": link, "title": title, "post_id": post_id}

    @staticmethod
    def _extract_post_id(href: str) -> str:
        m = re.search(r"/b/[^/]+/(\d+)", href)
        return m.group(1) if m else ""

    # ---------- 개별 게시글 이미지 추출 ----------

    def extract_all_images(self, post_url: str) -> list[MediaCandidate]:
        try:
            res = request_with_policy(
                lambda: self.session.get(post_url, timeout=15),
                self.retry_policy,
            )
        except BlockedByChallenge:
            logger.warning("아카라이브 게시글 Cloudflare challenge 감지: %s", post_url)
            return []
        except requests.RequestException as e:
            logger.warning(
                "아카라이브 게시글 요청 실패 (%s): %s",
                post_url,
                type(e).__name__,
            )
            return []
        except Exception as e:
            logger.warning(f"아카라이브 게시글 요청 실패 ({post_url}): {e}")
            return []

        soup = BeautifulSoup(res.text, "lxml")
        images = []
        seen_urls = set()

        body = soup.select_one("div.article-body")
        if body:
            for img in body.find_all("img"):
                self._collect_image(img, images, seen_urls, post_url)

        content = soup.select_one(".fr-view.article-content")
        if content and content.parent != body:
            for img in content.find_all("img"):
                self._collect_image(img, images, seen_urls, post_url)

        logger.info(f"아카라이브 게시글 이미지 {len(images)}개 발견: {post_url}")
        return images

    def _collect_image(
        self,
        img_tag,
        images: list[MediaCandidate],
        seen_urls: set[str],
        post_url: str = "",
    ) -> None:
        classes = img_tag.get("class", [])
        if "arca-emoticon" in classes or img_tag.get("data-type") == "emoticon":
            return

        urls = _ordered_media_urls(img_tag)
        if not urls:
            return

        primary_url, *fallback_urls = urls
        parts = urlsplit(primary_url)
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if clean_url in seen_urls:
            return
        seen_urls.add(clean_url)

        filename = os.path.basename(unquote(parts.path))
        if not filename:
            pid = self._extract_post_id(post_url) if post_url else ""
            filename = f"arca_{pid}_{len(images)}.jpg"

        post_id = self._extract_post_id(post_url) if post_url else None
        images.append(
            MediaCandidate(
                primary_url=primary_url,
                fallback_urls=tuple(fallback_urls),
                filename_hint=filename,
                headers={"Referer": post_url} if post_url else None,
                source_post_id=post_id,
                expected_media_type="image",
            )
        )
