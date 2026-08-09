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
from dataclasses import replace

import discord
import requests

from Module.arca_crawler import ArcaliveCrawler, _is_allowed_image_url
from Module.config import app_config
from Module.delivery_archive import DeliveryArchive
from Module.delivery_result import ChannelDelivery, DeliveryOutcome, DeliveryResult
from Module.embeds import make_image_embed
from Module.image_handler import ImageHandler
from Module.media_candidate import MediaCandidate
from Module.media_download import (
    MediaDownloadRejected,
    VerifiedMedia,
    download_media_candidate,
)
from Module.media_pipeline import MediaPipeline
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


class ArcaBot(discord.Client):
    """아카라이브 게시글을 크롤링하여 디스코드로 전송하는 봇.

    Telegram 전송 없이 Discord embed만 사용.
    게시글 내 모든 이미지를 추출하여 전송한다.
    """

    def __init__(self, token: object, base_url: object, channel_ids: object, intents: object, gallery_name: object="") -> None:
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
        )
        self._crawler_task: asyncio.Task | None = None
        self._download_semaphore = asyncio.Semaphore(
            max(1, min(4, app_config.arca_download_concurrency))
        )

    async def on_ready(self) -> object:
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
        if self.delivery_archive is not None:
            self.delivery_archive.close()
        await super().close()

    async def start_crawling(self) -> object:
        """주기적으로 새 게시글을 폴링한다."""
        while True:
            try:
                posts = await asyncio.to_thread(self.crawler.get_latest_posts)
                for post in posts:
                    logger.info(f"[아카라이브] 새 게시글: {post['title']} ({post['link']})")
                    if await self.process_post(post):
                        self.crawler.mark_sent(post["post_id"])
            except discord.ConnectionClosed:
                logger.warning("[아카라이브] Discord 연결 끊김. 재연결 대기...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"[아카라이브] 크롤링 오류: {e}", exc_info=True)
            # 30~60초 간격 폴링
            delay = random.uniform(30, 60)
            await asyncio.sleep(delay)

    async def process_post(self, post: object) -> object:
        """게시글 내 모든 이미지를 추출하여 디스코드로 전송한다."""
        images = await asyncio.to_thread(self.crawler.extract_all_images, post["link"])
        if images is None:
            logger.warning(f"[아카라이브] 게시글 조회 실패, 다음 폴링에서 재시도: {post['title']}")
            return False
        if not images:
            logger.info(f"[아카라이브] 이미지 없음: {post['title']}")
            return True

        # 게시글당 최대 이미지 수 제한
        if len(images) > MAX_IMAGES_PER_POST:
            logger.info(f"[아카라이브] 이미지 {len(images)}개 중 {MAX_IMAGES_PER_POST}개만 처리: {post['title']}")
            images = images[:MAX_IMAGES_PER_POST]

        title = post["title"]
        link = post["link"]
        logger.info(f"[아카라이브] {title}: {len(images)}개 이미지 추출됨")

        downloaded, all_resolved = await self._download_and_process(images, link)
        if not downloaded:
            logger.info(f"[아카라이브] 다운로드 성공한 이미지 없음: {title}")
            return all_resolved

        # 배치 처리: MAX_EMBEDS_PER_MSG개씩 나눠서 전송
        delivery_result = DeliveryResult(())
        for batch_start in range(0, len(downloaded), MAX_EMBEDS_PER_MSG):
            batch = downloaded[batch_start : batch_start + MAX_EMBEDS_PER_MSG]
            batch_result = await self._send_image_batch(batch, title, link, batch_start)
            if batch_result.acknowledged:
                for item in batch:
                    self.image_handler.mark_hash_sent(item["content_hash"])
            delivery_result = delivery_result.merge(batch_result)
        return delivery_result.acknowledged and all_resolved

    async def _download_and_process(
        self, images: list[MediaCandidate], link: str
    ) -> tuple[list[dict[str, object]], bool]:
        """이미지 URL 목록을 다운로드→압축→중복제거하여 전송 가능한 버퍼 목록으로 만든다."""
        results = await asyncio.gather(
            *(self._download_and_process_one(img_info, link) for img_info in images)
        )
        return self._deduplicate_downloads(results), all(
            resolved for _, resolved in results
        )

    @staticmethod
    def _deduplicate_downloads(results) -> list[dict[str, object]]:
        unique_items = []
        seen_hashes = set()
        for item, _ in results:
            if item is None:
                continue
            content_hash = item["content_hash"]
            if content_hash in seen_hashes:
                logger.info("[아카라이브] 게시글 내 중복 이미지 스킵: %s", item["filename"])
                continue
            seen_hashes.add(content_hash)
            unique_items.append(item)
        return unique_items

    async def _download_and_process_one(
        self, candidate: MediaCandidate, link: str
    ) -> tuple[dict[str, object] | None, bool]:
        async with self._download_semaphore:
            filename = candidate.filename_hint or "arca-image"
            try:
                verified = await asyncio.to_thread(
                    self._download_single_image,
                    candidate,
                    link,
                )
                if verified is None:
                    return None, False

                discord_buffer, telegram_buffer, is_gif = await asyncio.to_thread(
                    self.image_handler.process_image,
                    verified.data,
                    verified.filename,
                )

                if self.image_handler.has_seen_hash(verified.content_hash):
                    logger.info(f"[아카라이브] 중복 이미지 스킵: {verified.filename}")
                    return None, True

                return {
                    "discord_buffer": discord_buffer,
                    "telegram_buffer": telegram_buffer,
                    "filename": verified.filename,
                    "is_gif": is_gif,
                    "original_data": verified.data,
                    "content_hash": verified.content_hash,
                    "validated": True,
                }, True
            except MediaDownloadRejected as e:
                logger.warning(
                    f"[아카라이브] 영구적으로 거절된 이미지 ({filename}): {e}"
                )
                return None, True
            except ValueError as e:
                logger.warning(f"[아카라이브] 이미지 처리 실패 ({filename}): {e}")
                return None, True
            except OSError as e:
                logger.warning(f"[아카라이브] 이미지 처리 재시도 필요 ({filename}): {e}")
                return None, False

            # Hold the semaphore slot briefly after every CDN attempt, including failures.
            finally:
                await asyncio.sleep(IMAGE_DOWNLOAD_DELAY)

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
        try:
            return download_media_candidate(
                requests,
                candidate,
                is_allowed_url=_is_allowed_image_url,
                validate=self.image_handler.validate_image_data,
                timeout=15,
                max_bytes=app_config.media_download_max_mb * 1024 * 1024,
            )
        except requests.RequestException as e:
            logger.warning("이미지 다운로드 실패 (%s): %s", candidate.primary_url, e)
            return None

    async def _send_image_batch(
        self,
        batch: list[dict[str, object]],
        title: str,
        link: str,
        batch_index: int,
    ) -> DeliveryResult:
        """한 배치의 이미지를 Discord embed로 전송한다.

        - 첫 번째 embed: title + link 포함
        - 나머지 embed: 이미지만 (제목 없는 깔끔한 갤러리 형태)
        """
        # 웹 갤러리 적재용 스냅샷 — 전송 과정에서 버퍼 위치가 소비되므로 미리 확보
        gallery_snapshot = None
        if self.web_gallery_enabled:
            gallery_snapshot = [
                (self.media_pipeline._web_image_data(item), item["filename"])
                for item in batch
            ]

        requested_media = self.media_pipeline._media_ids(batch)
        delivery_result = DeliveryResult(())
        for channel_id in self.channel_ids:
            channel = self.get_channel(int(channel_id))
            if not channel:
                logger.warning(f"[아카라이브] 채널 없음: {channel_id}")
                delivery_result = delivery_result.merge(DeliveryResult((
                    ChannelDelivery(
                        transport="discord",
                        destination_id=str(channel_id),
                        outcome=DeliveryOutcome.FAILED,
                        requested_media=requested_media,
                        delivered_media=(),
                        ack_eligible=True,
                        reason="channel_not_found",
                    ),
                )))
                continue

            batch_result = await self.media_pipeline.send_batch_to_channel(
                channel,
                batch,
                title=title,
                link=link,
                batch_index=batch_index,
                destination_id=str(channel_id),
            )
            delivery_result = delivery_result.merge(batch_result)

        # 전송 성공한 배치를 공유 웹 갤러리에 적재
        # (fallback 경로는 _send_fallback 내부에서 개별 적재)
        if delivery_result.acknowledged and gallery_snapshot:
            for i, (data, filename) in enumerate(gallery_snapshot):
                web_result = await self.media_pipeline.attach_to_web_gallery(
                    data,
                    filename,
                    batch_index + i,
                    title,
                    link,
                )
                delivery_result = delivery_result.merge(web_result)

        # 배치 간 딜레이 (rate limit 방지)
        if batch_index > 0:
            await asyncio.sleep(INTER_IMAGE_DELAY)
        return delivery_result

    async def _save_to_web_gallery(self, data: bytes, filename: str,
                                   global_idx: int, title: str, link: str) -> DeliveryResult:
        """WEB_GALLERY=1 이면 전송된 이미지를 공유 웹 갤러리에 적재한다.

        첫 번째 이미지에는 원본 제목, 이후 이미지에는 '제목 - N' 형식으로 표시한다.
        """
        return await self.media_pipeline.attach_to_web_gallery(data, filename, global_idx, title, link)

    async def _send_fallback(self, channel: object, batch: list[dict[str, object]], title: str,
                              link: str, batch_index: int) -> DeliveryResult:
        """413(파일 크기 초과) 발생 시 한 장씩 개별 전송 (재압축 포함)."""
        all_sent = True
        requested_media = self.media_pipeline._media_ids(batch)
        delivered_media = []
        web_result = DeliveryResult(())
        for i, item in enumerate(batch):
            global_idx = batch_index + i
            item["discord_buffer"].seek(0)
            buffer = item["discord_buffer"]
            filename = item["filename"]

            try:
                embed_title = title if global_idx == 0 else None
                embed_link = link if global_idx == 0 else None
                embed = make_image_embed(
                    filename, title=embed_title, url=embed_link, color=ARCA_EMBED_COLOR,
                )

                # 전송 전에 스냅샷 확보 (send가 버퍼 위치를 소비함)
                data = buffer.getvalue()
                await channel.send(
                    file=discord.File(buffer, filename=filename),
                    embed=embed,
                )
                self.image_handler.mark_hash_sent(item["content_hash"])
                delivered_media.append(requested_media[i])
                gallery_result = await self._save_to_web_gallery(data, filename, global_idx, title, link)
                web_result = web_result.merge(gallery_result)
            except discord.HTTPException as e2:
                if e2.status == 413:
                    # 재압축 시도
                    logger.warning(f"[아카라이브] 413 재압축 시도: {filename}")
                    recompressed = await asyncio.to_thread(
                        self.message_sender.recompress_for_discord,
                        channel, buffer, filename,
                    )
                    if recompressed:
                        embed = make_image_embed(filename, color=ARCA_EMBED_COLOR)
                        data = recompressed.getvalue()
                        try:
                            await channel.send(
                                file=discord.File(recompressed, filename=filename),
                                embed=embed,
                            )
                            self.image_handler.mark_hash_sent(item["content_hash"])
                            delivered_media.append(requested_media[i])
                            gallery_result = await self._save_to_web_gallery(
                                data, filename, global_idx, title, link
                            )
                            web_result = web_result.merge(gallery_result)
                        except discord.HTTPException as retry_error:
                            all_sent = False
                            logger.error("아카라이브 fallback 재시도 실패: %s", retry_error)
                    else:
                        all_sent = False
                else:
                    logger.error(f"[아카라이브] fallback 전송 실패: {e2}")
                    all_sent = False

            await asyncio.sleep(0.5)

        if all_sent:
            outcome = DeliveryOutcome.SUCCEEDED
        elif delivered_media:
            outcome = DeliveryOutcome.PARTIAL
        else:
            outcome = DeliveryOutcome.FAILED

        channel_id = getattr(channel, "id", "")
        discord_result = DeliveryResult((
            ChannelDelivery(
                transport="discord",
                destination_id=str(channel_id or ""),
                outcome=outcome,
                requested_media=requested_media,
                delivered_media=tuple(delivered_media),
                ack_eligible=True,
                reason=None if all_sent else "send_failed",
            ),
        ))
        return discord_result.merge(web_result)

    async def run_bot(self) -> object:
        async with self:
            await self.start(self.token)
