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
            logger.warning(f"[아카라이브] 채널 없음: {channel_id}")
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

    async def send_single_to_channels(
        self,
        image_item,
        *,
        title=None,
        link=None,
        global_index=0,
    ) -> DeliveryResult:
        """단일 이미지를 모든 Discord 채널로 팬아웃한다."""
        discord_buffer = image_item["discord_buffer"]
        filename = image_item["filename"]
        requested_media = self._media_ids((image_item,))

        result = DeliveryResult(())
        for channel_id in self.channel_ids:
            channel = self._get_channel(channel_id)
            if not channel:
                result = result.merge(DeliveryResult((
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

            sent = await self.message_sender.send_to_discord(
                channel,
                title or "",
                discord_buffer,
                filename,
                link,
                validated=image_item.get("validated", False),
                footer=f"{self.source_label} · 1개 이미지",
            )
            result = result.merge(DeliveryResult((
                ChannelDelivery(
                    transport="discord",
                    destination_id=str(channel_id),
                    outcome=DeliveryOutcome.SUCCEEDED if sent else DeliveryOutcome.FAILED,
                    requested_media=requested_media,
                    delivered_media=requested_media if sent else (),
                    ack_eligible=True,
                    reason=None if sent else "send_failed",
                ),
            )))
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

            discord_result = await self.send_single_to_channels(
                image_item,
                title=title if global_index == 0 else "",
                link=link if global_index == 0 else None,
                global_index=global_index,
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

    async def send_batch_to_channel(
        self,
        channel,
        batch,
        *,
        title,
        link,
        batch_index,
        destination_id: str | None = None,
    ) -> DeliveryResult:
        """한 채널에 배치 이미지를 단일 Discord 메시지로 전송한다."""
        requested_media = self._media_ids(batch)
        resolved_destination_id = destination_id
        if resolved_destination_id is None:
            channel_id = getattr(channel, "id", "") if channel is not None else ""
            resolved_destination_id = str(channel_id or "")

        if channel is None:
            return DeliveryResult((
                ChannelDelivery(
                    transport="discord",
                    destination_id=resolved_destination_id,
                    outcome=DeliveryOutcome.FAILED,
                    requested_media=requested_media,
                    delivered_media=(),
                    ack_eligible=True,
                    reason="channel_not_found",
                ),
            ))

        for item in batch:
            item["discord_buffer"].seek(0)

        files = []
        embeds = []

        for i, item in enumerate(batch):
            buffer = item["discord_buffer"]
            filename = item["filename"]
            global_idx = batch_index + i

            files.append(discord.File(buffer, filename=filename))

            if global_idx == 0:
                embed = make_image_embed(
                    filename,
                    title=title,
                    url=link,
                    color=self.discord_embed_color,
                    footer=f"{self.source_label} · {len(batch)}개 이미지",
                )
            else:
                embed = make_image_embed(filename, color=self.discord_embed_color)
            embeds.append(embed)

        try:
            await channel.send(files=files, embeds=embeds)
            logger.info(
                f"[아카라이브] 배치 전송 완료: {title} "
                f"({batch_index + 1}~{batch_index + len(batch)}/{len(batch)})"
            )
            return DeliveryResult((
                ChannelDelivery(
                    transport="discord",
                    destination_id=resolved_destination_id,
                    outcome=DeliveryOutcome.SUCCEEDED,
                    requested_media=requested_media,
                    delivered_media=requested_media,
                    ack_eligible=True,
                ),
            ))
        except discord.HTTPException as e:
            logger.error(f"[아카라이브] Discord 전송 실패: {e.status} {e.text}")
            if e.status == 413 and hasattr(self.client, "_send_fallback"):
                return await self.client._send_fallback(
                    channel, batch, title, link, batch_index
                )
            return DeliveryResult((
                ChannelDelivery(
                    transport="discord",
                    destination_id=resolved_destination_id,
                    outcome=DeliveryOutcome.FAILED,
                    requested_media=requested_media,
                    delivered_media=(),
                    ack_eligible=True,
                    reason="send_failed",
                ),
            ))
