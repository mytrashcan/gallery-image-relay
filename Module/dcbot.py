from __future__ import annotations

import asyncio
import logging
import random

import discord

from Module.config import app_config
from Module.crawler import DCInsideCrawler
from Module.delivery_archive import DeliveryArchive
from Module.image_handler import ImageHandler
from Module.media_pipeline import MediaPipeline
from Module.message_sender import MessageSender
from Module.process_leader import ProcessLeaderLock

logger = logging.getLogger(__name__)

# 이미지 해시 캐시를 초기화하는 디스코드 채팅 명령어
CLEAR_CACHE_COMMAND = "!쓰담쓰담"


class DCBot(discord.Client):
    def __init__(self, token: object, base_url: object, channel_ids: object, telegram_token: object, telegram_chat_id: object, intents: object, gallery_name: object="") -> None:
        super().__init__(intents=intents)
        self.token = token
        self.base_url = base_url
        self.channel_ids = channel_ids
        self.gallery_name = str(gallery_name or "")
        self.delivery_archive = (
            DeliveryArchive(app_config.archive_path) if self.gallery_name else None
        )
        self.crawler = DCInsideCrawler(
            base_url,
            gallery_name=self.gallery_name,
            delivery_archive=self.delivery_archive,
        )
        self.image_handler = ImageHandler(
            source="dcinside",
            gallery_name=self.gallery_name,
            delivery_archive=self.delivery_archive,
        )
        self.message_sender = MessageSender(telegram_token, telegram_chat_id, image_handler=self.image_handler)
        self.media_pipeline = MediaPipeline(
            self.message_sender,
            self,
            self.channel_ids,
            image_handler=self.image_handler,
            web_gallery_enabled=app_config.web_gallery,
            web_gallery_name=self.gallery_name,
            source_label="디시인사이드",
        )
        self._crawler_task: asyncio.Task | None = None
        self._command_leader = ProcessLeaderLock()

    async def on_ready(self) -> object:
        logger.info(f"Logged in as {self.user}")

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
        self._command_leader.close()
        if self.delivery_archive is not None:
            self.delivery_archive.close()
        await super().close()

    async def start_crawling(self) -> object:
        while True:
            try:
                post = await asyncio.to_thread(self.crawler.get_latest_post)
                if post:
                    if not post['has_image'] or await self.process_post(post):
                        self.crawler.mark_sent(post["post_id"])
            except discord.ConnectionClosed:
                logger.warning("Discord 연결이 끊어졌습니다. 재연결 대기 중...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"크롤링 중 오류: {e}", exc_info=True)
            delay = random.uniform(20, 40)
            await asyncio.sleep(delay)

    async def process_post(self, post: object) -> object:
        # blocking I/O를 별도 스레드에서 실행
        images = await asyncio.to_thread(self.image_handler.download_images, post['link'])
        if images is None:
            return False
        if not images:
            return True

        media_items = [
            {
                "discord_buffer": discord_buffer,
                "telegram_buffer": telegram_buffer,
                "filename": filename,
                "is_gif": is_gif,
                "original_data": original_data,
                "content_hash": content_hash,
                "validated": True,
            }
            for discord_buffer, telegram_buffer, filename, is_gif, original_data, content_hash in images
        ]

        delivery_result = await self.media_pipeline.distribute(
            media_items,
            title=post['title'],
            link=post['link'],
            inter_image_delay=1.0,
        )
        if delivery_result.acknowledged:
            for item in media_items:
                self.image_handler.mark_hash_sent(item["content_hash"])
        return delivery_result.acknowledged

    async def on_message(self, message: object) -> object:
        if message.author == self.user:
            return

        if message.content.strip() == CLEAR_CACHE_COMMAND:
            self.image_handler.clear_seen_hashes()

            if not self._command_leader.try_acquire():
                return

            file = discord.File("gaki.png", filename="gaki.png")
            embed = discord.Embed(
                title="이미지 캐시를 초기화했습니다!",
                description="이제 새로운 이미지들을 받을 준비 완료!",
                color=0xFF69B4
            )
            embed.set_image(url="attachment://gaki.png")
            await message.channel.send(embed=embed, file=file)

    async def run_bot(self) -> object:
        async with self:
            await self.start(self.token)
