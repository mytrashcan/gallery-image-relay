import asyncio
import logging

import discord

from Module.delivery_result import ChannelDelivery, DeliveryOutcome, DeliveryResult
from Module.embeds import make_image_embed
from Module.gallery_client import GalleryClient

logger = logging.getLogger(__name__)


class MediaPipeline:
    def __init__(
        self,
        message_sender,
        client,
        channel_ids,
        image_handler=None,
        web_gallery_enabled: bool = False,
        web_gallery_name: str = "",
        discord_embed_color: int = 0xFF5733,
        telegram_enabled: bool = True,
        source_label: str = "아카라이브",
        web_publish_requires_discord_success: bool = False,
    ):
        self.message_sender = message_sender
        self.client = client
        self.channel_ids = channel_ids
        self.image_handler = image_handler
        self.web_gallery_enabled = web_gallery_enabled
        self.web_gallery_name = web_gallery_name
        self.discord_embed_color = discord_embed_color
        self.telegram_enabled = telegram_enabled
        self.source_label = source_label
        self.web_publish_requires_discord_success = web_publish_requires_discord_success
        self.gallery_client = GalleryClient() if web_gallery_enabled else None
        self._web_queue: asyncio.Queue | None = None
        self._web_worker_task: asyncio.Task | None = None

    def _ensure_web_worker(self) -> None:
        if self.gallery_client is None or self._web_worker_task is not None:
            return
        from Module.config import app_config

        self._web_queue = asyncio.Queue(maxsize=max(1, app_config.web_upload_queue_size))
        self._web_worker_task = asyncio.create_task(self._web_worker())

    async def _web_worker(self) -> None:
        assert self._web_queue is not None
        while True:
            args = await self._web_queue.get()
            try:
                await self.gallery_client.publish_async(*args[0], **args[1])
            except Exception as exc:
                logger.warning("웹 갤러리 백그라운드 전송 실패: %s", type(exc).__name__)
            finally:
                self._web_queue.task_done()

    def _get_channel(self, channel_id, *, warn_missing: bool = False):
        channel = self.client.get_channel(int(channel_id))
        if channel is None and warn_missing:
            logger.warning("[%s] 채널 없음: %s", self.source_label, channel_id)
        return channel

    def _gallery_title(self, title: str, global_idx: int) -> str:
        return title if global_idx == 0 else f"{title} - {global_idx + 1}"

    async def attach_to_web_gallery(
        self,
        data,
        filename,
        global_idx,
        title,
        link,
        *,
        media_id: str | None = None,
    ) -> DeliveryResult:
        """WEB_GALLERY=1 이면 이미지를 공유 웹 갤러리에 적재한다."""
        requested_media = (media_id or filename,)
        destination_id = self.web_gallery_name
        if not self.gallery_client:
            return DeliveryResult((
                ChannelDelivery(
                    transport="web_gallery",
                    destination_id=destination_id,
                    outcome=DeliveryOutcome.SKIPPED,
                    requested_media=requested_media,
                    delivered_media=(),
                    ack_eligible=False,
                    reason="gallery_disabled",
                ),
            ))
        if not data:
            return DeliveryResult((
                ChannelDelivery(
                    transport="web_gallery",
                    destination_id=destination_id,
                    outcome=DeliveryOutcome.SKIPPED,
                    requested_media=requested_media,
                    delivered_media=(),
                    ack_eligible=False,
                    reason="empty_media",
                ),
            ))
        self._ensure_web_worker()
        assert self._web_queue is not None
        payload = (
            (data, filename),
            {
                "title": self._gallery_title(title, global_idx),
                "link": link if global_idx == 0 else "",
                "gallery": self.web_gallery_name,
            },
        )
        try:
            self._web_queue.put_nowait(payload)
            return DeliveryResult((
                ChannelDelivery(
                    transport="web_gallery",
                    destination_id=destination_id,
                    outcome=DeliveryOutcome.QUEUED,
                    requested_media=requested_media,
                    delivered_media=(),
                    ack_eligible=False,
                ),
            ))
        except asyncio.QueueFull:
            logger.warning("웹 갤러리 큐가 가득 차 이미지를 건너뜁니다: %s", filename)
            return DeliveryResult((
                ChannelDelivery(
                    transport="web_gallery",
                    destination_id=destination_id,
                    outcome=DeliveryOutcome.FAILED,
                    requested_media=requested_media,
                    delivered_media=(),
                    ack_eligible=False,
                    reason="queue_full",
                ),
            ))

    @staticmethod
    def _web_image_data(image_item) -> bytes:
        from Module.config import app_config

        original = image_item.get("original_data") or b""
        if len(original) <= app_config.web_ingest_max_mb * 1024 * 1024:
            return original or image_item["discord_buffer"].getvalue()
        return image_item["discord_buffer"].getvalue()

    @staticmethod
    def _media_ids(items) -> tuple[str, ...]:
        return tuple(str(item.get("content_hash") or item["filename"]) for item in items)

    def _build_discord_payload(
        self,
        items,
        *,
        title: str,
        link: str | None,
        start_index: int = 0,
    ) -> tuple[list[discord.File], list[discord.Embed]]:
        files = []
        embeds = []
        for item_index, item in enumerate(items):
            image_buffer = item["discord_buffer"]
            image_buffer.seek(0)
            filename = item["filename"]
            global_index = start_index + item_index
            is_first_global_image = global_index == 0

            files.append(discord.File(image_buffer, filename=filename))
            embeds.append(make_image_embed(
                filename,
                title=title if is_first_global_image else None,
                url=link if is_first_global_image else None,
                color=self.discord_embed_color,
                footer=(
                    f"{self.source_label} · {len(items)}개 이미지"
                    if is_first_global_image
                    else None
                ),
            ))
        return files, embeds

    async def _attach_successful_batch_to_web(
        self,
        items,
        *,
        title: str,
        link: str | None,
        start_index: int,
        include_media: set[str] | None = None,
    ) -> DeliveryResult:
        result = DeliveryResult(())
        requested_media = self._media_ids(items)
        for item_index, item in enumerate(items):
            media_id = requested_media[item_index]
            if include_media is not None and media_id not in include_media:
                continue
            web_result = await self.attach_to_web_gallery(
                self._web_image_data(item),
                item["filename"],
                start_index + item_index,
                title,
                link or "",
                media_id=media_id,
            )
            result = result.merge(web_result)
        return result

    async def send_discord_batch(
        self,
        items,
        *,
        title: str,
        link: str | None,
        start_index: int = 0,
    ) -> DeliveryResult:
        """한 이미지 batch를 모든 Discord 채널로 fan-out한다."""
        batch = list(items)
        requested_media = self._media_ids(batch)

        result = DeliveryResult(())
        for channel_id in self.channel_ids:
            destination_id = str(channel_id)
            channel = self._get_channel(channel_id, warn_missing=True)
            if not channel:
                result = result.merge(DeliveryResult((
                    ChannelDelivery(
                        transport="discord",
                        destination_id=destination_id,
                        outcome=DeliveryOutcome.FAILED,
                        requested_media=requested_media,
                        delivered_media=(),
                        ack_eligible=True,
                        reason="channel_not_found",
                    ),
                )))
                continue

            files, embeds = self._build_discord_payload(
                batch,
                title=title,
                link=link,
                start_index=start_index,
            )
            channel_delivery = await self.message_sender.send_discord_payload(
                channel,
                batch,
                files=files,
                embeds=embeds,
                destination_id=destination_id,
                requested_media=requested_media,
            )
            result = result.merge(DeliveryResult((channel_delivery,)))

        if (
            self.web_gallery_enabled
            and self.web_publish_requires_discord_success
            and result.acknowledged
        ):
            result = result.merge(await self._attach_successful_batch_to_web(
                batch,
                title=title,
                link=link,
                start_index=start_index,
            ))
        elif self.web_gallery_enabled and self.web_publish_requires_discord_success:
            delivered_media = {
                media_id
                for delivery in result.deliveries
                if delivery.transport == "discord"
                for media_id in delivery.delivered_media
            }
            if delivered_media:
                result = result.merge(await self._attach_successful_batch_to_web(
                    batch,
                    title=title,
                    link=link,
                    start_index=start_index,
                    include_media=delivered_media,
                ))
        return result

    async def distribute(
        self,
        images,
        *,
        title="",
        link=None,
        gallery_title=None,
        gallery_link=None,
        inter_image_delay=0,
    ) -> DeliveryResult:
        """이미지 목록을 Discord / Telegram / Web Gallery로 분배한다."""
        total = len(images)
        result = DeliveryResult(())
        for global_index, image_item in enumerate(images):
            telegram_buffer = image_item["telegram_buffer"]
            filename = image_item["filename"]
            is_gif = image_item["is_gif"]
            requested_media = self._media_ids((image_item,))

            discord_result = await self.send_discord_batch(
                [image_item],
                title=title,
                link=link,
                start_index=global_index,
            )
            result = result.merge(discord_result)

            if self.telegram_enabled:
                telegram_sent = await self.message_sender.send_to_telegram(
                    telegram_buffer,
                    filename,
                    is_gif,
                    validated=image_item.get("validated", False),
                )
                telegram_chat_id = getattr(self.message_sender, "telegram_chat_id", "")
                result = result.merge(DeliveryResult((
                    ChannelDelivery(
                        transport="telegram",
                        destination_id=str(telegram_chat_id or ""),
                        outcome=DeliveryOutcome.SUCCEEDED if telegram_sent else DeliveryOutcome.FAILED,
                        requested_media=requested_media,
                        delivered_media=requested_media if telegram_sent else (),
                        ack_eligible=True,
                        reason=None if telegram_sent else "send_failed",
                    ),
                )))

            if self.web_gallery_enabled:
                base_title = gallery_title if gallery_title is not None else title
                base_link = gallery_link if gallery_link is not None else link
                web_result = await self.attach_to_web_gallery(
                    self._web_image_data(image_item),
                    filename,
                    global_index,
                    base_title,
                    base_link if base_link is not None else "",
                    media_id=requested_media[0],
                )
                result = result.merge(web_result)

            if inter_image_delay and global_index < total - 1:
                await asyncio.sleep(inter_image_delay)
        return result

    async def close(self) -> None:
        if self._web_queue is not None:
            try:
                await asyncio.wait_for(self._web_queue.join(), timeout=5)
            except TimeoutError:
                logger.warning("웹 갤러리 큐 종료 대기 시간을 초과했습니다.")
        if self._web_worker_task is not None:
            self._web_worker_task.cancel()
            await asyncio.gather(self._web_worker_task, return_exceptions=True)
        if self.gallery_client is not None:
            self.gallery_client.close()
