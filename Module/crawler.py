from __future__ import annotations

import logging
from typing import TypedDict
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, SoupStrainer
from bs4.element import Tag

from Module.config import BS_PARSER, HEADERS, REQUEST_TIMEOUT
from Module.delivery_archive import DeliveryArchive, post_key

# BoundedSet 은 공통 LRUCache 로 통합됨 — 기존 import(arca_crawler/테스트) 호환 위해 재노출
from Module.lru_cache import BoundedSet, LRUCache  # noqa: F401
from Module.retry_policy import (
    BlockedByChallenge,
    RetryPolicy,
    request_with_policy,
)

logger = logging.getLogger(__name__)

MAX_CACHE_SIZE = 500

# 최신 일반 게시물은 갤러리 관리자/운영자가 유해 게시물을 먼저 차단할 수 있도록 보류한다.
POST_SAFETY_SKIP_COUNT = 20

# tr 요소만 파싱하여 파싱 비용 절감
# (SoupStrainer의 class_ 매칭은 다중 클래스 속성에서 동작하지 않으므로 태그로만 거름)
_POST_ROW_STRAINER = SoupStrainer("tr")

_PAGE_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    backoff="exponential",
    base_delay=1.0,
    max_delay=4.0,
)


class DCPost(TypedDict):
    link: str
    title: str
    post_id: str
    has_image: bool


class DCInsideCrawler:
    def __init__(
        self,
        base_url: str,
        *,
        retry_policy: RetryPolicy | None = None,
        gallery_name: str = "",
        delivery_archive: DeliveryArchive | None = None,
    ) -> None:
        self.base_url = base_url
        self.sent_post_ids = LRUCache(MAX_CACHE_SIZE)
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.retry_policy = retry_policy or _PAGE_RETRY_POLICY
        self.gallery_name = gallery_name
        self.delivery_archive = delivery_archive
        if delivery_archive is not None and not gallery_name:
            raise ValueError("gallery_name is required when using a delivery archive")

    def image_check(self, element: Tag) -> bool:
        """이미지 포함 여부 체크"""
        return element.select_one(".icon_pic") is not None

    def get_latest_post(self) -> DCPost | None:
        """최신 게시글 정보 가져오기 (동기)"""
        try:
            res = request_with_policy(
                lambda: self.session.get(self.base_url, timeout=REQUEST_TIMEOUT),
                self.retry_policy,
            )
            soup = BeautifulSoup(res.text, BS_PARSER, parse_only=_POST_ROW_STRAINER)

            posts = soup.select("tr.ub-content")
            if not posts:
                return None

            normal_post_count = 0
            for post in posts:
                try:
                    if post.get("data-type") == "icon_notice":
                        continue

                    title_element = post.select_one('td.gall_tit a[href*="/board/view/"]')
                    if not title_element:
                        continue

                    link = urljoin("https://gall.dcinside.com", title_element.get("href", ""))
                    parts = urlsplit(link)
                    post_ids = parse_qs(parts.query).get("no", [])
                    if parts.hostname != "gall.dcinside.com" or not post_ids:
                        continue

                    normal_post_count += 1
                    if normal_post_count <= POST_SAFETY_SKIP_COUNT:
                        continue

                    post_id = post_ids[0]
                    title = title_element.text.strip()
                    image_insert = self.image_check(post)

                    logger.debug(f"{title} {link} {image_insert}")

                    if not self._has_sent(post_id):
                        return {
                            'link': link,
                            'title': title,
                            'post_id': post_id,
                            'has_image': image_insert
                        }

                except Exception as e:
                    logger.warning(f"게시글 파싱 실패: {e}")
                    continue

            return None

        except BlockedByChallenge:
            logger.warning("DCInside Cloudflare challenge 감지: %s", self.base_url)
            return None
        except requests.Timeout:
            logger.warning(f"크롤링 타임아웃: {self.base_url}")
            return None
        except requests.RequestException as e:
            logger.error(f"크롤링 요청 실패: {e}")
            return None

    def mark_sent(self, post_id: str) -> None:
        """Acknowledge a post only after delivery succeeds."""
        if self.delivery_archive is not None:
            self.delivery_archive.add(
                "dcinside",
                self.gallery_name,
                post_key(post_id),
            )
        self.sent_post_ids.add(post_id)

    def _has_sent(self, post_id: str) -> bool:
        if post_id in self.sent_post_ids:
            return True
        if self.delivery_archive is None:
            return False
        if not self.delivery_archive.check(
            "dcinside",
            self.gallery_name,
            post_key(post_id),
        ):
            return False
        self.sent_post_ids.add(post_id)
        return True
