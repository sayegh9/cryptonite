"""Thin httpx clients: the Telegram Bot API, ntfy and the Cryptonite API. No SDKs.

The bot token is part of every Telegram URL, so the httpx request logger is silenced (it logs full
URLs at INFO) and errors are reported without the URL.
"""

import json
import logging
from typing import Any

import httpx
from cryptonite_schemas import notifier as contract

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("notifier.clients")

TELEGRAM_API = "https://api.telegram.org"
LONG_POLL_S = 30


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient, base_url: str = TELEGRAM_API) -> None:
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._http = http

    async def call(self, method: str, payload: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        try:
            resp = await self._http.post(f"{self._base}/{method}", json=payload or {}, timeout=timeout)
        except httpx.HTTPError as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None  # never the URL: it holds the token
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status_code}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('error_code')} {body.get('description', '')}"[:300])
        return body.get("result")

    async def get_updates(self, offset: int | None, timeout: int = LONG_POLL_S) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return await self.call("getUpdates", payload, timeout=timeout + 10) or []

    async def send_message(self, chat_id: int | str, text: str, buttons: list[list[dict[str, str]]] | None = None) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return await self.call("sendMessage", payload)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    async def clear_buttons(self, chat_id: int | str, message_id: int) -> None:
        await self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                                   "reply_markup": {"inline_keyboard": []}})

    async def delete_message(self, chat_id: int | str, message_id: int) -> None:
        await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


class NtfyClient:
    """ntfy (https://ntfy.sh or self-hosted) for Critical alerts: a second, independent channel."""

    def __init__(self, url: str, topic: str, http: httpx.AsyncClient, token: str | None = None) -> None:
        self._url = url.rstrip("/")
        self._topic = topic
        self._http = http
        self._token = token

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._topic)

    async def publish(self, title: str, message: str, priority: int = 5, tags: list[str] | None = None) -> None:
        if not self.enabled:
            return
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        body = {"topic": self._topic, "title": title[:250], "message": message[:4000], "priority": priority,
                "tags": tags or ["rotating_light"]}
        resp = await self._http.post(self._url, content=json.dumps(body), headers=headers, timeout=10.0)
        if resp.status_code >= 400:
            raise RuntimeError(f"ntfy: HTTP {resp.status_code}")


class ApiClient:
    """Decisions go to the Cryptonite API, HMAC-signed with a single-use nonce."""

    def __init__(self, base_url: str, secret: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._secret = secret
        self._http = http

    async def decide(self, approval_id: str, decision: str, chat_id: int | str, totp_code: str | None = None) -> tuple[int, dict]:
        path = f"/api/v1/ai/notifier/approvals/{approval_id}"
        body = json.dumps({"decision": decision, "chat_id": str(chat_id), "totp_code": totp_code}).encode()
        headers = {"Content-Type": "application/json", **contract.sign_request(self._secret, "POST", path, body)}
        resp = await self._http.post(f"{self._base}{path}", content=body, headers=headers, timeout=15.0)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp.status_code, data
