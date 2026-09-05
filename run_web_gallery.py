"""Run one crawler and its RAM gallery under one supervised event loop."""
import asyncio
import secrets
import sys
from contextlib import contextmanager

import uvicorn

from Module.arca_bot import ArcaBot
from Module.config import app_config, get_discord_intents, load_gallery_configs
from Module.dcbot import DCBot
from Module.lifecycle import run_until_signal
from web_app import create_app


class EmbeddedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The parent owns cancellation and joins both services.
        yield


async def supervise(server, bot):
    web_task = asyncio.create_task(server.serve())
    bot_task = None
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if web_task.done():
                    await web_task
                    raise RuntimeError("Web server exited before readiness")
                await asyncio.sleep(0.05)
        bot_task = asyncio.create_task(bot.run_bot())
        done, _ = await asyncio.wait((web_task, bot_task), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        if bot_task is not None:
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        else:
            await bot.close()
        server.should_exit = True
        try:
            await asyncio.wait_for(web_task, timeout=10)
        except TimeoutError:
            web_task.cancel()
            await asyncio.gather(web_task, return_exceptions=True)


async def main(gallery_name):
    galleries = load_gallery_configs()
    if gallery_name not in galleries:
        raise ValueError("Unknown gallery name")
    config = galleries[gallery_name]
    if not app_config.discord_token:
        raise ValueError("DISCORD_TOKEN is required")
    if config.get("type") != "arca":
        app_config.validate_required()
    app_config.web_ingest_token = app_config.web_ingest_token or secrets.token_urlsafe(32)
    app_config.web_gallery = True
    local_host = "[::1]" if ":" in app_config.web_host else "127.0.0.1"
    app_config.web_gallery_url = f"http://{local_host}:{app_config.web_port}"
    kwargs = dict(token=app_config.discord_token, base_url=config["base_url"],
                  channel_ids=config["channel_ids"], intents=get_discord_intents(), gallery_name=gallery_name)
    if config.get("type") == "arca":
        bot = ArcaBot(**kwargs)
    else:
        bot = DCBot(**kwargs, telegram_token=app_config.telegram_token, telegram_chat_id=app_config.telegram_chat_id)
    server = EmbeddedServer(uvicorn.Config(create_app(), host=app_config.web_host,
                            port=app_config.web_port, log_level="info", timeout_graceful_shutdown=10))
    await supervise(server, bot)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python run_web_gallery.py <gallery_name>")
    asyncio.run(run_until_signal(main(sys.argv[1])))
