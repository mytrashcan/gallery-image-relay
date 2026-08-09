from __future__ import annotations

import asyncio
import logging

import discord
from PIL import Image
from telegram import Bot
from telegram.request import HTTPXRequest

from Module.embeds import make_image_embed

logger = logging.getLogger(__name__)

DISCORD_EMBED_COLOR = 0xFF5733


class MessageSender:
    def __init__(self, telegram_bot_token: object, telegram_chat_id: object, image_handler: object=None) -> None:
        # 타임아웃 설정 증가 (기본 5초 -> 30초)
        request = HTTPXRequest(
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0
        )
        self.telegram_bot = Bot(token=telegram_bot_token, request=request) if telegram_bot_token else None
        self.telegram_chat_id = telegram_chat_id
        # 413(파일 크기 초과) 시 재압축 폴백에 사용 (없으면 폴백 비활성화)
        self.image_handler = image_handler

    def validate_image_buffer(self, image_buffer: object) -> object:
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
                logger.error(f"이미지 버퍼 손상됨: {e}")
                return False

        except (OSError, ValueError) as e:
            logger.error(f"이미지 검증 실패: {e}")
            return False

    def recompress_for_discord(self, channel: object, image_buffer: object, filename: object) -> object:
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

    async def send_to_discord(self, channel: object, title: object, image_buffer: object, filename: object, url: object=None, *, validated: bool=False, footer: object=None) -> object:
        """디스코드로 이미지 전송 (413 시 재압축 후 1회 재시도)

        url이 주어지면 임베드 제목이 해당 게시글로 가는 하이퍼링크가 된다.
        footer가 주어지면 임베드 하단에 표시된다.
        """
        try:
            if not validated and not await asyncio.to_thread(self.validate_image_buffer, image_buffer):
                logger.error("Discord 전송 취소: 이미지 검증 실패")
                return False

            embed = make_image_embed(filename, title=title, url=url, color=DISCORD_EMBED_COLOR, footer=footer)

            try:
                await channel.send(
                    file=discord.File(image_buffer, filename=filename),
                    embed=embed
                )
            except discord.HTTPException as e:
                if e.status != 413 or self.image_handler is None:
                    raise
                logger.warning(f"Discord 413 (파일 크기 초과): {filename} — 재압축 후 재시도")
                recompressed = await asyncio.to_thread(
                    self.recompress_for_discord, channel, image_buffer, filename
                )
                if recompressed is None:
                    logger.error(f"Discord 재압축 실패: {filename}")
                    return False
                await channel.send(
                    file=discord.File(recompressed, filename=filename),
                    embed=embed
                )

            logger.info(f"Discord 전송 성공: {filename}")
            return True

        except discord.HTTPException as e:
            logger.error(f"Discord HTTP 에러: {e.status} - {e.text}")
            return False
        except Exception as e:
            logger.error(f"Discord 전송 실패: {type(e).__name__}: {str(e)}")
            return False
        finally:
            # 호출부(dcbot)가 같은 버퍼로 여러 채널에 반복 전송하므로,
            # 성공/실패와 무관하게 항상 읽기 위치를 리셋해 다음 전송에 대비한다.
            try:
                image_buffer.seek(0)
            except (OSError, ValueError):
                pass

    async def send_to_telegram(self, image_buffer: object, filename: object=None, is_gif: object=False, max_retries: object=3, *, validated: bool=False) -> object:
        """텔레그램으로 이미지 전송 (GIF는 animation으로, 재시도 포함)"""
        if self.telegram_bot is None:
            logger.debug("Telegram 봇이 설정되지 않음 — 전송 건너뜀")
            return False

        if not validated and not await asyncio.to_thread(self.validate_image_buffer, image_buffer):
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

                logger.info(f"Telegram 전송 성공: {filename}")
                return True

            except Exception as e:
                error_name = type(e).__name__
                if "TimedOut" in error_name or "Timed out" in str(e):
                    logger.warning(f"Telegram 타임아웃 (시도 {attempt + 1}/{max_retries}): {filename}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 * (attempt + 1))  # 점진적 대기 (2초, 4초, 6초)
                        continue

                logger.error(f"Telegram 전송 실패: {error_name}: {str(e)}")
                return False

        return False
