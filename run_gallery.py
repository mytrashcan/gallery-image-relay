"""Single gallery runner — routes to DCInside (DCBot) or Arcalive (ArcaBot) based on galleries.json type."""
from __future__ import annotations

import asyncio
import sys

from Module.lifecycle import run_until_signal

# config는 arca_crawler보다 먼저 import해야 함 (arca_crawler가 app_config로 프록시를 읽음)
from Module.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TOKEN, get_discord_intents, validate_required_env  # isort: skip
from Module.arca_bot import ArcaBot
from Module.dcbot import DCBot


def load_gallery_config(gallery_name):
    from Module.config import load_gallery_configs
    galleries = load_gallery_configs()
    if gallery_name not in galleries:
        raise SystemExit("Unknown gallery name")
    return galleries[gallery_name]


async def main(gallery_name: object) -> object:
    config = load_gallery_config(gallery_name)
    intents = get_discord_intents()

    if config.get("type") == "arca":
        # ArcaBot은 Telegram을 쓰지 않으므로 DISCORD_TOKEN만 확인한다.
        if not TOKEN:
            print("필수 환경변수가 없습니다: DISCORD_TOKEN")
            sys.exit(1)
        # WEB_GALLERY=1 처리는 ArcaBot 내부에서 수행
        bot = ArcaBot(
            token=TOKEN,
            base_url=config["base_url"],
            channel_ids=config["channel_ids"],
            intents=intents,
            gallery_name=gallery_name,
        )
    else:
        validate_required_env()
        bot = DCBot(
            token=TOKEN,
            base_url=config["base_url"],
            channel_ids=config["channel_ids"],
            telegram_token=TELEGRAM_BOT_TOKEN,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            intents=intents,
            gallery_name=gallery_name,
        )

    await bot.run_bot()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python run_gallery.py <gallery_name>")
        print("  WEB_GALLERY=1 로 웹 갤러리 연동")
        sys.exit(1)

    asyncio.run(run_until_signal(main(sys.argv[1])))
