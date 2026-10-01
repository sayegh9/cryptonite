"""python -m cryptonite_notifier — configuration comes from the environment only (never commit tokens)."""

import asyncio
import logging
import os

import asyncpg
import httpx
import redis.asyncio as aioredis

from cryptonite_notifier.bot import Config, Notifier
from cryptonite_notifier.clients import ApiClient, NtfyClient, TelegramClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("notifier")


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chats = Config.parse_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    secret = os.getenv("NOTIFIER_HMAC_SECRET", "")
    if not secret:
        raise SystemExit("NOTIFIER_HMAC_SECRET is required (shared with the api service)")
    if token and not chats:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_IDS is required: the bot serves only allow-listed chats")
    db_url = os.getenv("DATABASE_URL", "postgresql://cryptonite_api:api_dev_pass@127.0.0.1:5432/cryptonite")
    pool = await asyncpg.create_pool(db_url.replace("postgresql+asyncpg://", "postgresql://"), min_size=1, max_size=3)
    redis = aioredis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True,
                              socket_timeout=60)
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as http:
        telegram = TelegramClient(token, http) if token else None
        if telegram is None:
            logger.error("TELEGRAM_BOT_TOKEN not set: Telegram disabled, Critical alerts go to ntfy only")
        ntfy = NtfyClient(os.getenv("NTFY_URL", "https://ntfy.sh"), os.getenv("NTFY_TOPIC", ""), http, os.getenv("NTFY_TOKEN"))
        if not ntfy.enabled:
            logger.warning("NTFY_TOPIC not set: Critical alerts go to Telegram only")
        api = ApiClient(os.getenv("CRYPTONITE_API_URL", "http://api:8000"), secret, http)
        notifier = Notifier(Config(chats, secret), telegram, ntfy, api, pool, redis)
        logger.info("Notifier running: telegram=%s chats=%d ntfy=%s", telegram is not None, len(chats), ntfy.enabled)
        await notifier.run()


if __name__ == "__main__":
    asyncio.run(main())
