"""A5.4 notifier: Telegram (mocked Bot API over httpx.MockTransport), ntfy, and the signed decision
endpoint of the API. The live bot test with a real BotFather token is an operator step (HANDOFF).
"""

import datetime
import json
import logging
import uuid

import httpx
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution.approvals import create_approval
from cryptonite_notifier.bot import ALERT_LIST, OFFSET_KEY, Config, Notifier
from cryptonite_notifier.clients import ApiClient, NtfyClient, TelegramClient
from cryptonite_schemas import notifier as contract
from cryptonite_schemas.mandate import ActionClass
from migrated_db import migrated_database

TOKEN = "123456:TEST-token-never-real"
SECRET = "notifier-test-secret"
OPERATOR_CHAT = 4242
FOREIGN_CHAT = 999


class FakeTelegram:
    """The Bot API behind httpx.MockTransport: records calls, serves queued updates."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.next_id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.telegram.org" and request.url.path.startswith(f"/bot{TOKEN}/")
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": out})
        if method == "sendMessage":
            self.next_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, method="sendMessage"):
        return [b for m, b in self.calls if m == method]


class FakeApi:
    def __init__(self, status=200, body=None):
        self.requests: list[httpx.Request] = []
        self.status, self.body = status, body or {"status": "APPROVED", "command_id": str(uuid.uuid4())}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


class FakeNtfy:
    def __init__(self):
        self.posts: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(json.loads(request.content))
        return httpx.Response(200, json={})


class FakeRedis:
    def __init__(self):
        self.kv, self.lists = {}, {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    async def rpop(self, k):
        lst = self.lists.get(k) or []
        return lst.pop() if lst else None


def make_notifier(pool=None, api_status=200, api_body=None):
    tg, api, ntfy = FakeTelegram(), FakeApi(api_status, api_body), FakeNtfy()

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.telegram.org":
            return tg.handler(request)
        if request.url.host == "ntfy.test":
            return ntfy.handler(request)
        return api.handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(route))
    n = Notifier(Config(frozenset({str(OPERATOR_CHAT)}), SECRET), TelegramClient(TOKEN, http),
                 NtfyClient("https://ntfy.test", "cryptonite-critical", http), ApiClient("http://api.test", SECRET, http),
                 pool or FakePool(), FakeRedis())
    return n, tg, api, ntfy


class FakePool:
    def __init__(self, approval=None):
        self.approval = approval

    async def fetchrow(self, sql, *args):
        return self.approval

    async def fetchval(self, sql, *args):
        return 0

    async def fetch(self, sql, *args):
        return []


def approval_row(action="ADD_RISK", status="PENDING"):
    return {"id": uuid.uuid4(), "action_class": action, "command_type": "SUBMIT_ORDER", "payload": {"instrument_id": "BTCUSDT.BINANCE"},
            "summary": "BUY BTCUSDT 6% of mandate ($600)", "status": status, "level_at_creation": 1, "timeout_policy": "DENY",
            "expires_at": datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=10), "trace_id": None}


def press(chat, data, message_id=7):
    return {"update_id": 1, "callback_query": {"id": "cb1", "data": data, "message": {"message_id": message_id, "chat": {"id": chat}}}}


def text(chat, t, message_id=8):
    return {"update_id": 2, "message": {"message_id": message_id, "chat": {"id": chat}, "text": t}}


# ------------------------------------------------------------------------------------ allow-list


async def test_foreign_chats_are_dropped_without_a_reply(caplog):
    a = approval_row()
    n, tg, api, _ = make_notifier(FakePool(a))
    with caplog.at_level(logging.WARNING, logger="notifier"):
        assert await n.handle_update(press(FOREIGN_CHAT, contract.callback_data(SECRET, "a", a["id"]))) == "dropped"
        assert await n.handle_update(text(FOREIGN_CHAT, "/status")) == "dropped"
        assert await n.handle_update(text(FOREIGN_CHAT, "123456")) == "dropped"
    assert tg.calls == [] and api.requests == []  # not even answered
    assert "not allowed: 999" in caplog.text


async def test_forged_button_data_is_refused():
    n, tg, api, _ = make_notifier(FakePool(approval_row()))
    forged = f"a:{uuid.uuid4().hex}:deadbeef"
    assert await n.handle_update(press(OPERATOR_CHAT, forged)) == "forged"
    assert api.requests == []


# ------------------------------------------------------------------------------------ decisions


async def test_add_risk_approval_asks_for_totp_then_sends_a_signed_decision():
    a = approval_row("ADD_RISK")
    n, tg, api, _ = make_notifier(FakePool(a))
    assert await n.handle_update(press(OPERATOR_CHAT, contract.callback_data(SECRET, "a", a["id"]))) == "totp_requested"
    assert api.requests == []  # nothing decided before the code
    assert "TOTP" in tg.sent()[-1]["text"]

    assert await n.handle_update(text(OPERATOR_CHAT, "123456", message_id=55)) == "decided:APPROVED"
    (req,) = api.requests
    assert req.url.path == f"/api/v1/ai/notifier/approvals/{a['id']}"
    assert contract.verify_request(SECRET, "POST", req.url.path, req.content, req.headers) is not None
    assert json.loads(req.content) == {"decision": "APPROVE", "chat_id": str(OPERATOR_CHAT), "totp_code": "123456"}
    assert {"chat_id": OPERATOR_CHAT, "message_id": 55} in tg.sent("deleteMessage")  # the code leaves the chat
    assert tg.sent("editMessageReplyMarkup")[0]["message_id"] == 7  # buttons removed from the card


async def test_a_code_without_a_pending_approval_decides_nothing():
    n, tg, api, _ = make_notifier(FakePool(approval_row()))
    assert await n.handle_update(text(OPERATOR_CHAT, "123456")) == "totp_unexpected"
    assert api.requests == []


async def test_reduce_risk_approve_and_veto_are_one_tap():
    a = approval_row("REDUCE_RISK")
    n, tg, api, _ = make_notifier(FakePool(a))
    assert (await n.handle_update(press(OPERATOR_CHAT, contract.callback_data(SECRET, "a", a["id"])))).startswith("decided")
    assert json.loads(api.requests[-1].content)["totp_code"] is None
    b = approval_row("ADD_RISK")
    n2, _, api2, _ = make_notifier(FakePool(b), api_body={"status": "VETOED", "command_id": None})
    assert await n2.handle_update(press(OPERATOR_CHAT, contract.callback_data(SECRET, "v", b["id"]))) == "decided:VETOED"
    assert json.loads(api2.requests[-1].content)["decision"] == "VETO"


async def test_a_rejected_totp_is_reported():
    a = approval_row("ADD_RISK")
    n, tg, api, _ = make_notifier(FakePool(a), api_status=401, api_body={"detail": "TOTP_REQUIRED"})
    await n.handle_update(press(OPERATOR_CHAT, contract.callback_data(SECRET, "a", a["id"])))
    assert await n.handle_update(text(OPERATOR_CHAT, "000001")) == "totp_rejected"
    assert "nothing was approved" in tg.sent()[-1]["text"]


async def test_polling_keeps_the_offset_and_never_logs_the_token(caplog):
    n, tg, _, _ = make_notifier()
    tg.updates = [{**text(OPERATOR_CHAT, "/help"), "update_id": 41}]
    with caplog.at_level(logging.DEBUG):
        assert await n.poll_once() == 1
        await n.poll_once()
    assert n.redis.kv[OFFSET_KEY] == "42"
    polls = tg.sent("getUpdates")
    assert polls[1]["offset"] == 42 and polls[0]["timeout"] == 30  # long polling
    assert TOKEN not in caplog.text


# ------------------------------------------------------------------------------------ alerts


async def test_send_alert_and_critical_channel_alerts_reach_telegram_and_ntfy():
    n, tg, _, ntfy = make_notifier()
    n.redis.lists[ALERT_LIST] = [json.dumps({"title": "Kill switch", "message": "KILL SWITCH TRIPPED by op"})]
    assert await n.drain_alert_list_once() == 1
    assert "KILL SWITCH TRIPPED" in tg.sent()[-1]["text"] and tg.sent()[-1]["chat_id"] == str(OPERATOR_CHAT)
    assert ntfy.posts[-1]["topic"] == "cryptonite-critical" and ntfy.posts[-1]["priority"] == 5

    alert = {"type": "EXIT_MONITOR", "severity": "CRITICAL", "instrument_id": "BTCUSDT.BINANCE", "action": "ALERT", "reason": "NO_FRESH_QUOTE"}
    assert await n.on_system_alert(json.dumps(alert)) is True
    assert await n.on_system_alert(json.dumps(alert)) is False  # debounced
    assert await n.on_system_alert(json.dumps({"severity": "INFO", "kind": "X"})) is False
    assert await n.on_system_alert(json.dumps({"severity": "CRITICAL", "kind": "MANDATE_HARD_STOP"})) is False  # outbox has it
    assert len(ntfy.posts) == 2


def test_kill_switch_send_alert_is_queued_for_the_notifier(monkeypatch):
    import threading

    from cryptonite_execution import kill_switch

    pushed = []
    monkeypatch.setattr(kill_switch, "_push_to_notifier", lambda message, title: pushed.append((title, message)))
    monkeypatch.setattr(kill_switch, "SLACK_WEBHOOK_URL", None)
    done = threading.Event()
    real_thread = threading.Thread

    class Sync(real_thread):
        def start(self):
            self.run()
            done.set()

    monkeypatch.setattr(kill_switch.threading, "Thread", Sync)
    kill_switch.LatchedKillSwitch().trip(latched_by="op", reason="test")
    assert done.is_set() and pushed and pushed[0][0] == "Kill switch" and "KILL SWITCH TRIPPED" in pushed[0][1]


# ------------------------------------------------------------------------------ outbox (database)


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_notifier") as url:
        yield url


@pytest.fixture
async def h(db_url, monkeypatch):
    monkeypatch.setenv("NOTIFIER_HMAC_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(OPERATOR_CHAT))
    monkeypatch.setenv("ALLOW_DEV_TOTP", "false")
    harness = await make_harness(db_url)
    monkeypatch.setenv("NOTIFIER_OPERATOR_USERNAME", harness.username)
    try:
        yield harness
    finally:
        await close_harness(harness)


async def test_outbox_action_card_and_critical_alert(h):
    m = await h.mandate(level=1)
    async with h.pool.acquire() as conn:
        a = await create_approval(conn, m, ActionClass.ADD_RISK, "SUBMIT_ORDER", {"instrument_id": "BTCUSDT.BINANCE", "side": "BUY"},
                                  summary="BUY BTCUSDT 6% of mandate ($600)", source="test", source_ref=uuid.uuid4().hex)
        await conn.execute("INSERT INTO notifications_outbox (tier, kind, title, body) VALUES ('CRITICAL', 'MANDATE_HARD_STOP', 'Hard stop hit', 'Drawdown 20.1%')")
    n, tg, _, ntfy = make_notifier(h.pool)
    assert await n.dispatch_outbox_once() == 2
    card = next(b for b in tg.sent() if "ADD RISK" in b["text"])
    assert "⏱ 1" in card["text"] and "answer is no" in card["text"]  # 15:00 countdown; timeout means no
    buttons = card["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Approve 🔐", "Veto", "Details"]
    assert contract.parse_callback_data(SECRET, buttons[0]["callback_data"]) == ("APPROVE", a["id"])
    assert any("Hard stop hit" in b["text"] for b in tg.sent()) and ntfy.posts[-1]["title"] == "Cryptonite: Hard stop hit"
    assert await h.pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE status = 'SENT'") >= 2
    assert await n.dispatch_outbox_once() == 0  # sent once


async def test_a_critical_alert_with_no_channel_is_an_error():
    n, _, _, _ = make_notifier()
    n.telegram = None
    n.ntfy = None
    with pytest.raises(RuntimeError):
        await n.send_critical("x", "y")  # nowhere to deliver a Critical alert is an error


# ------------------------------------------------------------------ API: signed decision endpoint


async def signed_post(h, approval_id, body: dict, *, secret=SECRET, nonce=None):
    path = f"/api/v1/ai/notifier/approvals/{approval_id}"
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", **contract.sign_request(secret, "POST", path, raw, nonce=nonce)}
    async with h.client() as c:
        c.cookies.clear()  # machine call: no operator session
        return await c.post(path, content=raw, headers=headers)


async def pending(h, action=ActionClass.ADD_RISK):
    m = await h.mandate(level=1)
    payload = {"instrument_id": "BTCUSDT.BINANCE", "side": "BUY"} if action is ActionClass.ADD_RISK else {"instrument_id": "BTCUSDT.BINANCE", "quantity": "0.01"}
    async with h.pool.acquire() as conn:
        return await create_approval(conn, m, action, "SUBMIT_ORDER" if action is ActionClass.ADD_RISK else "CLOSE_POSITION",
                                     payload, summary="s", source="test", source_ref=uuid.uuid4().hex)


async def test_signed_endpoint_rejects_bad_signatures_replays_and_foreign_chats(h):
    a = await pending(h)
    veto = {"decision": "VETO", "chat_id": str(OPERATOR_CHAT)}
    assert (await signed_post(h, a["id"], veto, secret="wrong")).status_code == 401
    assert (await signed_post(h, a["id"], {**veto, "chat_id": str(FOREIGN_CHAT)})).status_code == 403
    nonce = uuid.uuid4().hex
    ok = await signed_post(h, a["id"], veto, nonce=nonce)
    assert ok.status_code == 200 and ok.json()["status"] == "VETOED"
    assert (await signed_post(h, a["id"], veto, nonce=nonce)).status_code == 401  # replayed nonce


async def test_telegram_add_risk_approval_needs_a_valid_totp(h):
    a = await pending(h)
    await h.redis.delete(f"ai:totp_failures:telegram:{h.username}")
    approve = {"decision": "APPROVE", "chat_id": str(OPERATOR_CHAT)}
    assert (await signed_post(h, a["id"], approve)).status_code == 401
    assert (await signed_post(h, a["id"], {**approve, "totp_code": "000000"})).status_code == 401  # dev bypass is off
    wrong = f"{(int(h.totp.now()) + 1) % 1_000_000:06d}"
    assert (await signed_post(h, a["id"], {**approve, "totp_code": wrong})).status_code == 401
    assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", a["id"]) == "PENDING"
    ok = await signed_post(h, a["id"], {**approve, "totp_code": h.totp.now()})
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED" and ok.json()["command_id"]
    row = await h.pool.fetchrow("SELECT decided_via, decided_by FROM ai_approvals WHERE id = $1", a["id"])
    assert (row["decided_via"], row["decided_by"]) == ("telegram", f"telegram:{OPERATOR_CHAT}")


async def test_telegram_reduce_risk_approval_is_one_tap(h):
    a = await pending(h, ActionClass.REDUCE_RISK)
    ok = await signed_post(h, a["id"], {"decision": "APPROVE", "chat_id": str(OPERATOR_CHAT)})
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"
    cmd = await h.pool.fetchrow("SELECT command_type FROM execution_commands WHERE command_id = $1", uuid.UUID(ok.json()["command_id"]))
    assert cmd["command_type"] == "CLOSE_POSITION"


async def test_telegram_totp_is_locked_out_after_five_wrong_codes(h):
    a = await pending(h)
    await h.redis.delete(f"ai:totp_failures:telegram:{h.username}")
    approve = {"decision": "APPROVE", "chat_id": str(OPERATOR_CHAT)}
    wrong = f"{(int(h.totp.now()) + 7) % 1_000_000:06d}"
    for _ in range(5):
        assert (await signed_post(h, a["id"], {**approve, "totp_code": wrong})).status_code == 401
    locked = await signed_post(h, a["id"], {**approve, "totp_code": h.totp.now()})
    assert locked.status_code == 429  # even the right code, until the lockout ends
    assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", a["id"]) == "PENDING"
    await h.redis.delete(f"ai:totp_failures:telegram:{h.username}")


async def test_in_an_allowed_group_only_allowed_people_are_served():
    n, tg, api, _ = make_notifier(FakePool(approval_row("REDUCE_RISK")))
    stranger = {"update_id": 3, "message": {"message_id": 9, "chat": {"id": OPERATOR_CHAT}, "from": {"id": FOREIGN_CHAT}, "text": "/status"}}
    assert await n.handle_update(stranger) == "dropped"
    me = {"update_id": 4, "message": {"message_id": 9, "chat": {"id": OPERATOR_CHAT}, "from": {"id": OPERATOR_CHAT}, "text": "/help"}}
    assert await n.handle_update(me) == "help"


async def test_the_notifier_role_can_deliver_but_never_decide(h, db_url):
    """Least privilege (review finding): the notifier marks outbox rows sent; it cannot decide approvals or edit mandates."""
    from urllib.parse import urlsplit, urlunsplit

    import asyncpg

    parts = urlsplit(db_url)
    role_url = urlunsplit(parts._replace(netloc=f"cryptonite_notifier:notifier_dev_pass@{parts.netloc.rsplit('@', 1)[-1]}"))
    await h.pool.execute("INSERT INTO notifications_outbox (tier, kind, title, body) VALUES ('INFO', 'TEST', 'hello', 'world')")
    pool = await asyncpg.create_pool(role_url, min_size=1, max_size=2)
    try:
        n, tg, _, _ = make_notifier(pool)
        assert await n.dispatch_outbox_once() >= 1
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.execute("UPDATE ai_approvals SET status = 'APPROVED'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.execute("UPDATE ai_mandates SET autonomy_level = 3")
    finally:
        await pool.close()
