# config.py
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_ARCHIVE_PATH = "var/delivery_archive.sqlite3"


@dataclass
class AppConfig:
    """Centralized application configuration loaded from environment variables.

    Usage:
        from Module.config import app_config
        token = app_config.discord_token
    """

    # Discord
    discord_token: str = ""
    discord_max_size_mb: int = 10

    # Telegram
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # Web gallery
    web_static_dir: str = "web_static"
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    web_image_ttl_seconds: int = 10800  # 3 hours
    web_feed_max_items: int = 120
    web_thumb_width: int = 480
    web_memory_max_mb: int = 256
    web_image_max_mb: int = 12
    web_ingest_max_mb: int = 12
    web_upload_queue_size: int = 20
    web_freshness_seconds: int = 900
    web_gallery_url: str = "http://127.0.0.1:8000"
    web_ingest_token: str = ""
    web_maintenance: bool = False
    web_maintenance_file: str = ""
    web_gallery: bool = False

    # Turnstile (Cloudflare bot protection)
    turnstile_sitekey: str = ""
    turnstile_secret: str = ""

    # Shared secret between Cloudflare (or another reverse proxy) and this origin.
    # When set, a request carrying cf-connecting-ip must also carry the matching
    # x-origin-secret header, otherwise the header is ignored and the socket peer
    # address is used instead (defends against direct-to-origin header spoofing).
    web_origin_secret: str = ""

    # Arca (SOCKS proxy for arcalive crawler)
    arca_socks_proxy: str = ""
    arca_download_concurrency: int = 2

    # Source media safety limits. Originals within these bounds remain untouched.
    media_download_max_mb: int = 15
    media_max_pixels: int = 24_000_000

    # Successful-delivery deduplication metadata (never image bytes).
    archive_path: str = DEFAULT_ARCHIVE_PATH

    # Dashboard
    dash_host: str = "127.0.0.1"
    dash_base_url: str = ""

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Build an AppConfig instance by reading all environment variables."""
        # DISCORD_MAX_SIZE_MB with safe fallback
        try:
            discord_max_size_mb_val = int(float(os.getenv("DISCORD_MAX_SIZE_MB", "10")))
        except ValueError:
            print("DISCORD_MAX_SIZE_MB 값이 잘못되었습니다. 기본값 10MB를 사용합니다.")
            discord_max_size_mb_val = 10

        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", ""),
            discord_max_size_mb=discord_max_size_mb_val,
            telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHANNEL", ""),
            web_static_dir=os.getenv("WEB_STATIC_DIR", "web_static"),
            web_host=os.getenv("WEB_HOST", "127.0.0.1"),
            web_port=int(os.getenv("WEB_PORT", "8000")),
            web_image_ttl_seconds=int(os.getenv("WEB_IMAGE_TTL_SECONDS", str(3 * 60 * 60))),
            web_feed_max_items=int(os.getenv("WEB_FEED_MAX_ITEMS", "120")),
            web_thumb_width=int(os.getenv("WEB_THUMB_WIDTH", "480")),
            web_memory_max_mb=int(os.getenv("WEB_MEMORY_MAX_MB", "256")),
            web_image_max_mb=int(os.getenv("WEB_IMAGE_MAX_MB", "12")),
            web_ingest_max_mb=int(os.getenv("WEB_INGEST_MAX_MB", "12")),
            web_upload_queue_size=int(os.getenv("WEB_UPLOAD_QUEUE_SIZE", "20")),
            web_freshness_seconds=int(os.getenv("WEB_FRESHNESS_SECONDS", "900")),
            web_gallery_url=os.getenv("WEB_GALLERY_URL", "http://127.0.0.1:8000"),
            web_ingest_token=os.getenv("WEB_INGEST_TOKEN", ""),
            web_maintenance=os.getenv("WEB_MAINTENANCE") == "1",
            web_maintenance_file=os.getenv("WEB_MAINTENANCE_FILE", ""),
            web_gallery=os.getenv("WEB_GALLERY") == "1",
            turnstile_sitekey=os.getenv("TURNSTILE_SITEKEY", ""),
            turnstile_secret=os.getenv("TURNSTILE_SECRET", ""),
            web_origin_secret=os.getenv("WEB_ORIGIN_SECRET", ""),
            arca_socks_proxy=os.getenv("ARCA_SOCKS_PROXY", ""),
            arca_download_concurrency=int(os.getenv("ARCA_DOWNLOAD_CONCURRENCY", "2")),
            media_download_max_mb=int(os.getenv("MEDIA_DOWNLOAD_MAX_MB", "15")),
            media_max_pixels=int(os.getenv("MEDIA_MAX_PIXELS", "24000000")),
            archive_path=os.getenv("ARCHIVE_PATH") or DEFAULT_ARCHIVE_PATH,
            dash_host=os.getenv("DASH_HOST", "127.0.0.1"),
            dash_base_url=os.getenv("DASH_BASE_URL", "").strip().rstrip("/"),
        )

    @property
    def discord_max_size_bytes(self) -> int:
        """Discord max upload size in bytes."""
        return self.discord_max_size_mb * 1024 * 1024

    @property
    def maintenance_file_path(self) -> Path:
        """Path to the maintenance flag file."""
        if self.web_maintenance_file:
            return Path(self.web_maintenance_file)
        return Path(self.web_static_dir).parent / ".maintenance"

    def validate_required(self) -> None:
        """Check that all required environment variables are set; exit if not."""
        required = {
            "DISCORD_TOKEN": self.discord_token,
            "TELEGRAM_TOKEN": self.telegram_token,
            "TELEGRAM_CHANNEL": self.telegram_chat_id,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            print(f"필수 환경변수 누락: {', '.join(missing)}")
            print(".env 파일을 확인해주세요.")
            sys.exit(1)


# ── Singleton instance (read once at import time) ─────────────────────
app_config = AppConfig.from_env()

# ====================================================================
# Backward-compatible module-level re-exports
#   Existing code that does `from Module.config import TOKEN` etc.
#   will continue to work unchanged.
# ====================================================================

TOKEN = app_config.discord_token
TELEGRAM_BOT_TOKEN = app_config.telegram_token
TELEGRAM_CHAT_ID = app_config.telegram_chat_id
DISCORD_MAX_SIZE = app_config.discord_max_size_bytes

# ── Non-env-derived constants (stay at module level) ─────────────────

# HTTP 요청 타임아웃 (초)
REQUEST_TIMEOUT = 15

# BeautifulSoup 파서 (lxml이 설치되어 있으면 사용 — html.parser보다 수 배 빠름)
try:
    import lxml  # noqa: F401
    BS_PARSER = "lxml"
except ImportError:
    BS_PARSER = "html.parser"

# 헤더 설정
HEADERS = {
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
    "sec-ch-ua-mobile": "?0",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 디스코드 인텐트 설정
def get_discord_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    return intents


def validate_required_env() -> None:
    """필수 환경변수 검증 (봇 시작 시 호출 — import 시점에는 검증하지 않음)."""
    app_config.validate_required()


# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
