"""
아카라이브 전용 Discord 봇.

Gallery Image Relay의 dcbot.py와 차이점:
- 게시글 내 모든 이미지를 추출하여 전송 (DCInside: 최상단 1개)
- Telegram 전송 없음 (순수 Discord 전용)
- 멀티 임베드 메시지 (한 게시글 여러 이미지를 하나의 메시지로)
- 모든 이미지는 인메모리(BytesIO)로 처리되고 웹 갤러리도 RAM에만 보관됨
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, replace
from queue import LifoQueue

import discord
import requests

from Module.arca_crawler import ArcaliveCrawler, ArcaPost, _is_allowed_image_url
from Module.config import app_config
from Module.delivery_archive import DeliveryArchive
from Module.delivery_result import DeliveryResult
from Module.image_handler import ImageHandler
from Module.lifecycle import run_blocking
from Module.media_candidate import MediaCandidate
from Module.media_download import (
    MediaDownloadRejected,
    VerifiedMedia,
    download_media_candidate,
)
from Module.media_pipeline import MediaPipeline, PreparedMedia
from Module.message_sender import MessageSender

logger = logging.getLogger(__name__)

# 아카라이브 임베드 색상 (블루 계열)
ARCA_EMBED_COLOR = 0x00A3FF
# Discord 메시지당 최대 임베드/파일 수
MAX_EMBEDS_PER_MSG = 10
# 게시글당 최대 이미지 수 (초과분은 무시)
MAX_IMAGES_PER_POST = 4
# 이미지 간 전송 딜레이(초)
INTER_IMAGE_DELAY = 1.0
# 이미지 다운로드 간격(초) — CDN rate limit 방지
IMAGE_DOWNLOAD_DELAY = 0.5


def _make_download_client() -> object:
    """Arca 이미지 다운로드용 requests 클라이언트를 돌려준다.

    페이지 크롤링(_create_session)과 동일하게 SOCKS 프록시를 적용한다.
    서버의 직접 IP는 아카 Cloudflare에서 403으로 차단되지만, 프록시
    egress(가정망)로는 ac.arca.live(썸네일)가 200으로 서빙된다
    (2026-08-20 실증: plain requests + ARCA_SOCKS_PROXY → 200 image/jpeg).

    각 동시 다운로드 슬롯이 별도의 재사용 Session을 소유한다.
    """
    proxy = getattr(app_config, "arca_socks_proxy", "")
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


@dataclass(frozen=True, slots=True)
class MediaPreparation:
    item: PreparedMedia | None
    resolved: bool


class ArcaBot(discord.Client):
    """아카라이브 게시글을 크롤링하여 디스코드로 전송하는 봇.

    Telegram 전송 없이 Discord embed만 사용.
    게시글 내 모든 이미지를 추출하여 전송한다.
    """

    def __init__(
        self,
        token: str,
        base_url: str,
        channel_ids: list[str],
        intents: discord.Intents,
        gallery_name: str = "",
    ) -> None:
        super().__init__(intents=intents)
        self.token = token
        self.base_url = base_url
        self.channel_ids = channel_ids
        self.web_gallery_name = str(gallery_name or "")
        self.web_gallery_enabled = app_config.web_gallery
        self.delivery_archive = (
            DeliveryArchive(app_config.archive_path)
            if self.web_gallery_name
            else None
        )
        self.crawler = ArcaliveCrawler(
            base_url,
            gallery_name=self.web_gallery_name,
            delivery_archive=self.delivery_archive,
        )
        self.image_handler = ImageHandler(
            source="arcalive",
            gallery_name=self.web_gallery_name,
            delivery_archive=self.delivery_archive,
        )
        # Telegram 없이 Discord 전용 MessageSender
        self.message_sender = MessageSender(
            telegram_bot_token=None,
            telegram_chat_id=None,
            image_handler=self.image_handler,
        )
        self.media_pipeline = MediaPipeline(
            self.message_sender,
            self,
            self.channel_ids,
            image_handler=self.image_handler,
            web_gallery_enabled=self.web_gallery_enabled,
            web_gallery_name=self.web_gallery_name,
            discord_embed_color=ARCA_EMBED_COLOR,
            telegram_enabled=False,
            source_label="아카라이브",
            web_publish_requires_discord_success=True,
            delivery_archive=self.delivery_archive,
            source="arcalive",
            gallery_name=self.web_gallery_name,
        )
        self._crawler_task: asyncio.Task | None = None
        self._download_semaphore = asyncio.Semaphore(
            max(1, min(4, app_config.arca_download_concurrency))
        )
        self._download_clients = LifoQueue()
        for _ in range(max(1, min(4, app_config.arca_download_concurrency))):
            self._download_clients.put(_make_download_client())

    async def on_ready(self) -> None:
        logger.info(f"[아카라이브] Logged in as {self.user}")

    async def setup_hook(self) -> None:
        if self._crawler_task is None or self._crawler_task.done():
            self._crawler_task = asyncio.create_task(self._run_crawler())

    async def _run_crawler(self) -> None:
        await self.wait_until_ready()
        await self.start_crawling()

    async def close(self) -> None:
        if self._crawler_task is not None:
            self._crawler_task.cancel()
            await asyncio.gather(self._crawler_task, return_exceptions=True)
        await self.media_pipeline.close()
        await self.message_sender.close()
        self.crawler.session.close()
        self.image_handler.session.close()
        if self.delivery_archive is not None:
            self.delivery_archive.close()
        while not self._download_clients.empty():
            self._download_clients.get_nowait().close()
        await super().close()

    async def start_crawling(self) -> None:
        """주기적으로 새 게시글을 폴링한다."""
        while True:
            try:
                posts = await run_blocking(self.crawler.get_latest_posts)
                for post in posts:
                    logger.info("post selected source=arcalive gallery=%s post_id=%s", self.web_gallery_name, post["post_id"])
                    if await self.process_post(post):
                        self.crawler.mark_sent(post["post_id"])
            except discord.ConnectionClosed:
                logger.warning("[아카라이브] Discord 연결 끊김. 재연결 대기...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"[아카라이브] 크롤링 오류: {type(e).__name__}", exc_info=True)
            # 30~60초 간격 폴링
            delay = random.uniform(30, 60)
            await asyncio.sleep(delay)

    async def process_post(self, post: ArcaPost) -> bool:
        """게시글 내 모든 이미지를 추출하여 디스코드로 전송한다."""
        images = await run_blocking(self.crawler.extract_all_images, post["link"])
        if images is None:
            logger.warning("[아카라이브] 게시글 조회 실패, 다음 폴링에서 재시도: [metadata omitted]")
            return False
        if not images:
            logger.info("[아카라이브] 이미지 없음: [metadata omitted]")
            return True

        # 게시글당 최대 이미지 수 제한
        if len(images) > MAX_IMAGES_PER_POST:
            logger.info(f"[아카라이브] 이미지 {len(images)}개 중 {MAX_IMAGES_PER_POST}개만 처리: [metadata omitted]")
            images = images[:MAX_IMAGES_PER_POST]

        title = post["title"]
        link = post["link"]
        logger.info(f"[아카라이브] [metadata omitted]: {len(images)}개 이미지 추출됨")

        downloaded, all_resolved = await self._download_and_process(images, link)
        if not downloaded:
            logger.info("[아카라이브] 다운로드 성공한 이미지 없음: [metadata omitted]")
            return all_resolved

        delivery_result = DeliveryResult(())
        try:
            for batch_start in range(0, len(downloaded), MAX_EMBEDS_PER_MSG):
                batch = downloaded[batch_start : batch_start + MAX_EMBEDS_PER_MSG]
                batch_result = await self._send_image_batch(batch, title, link, batch_start)
                for item in batch:
                    if batch_result.media_acknowledged(item.content_hash):
                        self.image_handler.mark_hash_sent(item.content_hash)
                delivery_result = delivery_result.merge(batch_result)
            return delivery_result.acknowledged and all_resolved
        finally:
            for item in downloaded:
                self.image_handler.release_hash(item.content_hash)

    async def _download_and_process(
        self, images: list[MediaCandidate], link: str
    ) -> tuple[list[PreparedMedia], bool]:
        """이미지 URL 목록을 다운로드→압축→중복제거하여 전송 가능한 버퍼 목록으로 만든다."""
        tasks = [asyncio.create_task(self._download_and_process_one(img_info, link)) for img_info in images]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, MediaPreparation) and result.item is not None:
                    self.image_handler.release_hash(result.item.content_hash)
            raise
        return self._deduplicate_downloads(results), all(result.resolved for result in results)

    @staticmethod
    def _deduplicate_downloads(results: list[MediaPreparation]) -> list[PreparedMedia]:
        unique_items = []
        seen_hashes = set()
        for result in results:
            item = result.item
            if item is None:
                continue
            content_hash = item.content_hash
            if content_hash in seen_hashes:
                logger.info("[아카라이브] 게시글 내 중복 이미지 스킵")
                continue
            seen_hashes.add(content_hash)
            unique_items.append(item)
        return unique_items

    async def _download_and_process_one(
        self, candidate: MediaCandidate, link: str
    ) -> MediaPreparation:
        async with self._download_semaphore:
            reserved_hash = None
            prepared = False
            try:
                verified = await run_blocking(
                    self._download_single_image,
                    candidate,
                    link,
                )
                if verified is None:
                    return MediaPreparation(None, False)

                # process_image(압축 등 CPU 작업) 전에 해시를 선점한다. 전송 성공
                # 시 mark_hash_sent로 확정되고, 실패 시 release_hash로 롤백되므로
                # 동일 이미지가 여러 게시글에서 동시에 유입돼도 이중 전송되지 않는다.
                if not self.image_handler.reserve_hash(verified.content_hash):
                    logger.info("[아카라이브] 중복 이미지 스킵: [metadata omitted]")
                    return MediaPreparation(None, True)

                reserved_hash = verified.content_hash

                discord_buffer, telegram_buffer, is_gif = await run_blocking(
                    self.image_handler.process_image,
                    verified.data,
                    verified.filename,
                )

                prepared = True
                return MediaPreparation(
                    PreparedMedia(
                        discord_buffer=discord_buffer,
                        telegram_buffer=telegram_buffer,
                        filename=verified.filename,
                        content_hash=verified.content_hash,
                        is_gif=is_gif,
                        original_data=verified.data,
                        validated=True,
                    ),
                    True,
                )
            except MediaDownloadRejected as e:
                logger.warning("Arca media rejected: %s", type(e).__name__)
                return MediaPreparation(None, True)
            except ValueError as e:
                logger.warning(f"[아카라이브] 이미지 처리 실패 ([metadata omitted]): {type(e).__name__}")
                return MediaPreparation(None, True)
            except OSError as e:
                logger.warning(f"[아카라이브] 이미지 처리 재시도 필요 ([metadata omitted]): {type(e).__name__}")
                return MediaPreparation(None, False)

            # Hold the semaphore slot briefly after every CDN attempt, including failures.
            finally:
                if reserved_hash is not None and not prepared:
                    self.image_handler.release_hash(reserved_hash)
                try:
                    await asyncio.sleep(IMAGE_DOWNLOAD_DELAY)
                except asyncio.CancelledError:
                    if reserved_hash is not None:
                        self.image_handler.release_hash(reserved_hash)
                    raise

    def _download_single_image(
        self,
        candidate: MediaCandidate,
        referer: str = "",
    ) -> VerifiedMedia | None:
        """Download and validate one ordered Arcalive media fallback chain.

        namu.la CDN은 Cloudflare 보호가 없으므로 일반 requests 사용.
        """
        if candidate.headers is None and referer:
            candidate = replace(candidate, headers={"Referer": referer})
        client = self._download_clients.get()
        try:
            return download_media_candidate(
                client,
                candidate,
                is_allowed_url=_is_allowed_image_url,
                validate=self.image_handler.validate_image_data,
                timeout=15,
                max_bytes=app_config.media_download_max_mb * 1024 * 1024,
            )
        except requests.RequestException as e:
            logger.warning("이미지 다운로드 실패: %s", type(e).__name__)
            return None
        finally:
            self._download_clients.put(client)

    async def _send_image_batch(
        self,
        batch: list[PreparedMedia],
        title: str,
        link: str,
        batch_index: int,
    ) -> DeliveryResult:
        """한 배치를 공통 Discord fan-out 경로로 전송한다."""
        delivery_result = await self.media_pipeline.send_discord_batch(
            batch,
            title=title,
            link=link,
            start_index=batch_index,
        )

        # 배치 간 딜레이 (rate limit 방지)
        if batch_index > 0:
            await asyncio.sleep(INTER_IMAGE_DELAY)
        return delivery_result

    async def run_bot(self) -> None:
        async with self:
            await self.start(self.token)
