"""The notifier (AI-first blueprint §4.6, A5.4).

- Telegram, long polling (``getUpdates``): no webhook, no inbound port. Only chats in
  ``TELEGRAM_ALLOWED_CHAT_IDS`` are served; anything else is dropped and logged (chat id only), with no
  reply (§10 invariant 7).
- Action cards (approvals) come from ``notifications_outbox`` with Approve / Veto / Details buttons.
  Button data carries an HMAC tag. Veto and approving a risk-reducing action are one tap. Approving
  new risk (ADD_RISK) asks for the operator's TOTP code; the code message is deleted from the chat.
  Every decision is an HMAC-signed call to the API, which re-checks the chat, the TOTP and the state.
- Critical alerts (outbox rows, ``send_alert`` via the ``notifier:alerts`` list, and Critical
  ``channel:system_alerts`` messages) go to Telegram **and** ntfy.
"""

import asyncio
import datetime
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptonite_schemas import notifier as contract

from cryptonite_notifier import render
from cryptonite_notifier.clients import ApiClient, NtfyClient, TelegramClient, TelegramError

logger = logging.getLogger("notifier")

ALERT_LIST = "notifier:alerts"  # kill_switch.send_alert pushes here
ALERT_CHANNEL = "channel:system_alerts"
HEARTBEAT_KEY = "notifier:heartbeat"
OFFSET_KEY = "notifier:telegram:offset"
# these reach Telegram through the outbox (the mandate monitor writes them there and on the channel)
OUTBOX_KINDS = {"MANDATE_HARD_STOP", "ACCOUNT_BACKSTOP_DRAWDOWN", "HEARTBEAT_DOWN",
                "MANDATE_DAILY_LOSS", "MANDATE_PROFIT_LOCK", "MANDATE_DAILY_TARGET",
                "PROFILE_BREAKER", "PROFILE_BREAKER_RESUMED", "PROFILE_FILL", "PROFILE_EXIT", "PROFILE_DAILY_SUMMARY"}  # ADR 0010 daily sessions
TOTP_WINDOW_S = 90
MAX_ATTEMPTS = 5
TOTP_RE = re.compile(r"^\d{6}$")


@dataclass
class Config:
    allowed_chat_ids: frozenset[str]
    hmac_secret: str

    @staticmethod
    def parse_chat_ids(raw: str) -> frozenset[str]:
        return frozenset(c.strip() for c in (raw or "").split(",") if c.strip())


@dataclass
class PendingTotp:
    approval_id: uuid.UUID
    card_message_id: int | None
    deadline: float


@dataclass
class Notifier:
    config: Config
    telegram: TelegramClient | None
    ntfy: NtfyClient | None
    api: ApiClient
    pool: Any
    redis: Any
    pending: dict[str, PendingTotp] = field(default_factory=dict)

    # ------------------------------------------------------------------ Telegram updates

    def allowed(self, chat_id: Any) -> bool:
        return chat_id is not None and str(chat_id) in self.config.allowed_chat_ids

    def _sender_allowed(self, obj: dict[str, Any]) -> bool:
        """The person, not just the chat: in a group every member shares the chat id. In a private chat
        the user id equals the chat id, so allow-listing your chat id covers both."""
        sender = (obj.get("from") or {}).get("id")
        return sender is None or self.allowed(sender)

    async def handle_update(self, update: dict[str, Any]) -> str:
        """Returns what happened (for logs and tests)."""
        if cb := update.get("callback_query"):
            chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
            if not self.allowed(chat_id) or not self._sender_allowed(cb):
                logger.warning("Dropped a button press from a chat that is not allowed: %s", chat_id)
                return "dropped"
            return await self._on_callback(cb, chat_id)
        if msg := update.get("message"):
            chat_id = (msg.get("chat") or {}).get("id")
            if not self.allowed(chat_id) or not self._sender_allowed(msg):
                logger.warning("Dropped a message from a chat that is not allowed: %s", chat_id)
                return "dropped"
            return await self._on_message(msg, chat_id)
        return "ignored"

    async def _approval(self, approval_id: uuid.UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """SELECT id, action_class, command_type, payload, summary, status, level_at_creation, timeout_policy,
                      expires_at, trace_id FROM ai_approvals WHERE id = $1""", approval_id)
        if row is None:
            return None
        a = dict(row)
        a["payload"] = json.loads(a["payload"]) if isinstance(a["payload"], str) else a["payload"]
        return a

    async def _on_callback(self, cb: dict[str, Any], chat_id: Any) -> str:
        parsed = contract.parse_callback_data(self.config.hmac_secret, cb.get("data") or "")
        if parsed is None:
            await self.telegram.answer_callback(cb["id"], "Unknown action")
            return "forged"
        action, approval_id = parsed
        card_id = (cb.get("message") or {}).get("message_id")
        if action == "DETAILS":
            a = await self._approval(approval_id)
            await self.telegram.answer_callback(cb["id"])
            await self.telegram.send_message(chat_id, render.details_text(a) if a else "Approval not found")
            return "details"
        if action == "VETO":
            await self.telegram.answer_callback(cb["id"], "Vetoing…")
            return await self._decide(chat_id, approval_id, "VETO", None, card_id)
        a = await self._approval(approval_id)
        if a is None:
            await self.telegram.answer_callback(cb["id"], "Approval not found")
            return "missing"
        if a["action_class"] == "ADD_RISK":
            self.pending[str(chat_id)] = PendingTotp(approval_id, card_id, time.monotonic() + TOTP_WINDOW_S)
            await self.telegram.answer_callback(cb["id"], "TOTP needed")
            await self.telegram.send_message(
                chat_id, f"🔐 Approving new risk needs your TOTP. Send the 6-digit code within {TOTP_WINDOW_S} s.\n"
                         f"{a['summary']}")
            return "totp_requested"
        await self.telegram.answer_callback(cb["id"], "Approving…")
        return await self._decide(chat_id, approval_id, "APPROVE", None, card_id)

    async def _on_message(self, msg: dict[str, Any], chat_id: Any) -> str:
        text = (msg.get("text") or "").strip()
        if TOTP_RE.match(text):
            pending = self.pending.pop(str(chat_id), None)
            try:  # the code is single-use anyway, but don't leave it in the chat
                await self.telegram.delete_message(chat_id, msg["message_id"])
            except (TelegramError, KeyError):
                pass
            if pending is None or pending.deadline < time.monotonic():
                await self.telegram.send_message(chat_id, "No approval is waiting for a TOTP code. Tap Approve 🔐 first.")
                return "totp_unexpected"
            return await self._decide(chat_id, pending.approval_id, "APPROVE", text, pending.card_message_id)
        if text.startswith("/start") or text.startswith("/help"):
            await self.telegram.send_message(
                chat_id, "Cryptonite notifier. Approval cards arrive here; Approve 🔐 asks for your TOTP. /status shows the queue.")
            return "help"
        if text.startswith("/status"):
            n = await self.pool.fetchval("SELECT count(*) FROM ai_approvals WHERE status = 'PENDING'")
            halted = await self.pool.fetch("SELECT name FROM ai_mandates WHERE status = 'HALTED'")
            await self.telegram.send_message(
                chat_id, f"Pending approvals: {n}\nHalted mandates: {', '.join(r['name'] for r in halted) or 'none'}")
            return "status"
        return "ignored"

    async def _decide(self, chat_id: Any, approval_id: uuid.UUID, decision: str, totp: str | None,
                      card_message_id: int | None) -> str:
        try:
            status, body = await self.api.decide(str(approval_id), decision, chat_id, totp)
        except Exception as e:
            logger.error("Decision call failed: %s", type(e).__name__)
            await self.telegram.send_message(chat_id, "Could not reach Cryptonite: nothing was decided. Try again or use Mission Control.")
            return "api_unreachable"
        if status == 200:
            done = body.get("status", decision)
            cmd = body.get("command_id")
            await self.telegram.send_message(chat_id, f"✅ {done}" + (f" · command {str(cmd)[:8]} queued" if cmd else ""))
            if card_message_id:
                try:
                    await self.telegram.clear_buttons(chat_id, card_message_id)
                except TelegramError:
                    pass
            return f"decided:{done}"
        detail = body.get("detail") or f"HTTP {status}"
        if status == 401 and "TOTP" in str(detail):
            await self.telegram.send_message(chat_id, "❌ TOTP rejected: nothing was approved.")
            return "totp_rejected"
        await self.telegram.send_message(chat_id, f"❌ Not decided: {detail}")
        return f"refused:{status}"

    async def poll_once(self) -> int:
        raw = await self.redis.get(OFFSET_KEY)
        updates = await self.telegram.get_updates(int(raw) if raw else None)
        for u in updates:
            try:
                await self.handle_update(u)
            except Exception as e:
                logger.error("Update %s failed: %s", u.get("update_id"), type(e).__name__)
            await self.redis.set(OFFSET_KEY, str(int(u["update_id"]) + 1))
        return len(updates)

    # ------------------------------------------------------------------ outgoing

    async def _to_all_chats(self, text: str, buttons=None) -> int | None:
        first = None
        if self.telegram is None:
            return None
        for chat in sorted(self.config.allowed_chat_ids):
            msg = await self.telegram.send_message(chat, text, buttons)
            first = first or (msg or {}).get("message_id")
        return first

    async def send_critical(self, title: str, body: str) -> None:
        """Telegram and ntfy, independently. Raises only when no channel delivered it."""
        delivered, errors = 0, []
        if self.telegram is not None:
            try:
                await self._to_all_chats(render.plain_text("CRITICAL", title, body))
                delivered += 1
            except Exception as e:
                errors.append(f"telegram: {type(e).__name__}")
        if self.ntfy is not None and self.ntfy.enabled:
            try:
                await self.ntfy.publish(f"Cryptonite: {title}", body, priority=5)
                delivered += 1
            except Exception as e:
                errors.append(f"ntfy: {e}")
        if not delivered:
            raise RuntimeError("Critical alert not delivered: " + ("; ".join(errors) or "no channel configured"))
        if errors:
            logger.error("Critical alert partly delivered: %s", "; ".join(errors))

    async def dispatch_outbox_once(self, now: datetime.datetime | None = None, limit: int = 20) -> int:
        now = now or datetime.datetime.now(datetime.UTC)
        sent = 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """SELECT id, tier, kind, title, body, payload, approval_id, attempts FROM notifications_outbox
                       WHERE status = 'PENDING' ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED""", limit)
                for r in rows:
                    row = dict(r)
                    try:
                        status, message_id = await self._deliver(row, now)
                        await conn.execute(
                            "UPDATE notifications_outbox SET status = $2, sent_at = now(), telegram_message_id = $3, attempts = attempts + 1 WHERE id = $1",
                            row["id"], status, message_id)
                        sent += status == "SENT"
                    except Exception as e:
                        failed = row["attempts"] + 1 >= MAX_ATTEMPTS
                        logger.error("Notification %s failed (%s): %s", row["id"], row["kind"], str(e)[:200])
                        await conn.execute(
                            "UPDATE notifications_outbox SET attempts = attempts + 1, last_error = $2, status = $3 WHERE id = $1",
                            row["id"], str(e)[:500], "FAILED" if failed else "PENDING")
        return sent

    async def _deliver(self, row: dict[str, Any], now: datetime.datetime) -> tuple[str, int | None]:
        if row["tier"] == "ACTION" and row["approval_id"]:
            a = await self._approval(row["approval_id"])
            if a is None or a["status"] != "PENDING" or a["expires_at"] <= now:
                return "SKIPPED", None  # decided or expired before it could be shown
            text = render.approval_text(row, a, now)
            return "SENT", await self._to_all_chats(text, render.approval_buttons(self.config.hmac_secret, a))
        if row["tier"] == "CRITICAL":
            await self.send_critical(row["title"], row["body"])
            return "SENT", None
        return "SENT", await self._to_all_chats(render.plain_text(row["tier"], row["title"], row["body"]))

    async def drain_alert_list_once(self, limit: int = 20) -> int:
        """``send_alert`` (kill switch, daily breaker) -> Telegram + ntfy."""
        n = 0
        for _ in range(limit):
            raw = await self.redis.rpop(ALERT_LIST)
            if raw is None:
                break
            try:
                alert = json.loads(raw)
            except ValueError:
                alert = {"message": str(raw)}
            await self.send_critical(alert.get("title") or "Alert", alert.get("message") or "")
            n += 1
        return n

    async def on_system_alert(self, raw: str) -> bool:
        """Critical ``channel:system_alerts`` messages (exit monitor, daily breaker) -> Telegram + ntfy."""
        try:
            alert = json.loads(raw)
        except ValueError:
            return False
        kind = str(alert.get("kind") or alert.get("reason") or "")
        critical = alert.get("severity") == "CRITICAL" or kind == "DAILY_DRAWDOWN_LIMIT_BREACHED"
        if not critical or kind in OUTBOX_KINDS or alert.get("type") == "mandate_updated":
            return False
        dedupe = f"notifier:alert:{kind}:{alert.get('instrument_id', '')}:{alert.get('action', '')}"
        if not await self.redis.set(dedupe, "1", nx=True, ex=600):
            return False
        title, body = render.alert_text(alert)
        await self.send_critical(title, body)
        return True

    # ------------------------------------------------------------------ loops

    async def heartbeat(self) -> None:
        await self.redis.set(HEARTBEAT_KEY, json.dumps({
            "telegram": self.telegram is not None, "ntfy": bool(self.ntfy and self.ntfy.enabled),
            "chats": len(self.config.allowed_chat_ids)}), ex=90)

    async def _loop(self, name: str, fn, interval: float) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                logger.error("%s: %s", name, e)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("%s failed: %s", name, str(e)[:200])
                await asyncio.sleep(2)
            await asyncio.sleep(interval)

    async def _alerts_channel(self) -> None:
        while True:
            pubsub = self.redis.pubsub()
            try:
                await pubsub.subscribe(ALERT_CHANNEL)
                async for message in pubsub.listen():
                    if message.get("type") == "message":
                        try:
                            await self.on_system_alert(message["data"])
                        except Exception as e:
                            logger.error("System alert relay failed: %s", str(e)[:200])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Alert channel error: %s", e)
                await asyncio.sleep(2)
            finally:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass

    async def run(self) -> None:
        tasks = [
            self._loop("heartbeat", self.heartbeat, 30),
            self._loop("outbox", self.dispatch_outbox_once, 2),
            self._loop("alert list", self.drain_alert_list_once, 2),
            self._alerts_channel(),
        ]
        if self.telegram is not None:
            tasks.append(self._loop("telegram poll", self.poll_once, 0))
        await asyncio.gather(*tasks)
