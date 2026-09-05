"""Redact transport URLs and configured credentials before handlers persist logs."""
import logging
import os
import re


class PrivacyFilter(logging.Filter):
    def filter(self, record):
        text = record.getMessage()
        text = re.sub(r"(?:https?|socks5h?)://[^\s\"']+", "[redacted-url]", text)
        for name in ("DISCORD_TOKEN", "TELEGRAM_TOKEN", "WEB_INGEST_TOKEN", "TURNSTILE_SECRET", "WEB_ORIGIN_SECRET"):
            value = os.getenv(name)
            if value:
                text = text.replace(value, "[redacted]")
        record.msg, record.args = text, ()
        if record.exc_info:
            # Tracebacks can repeat credential-bearing URLs from transport errors.
            record.msg += f" (exception={record.exc_info[0].__name__})"
            record.exc_info = None
            record.exc_text = None
        return True


def install_logging_privacy():
    for name in ("httpx", "httpcore", "urllib3", "telegram", "discord"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        if not any(isinstance(f, PrivacyFilter) for f in logger.filters):
            logger.addFilter(PrivacyFilter())
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, PrivacyFilter) for f in handler.filters):
            handler.addFilter(PrivacyFilter())
