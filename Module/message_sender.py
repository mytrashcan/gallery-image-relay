from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from io import BytesIO

import discord
from PIL import Image
from telegram import Bot
from telegram.request import HTTPXRequest

from Module.delivery_result import ChannelDelivery, DeliveryOutcome
from Module.embeds import make_image_embed
from Module.image_handler import ImageHandler
from Module.lifecycle import run_blocking
from Module.media_pipeline import PreparedMedia

logger = logging.getLogger(__name__)

DISCORD_EMBED_COLOR = 0xFF5733


class MessageSender:
    def __init__(
        self,
        telegram_bot_token: str | None,
        telegram_chat_id: str | None,
        image_handler: ImageHandler | None = None,
    ) -> None:
        # 타임아웃 설정 증가 (기본 5초 -> 30초)
        request = HTTPXRequest(
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0
        ) if telegram_bot_token else None
        self._telegram_request = request
        self.telegram_bot = Bot(token=telegram_bot_token, request=request, get_updates_request=request) if request else None
        self.telegram_chat_id = telegram_chat_id
        # 413(파일 크기 초과) 시 재압축 폴백에 사용 (없으면 폴백 비활성화)
        self.image_handler = image_handler

    async def close(self) -> None:
        if self._telegram_request is not None:
            await self._telegram_request.shutdown()

    def validate_image_buffer(self, image_buffer: BytesIO) -> bool:
        """메모리 버퍼의 이미지 유효성 검증"""
        try:
            image_buffer.seek(0, 2)
            file_size = image_buffer.tell()
            image_buffer.seek(0)

            if file_size == 0:
                logger.error("이미지 버퍼 크기가 0바이트")
                return False

            try:
                # verify()는 전체 디코딩 없이 무결성만 확인 (load()보다 훨씬 저렴)
                with Image.open(image_buffer) as img:
                    img.verify()

                image_buffer.seek(0)

                logger.debug(f"이미지 검증 성공 ({file_size} bytes)")
                return True

            except (OSError, ValueError, SyntaxError) as e:
                logger.error(f"이미지 버퍼 손상됨: {type(e).__name__}")
                return False

        except (OSError, ValueError) as e:
            logger.error(f"이미지 검증 실패: {type(e).__name__}")
            return False

    def recompress_for_discord(
        self,
        channel: discord.abc.Messageable,
        image_buffer: BytesIO,
        filename: str,
    ) -> BytesIO | None:
        """Re-compress image after 413 response using guild's actual limit."""
        image_buffer.seek(0)
        data = image_buffer.read()
        current_size = len(data)

        # 서버 부스트 레벨 기준 제한을 알 수 있으면 사용, 아니면 절반 크기로 시도
        # (discord.py 2.4.0은 무부스트 서버 filesize_limit을 25MB로 잘못 반환하므로 그대로 믿지 않음)
        guild = getattr(channel, "guild", None)
        limit = getattr(guild, "filesize_limit", None)
        if limit and limit < current_size:
            target = limit
        else:
            target = current_size // 2

        is_gif = data[:6] in (b"GIF87a", b"GIF89a")
        if is_gif:
            buffer, size = self.image_handler.compress_gif(data, target, filename)
        else:
            buffer, size = self.image_handler.compress_image(data, target, filename)

        if size >= current_size:
            return None
        buffer.seek(0)
        return buffer

    @staticmethod
    def _discord_delivery(
        destination_id: str,
        requested_media: tuple[str, ...],
        delivered_media: tuple[str, ...],
    ) -> ChannelDelivery:
        if len(delivered_media) == len(requested_media) and requested_media:
            outcome = DeliveryOutcome.SUCCEEDED
        elif delivered_media:
            outcome = DeliveryOutcome.PARTIAL
        else:
            outcome = DeliveryOutcome.FAILED
        return ChannelDelivery(
            transport="discord",
            destination_id=destination_id,
            outcome=outcome,
            requested_media=requested_media,
            delivered_media=delivered_media,
            ack_eligible=True,
            reason=None if outcome is DeliveryOutcome.SUCCEEDED else "send_failed",
        )

    async def _send_discord_file(
        self,
        channel: discord.abc.Messageable,
        image_buffer: BytesIO,
        filename: str,
        embed: discord.Embed,
        *,
        validated: bool = False,
    ) -> bool:
        try:
            if not validated and not await run_blocking(self.validate_image_buffer, image_buffer):
                logger.error("Discord 전송 취소: 이미지 검증 실패")
                return False

            try:
                await channel.send(
                    file=discord.File(image_buffer, filename=filename),
                    embed=embed,
                )
            except discord.HTTPException as exc:
                if exc.status != 413:
                    raise
                return await self._send_recompressed_discord_file(
                    channel,
                    image_buffer,
                    filename,
                    embed,
                )

            logger.info("Discord 전송 성공: [metadata omitted]")
            return True
        except discord.HTTPException as exc:
            logger.error(f"Discord HTTP 에러: {exc.status} - {type(exc).__name__}")
            return False
        except Exception as exc:
            logger.error(f"Discord 전송 실패: {type(exc).__name__}: {type(exc).__name__}")
            return False
        finally:
            try:
                image_buffer.seek(0)
            except (OSError, ValueError):
                pass

    async def _send_recompressed_discord_file(
        self,
        channel: discord.abc.Messageable,
        image_buffer: BytesIO,
        filename: str,
        embed: discord.Embed,
    ) -> bool:
        if self.image_handler is None:
            logger.error("Discord 재압축 불가: image handler 없음 ([metadata omitted])")
            return False

        try:
            logger.warning("Discord 413 (파일 크기 초과): [metadata omitted] — 재압축 후 재시도")
            recompressed = await run_blocking(
                self.recompress_for_discord, channel, image_buffer, filename
            )
            if recompressed is None:
                logger.error("Discord 재압축 실패: [metadata omitted]")
                return False
            await channel.send(
                file=discord.File(recompressed, filename=filename),
                embed=embed,
            )
            logger.info("Discord 재압축 전송 성공: [metadata omitted]")
            return True
        except discord.HTTPException as exc:
            logger.error(f"Discord 재압축 HTTP 에러: {exc.status} - {type(exc).__name__}")
            return False
        except Exception as exc:
            logger.error(f"Discord 재압축 전송 실패: {type(exc).__name__}: {type(exc).__name__}")
            return False
        finally:
            try:
                image_buffer.seek(0)
            except (OSError, ValueError):
                pass

    async def send_discord_payload(
        self,
        channel: discord.abc.Messageable,
        items: list[PreparedMedia],
        *,
        files: list[discord.File],
        embeds: list[discord.Embed],
        destination_id: str,
        requested_media: tuple[str, ...],
        on_delivered: Callable[[ChannelDelivery], None] | None = None,
    ) -> ChannelDelivery:
        """한 Discord 채널에 payload를 전송하고 413이면 항목별로 fallback한다."""
        if (
            not items
            or len(items) != len(files)
            or len(items) != len(embeds)
            or len(items) != len(requested_media)
        ):
            return self._discord_delivery(destination_id, requested_media, ())

        for item in items:
            image_buffer = item.discord_buffer
            if not item.validated and not await run_blocking(
                self.validate_image_buffer, image_buffer
            ):
                logger.error("Discord 배치 전송 취소: 이미지 검증 실패")
                return self._discord_delivery(destination_id, requested_media, ())
            image_buffer.seek(0)

        try:
            await channel.send(files=files, embeds=embeds)
            return self._discord_delivery(destination_id, requested_media, requested_media)
        except discord.HTTPException as exc:
            if exc.status != 413:
                logger.error(f"Discord HTTP 에러: {exc.status} - {type(exc).__name__}")
                return self._discord_delivery(destination_id, requested_media, ())
            logger.warning("Discord batch 413 — 항목별 전송으로 fallback")
        except Exception as exc:
            logger.error(f"Discord 전송 실패: {type(exc).__name__}: {type(exc).__name__}")
            return self._discord_delivery(destination_id, requested_media, ())
        finally:
            for item in items:
                try:
                    item.discord_buffer.seek(0)
                except (OSError, ValueError):
                    pass

        delivered_media = []
        for item, embed, media_id in zip(items, embeds, requested_media, strict=True):
            if len(items) == 1:
                sent = await self._send_recompressed_discord_file(
                    channel,
                    item.discord_buffer,
                    item.filename,
                    embed,
                )
            else:
                sent = await self._send_discord_file(
                    channel,
                    item.discord_buffer,
                    item.filename,
                    embed,
                    validated=True,
                )
            if sent:
                delivered_media.append(media_id)
                if on_delivered is not None:
                    # Persist before the next await: later fallback items can be
                    # cancelled or fail without forgetting this confirmed send.
                    on_delivered(self._discord_delivery(destination_id, (media_id,), (media_id,)))
            if len(items) > 1:
                await asyncio.sleep(0.5)

        return self._discord_delivery(
            destination_id,
            requested_media,
            tuple(delivered_media),
        )

    async def send_to_discord(
        self,
        channel: discord.abc.Messageable,
        title: str | None,
        image_buffer: BytesIO,
        filename: str,
        url: str | None = None,
        *,
        validated: bool = False,
        footer: str | None = None,
    ) -> bool:
        """디스코드로 이미지 전송 (413 시 재압축 후 1회 재시도)

        url이 주어지면 임베드 제목이 해당 게시글로 가는 하이퍼링크가 된다.
        footer가 주어지면 임베드 하단에 표시된다.
        """
        embed = make_image_embed(filename, title=title, url=url, color=DISCORD_EMBED_COLOR, footer=footer)
        return await self._send_discord_file(
            channel,
            image_buffer,
            filename,
            embed,
            validated=validated,
        )

    async def send_to_telegram(
        self,
        image_buffer: BytesIO,
        filename: str | None = None,
        is_gif: bool = False,
        max_retries: int = 3,
        *,
        validated: bool = False,
    ) -> bool:
        """텔레그램으로 이미지 전송 (GIF는 animation으로, 재시도 포함)"""
        if self.telegram_bot is None:
            logger.debug("Telegram 봇이 설정되지 않음 — 전송 건너뜀")
            return False

        if not validated and not await run_blocking(self.validate_image_buffer, image_buffer):
            logger.error("Telegram 전송 취소: 이미지 검증 실패")
            return False

        for attempt in range(max_retries):
            try:
                image_buffer.seek(0)  # 재시도 시 버퍼 위치 리셋

                if is_gif:
                    await self.telegram_bot.send_animation(
                        chat_id=self.telegram_chat_id,
                        animation=image_buffer,
                        filename=filename
                    )
                else:
                    await self.telegram_bot.send_photo(
                        chat_id=self.telegram_chat_id,
                        photo=image_buffer,
                        filename=filename
                    )

                logger.info("Telegram 전송 성공: [metadata omitted]")
                return True

            except Exception as e:
                error_name = type(e).__name__
                if "TimedOut" in error_name or "Timed out" in str(e):
                    logger.warning(f"Telegram 타임아웃 (시도 {attempt + 1}/{max_retries}): [metadata omitted]")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 * (attempt + 1))  # 점진적 대기 (2초, 4초, 6초)
                        continue

                logger.error(f"Telegram 전송 실패: {error_name}: {type(e).__name__}")
                return False

        return False
