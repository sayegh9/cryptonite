"""StreamHub perpetual relay: Redis perp channels -> framed WebSocket messages on /ws/stream.

Uses the real ws route and a real Redis, with private channel names and key prefix so a running
feed/orchestrator is never affected.
"""

import json
import time
import uuid
from contextlib import asynccontextmanager

import pytest
import redis
from connections import REDIS_URL
from cryptonite_api.routes.ws import router as ws_router
from cryptonite_api.stream_gateway import ClientConnection, StreamHub
from fastapi import FastAPI
from fastapi.testclient import TestClient

BTC = "BTCUSDT-PERP.BINANCE"
ETH = "ETHUSDT-PERP.BINANCE"


@pytest.fixture
def gateway():
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    channels = {
        f"{prefix}:perp_mark_ticks": "perp_mark_tick",
        f"{prefix}:perp_liquidations": "perp_liquidation",
        f"{prefix}:perp_funding_events": "perp_funding",
    }
    hub = StreamHub(redis_url=REDIS_URL, perp_channels=channels, perp_mark_key_pattern=f"{prefix}:perp:mark:*")

    @asynccontextmanager
    async def lifespan(app):
        await hub.start()
        yield
        await hub.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.stream_hub = hub
    app.include_router(ws_router)
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    names = {msg_type: name for name, msg_type in channels.items()}
    with TestClient(app) as client:
        yield {"client": client, "redis": r, "channel": names, "key_prefix": f"{prefix}:perp:mark:"}
    for key in r.scan_iter(match=f"{prefix}:*"):
        r.delete(key)
    r.close()


def wait_for_relay(g, timeout=5.0):
    """Block until the hub's perp relay has subscribed to every channel."""
    deadline = time.monotonic() + timeout
    while True:
        counts = dict(g["redis"].pubsub_numsub(*g["channel"].values()))
        if all(counts.get(ch, 0) >= 1 for ch in g["channel"].values()):
            return
        assert time.monotonic() < deadline, "perp relay never subscribed"
        time.sleep(0.02)


def receive_until(ws, predicate, max_messages=20):
    for _ in range(max_messages):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("expected message not received")


def subscribe(ws, **fields):
    ws.send_json({"action": "subscribe", **fields})
    return receive_until(ws, lambda m: "status" in m or "error" in m)


def mark(instrument_id: str, price: str) -> dict:
    return {"type": "perp.mark", "v": 1, "instrument_id": instrument_id, "mark_price": price,
            "index_price": price, "funding_rate": "0.0001", "next_funding_time_ns": 1, "ts_event_ns": 1}


def test_subscribe_ack_and_validation(gateway):
    with gateway["client"].websocket_connect("/ws/stream") as ws:
        ack = subscribe(ws, channels=["perpetuals"], instruments=[BTC])
        assert ack["status"] == "subscribed" and ack["instruments"] == [BTC]
        assert "Unsupported perpetual" in subscribe(ws, channels=["perpetuals"], instruments=["DOGEUSDT-PERP.BINANCE"])["error"]
        assert "require the 'perpetuals' channel" in subscribe(ws, channels=["ticker_update"], instruments=[BTC])["error"]


def test_cached_marks_hydrate_new_subscriber(gateway):
    gateway["redis"].set(gateway["key_prefix"] + "BTCUSDT", json.dumps(mark(BTC, "50000")))
    with gateway["client"].websocket_connect("/ws/stream") as ws:
        ws.send_json({"action": "subscribe", "channels": ["perpetuals"]})
        snap = receive_until(ws, lambda m: m.get("type") == "perp_mark_tick")
    assert snap["snapshot"] is True and snap["data"]["mark_price"] == "50000"


def test_perp_channels_are_framed_and_forwarded(gateway):
    with gateway["client"].websocket_connect("/ws/stream") as ws:
        assert subscribe(ws, channels=["perpetuals"])["status"] == "subscribed"
        wait_for_relay(gateway)
        r, ch = gateway["redis"], gateway["channel"]

        r.publish(ch["perp_mark_tick"], json.dumps(mark(BTC, "51000")))
        tick = receive_until(ws, lambda m: m.get("type") == "perp_mark_tick")
        assert tick == {"type": "perp_mark_tick", "data": mark(BTC, "51000")}

        liquidation = {"type": "perp.liquidation", "v": 1, "instrument_id": BTC, "side": "LONG", "quantity": "0.04"}
        r.publish(ch["perp_liquidation"], json.dumps(liquidation))
        assert receive_until(ws, lambda m: m.get("type") == "perp_liquidation") == {
            "type": "perp_liquidation", "data": liquidation,
        }

        funding = {"type": "perp.funding", "v": 1, "instrument_id": BTC, "cash_flow": "-0.2"}
        r.publish(ch["perp_funding"], json.dumps(funding))
        assert receive_until(ws, lambda m: m.get("type") == "perp_funding") == {"type": "perp_funding", "data": funding}


def test_instrument_filter_applies_to_marks_not_account_events(gateway):
    with gateway["client"].websocket_connect("/ws/stream") as ws:
        subscribe(ws, channels=["perpetuals"], instruments=[BTC])
        wait_for_relay(gateway)
        r, ch = gateway["redis"], gateway["channel"]
        r.publish(ch["perp_mark_tick"], json.dumps(mark(ETH, "3000")))  # filtered out
        r.publish(ch["perp_liquidation"], json.dumps({"instrument_id": ETH, "side": "SHORT"}))  # account event: delivered
        r.publish(ch["perp_mark_tick"], json.dumps(mark(BTC, "52000")))

        first = receive_until(ws, lambda m: m.get("type", "").startswith("perp_"))
        second = receive_until(ws, lambda m: m.get("type", "").startswith("perp_"))
    assert first == {"type": "perp_liquidation", "data": {"instrument_id": ETH, "side": "SHORT"}}
    assert second["type"] == "perp_mark_tick" and second["data"]["instrument_id"] == BTC


# --- Unit: routing and backpressure -----------------------------------------------------------

def test_non_subscribers_and_backpressure():
    hub = StreamHub(redis_url=REDIS_URL)
    client = ClientConnection(ws=None, client_id="c1")
    assert not hub._wants_perp(client, "perp_liquidation", BTC)  # not subscribed to perpetuals

    client.subscribe_perpetuals, client.perp_instruments = True, {BTC}
    assert hub._wants_perp(client, "perp_mark_tick", BTC)
    assert not hub._wants_perp(client, "perp_mark_tick", ETH)
    assert hub._wants_perp(client, "perp_funding", ETH)

    for i in range(client.queue.maxsize):
        client.queue.put_nowait({"type": "closed_bar_committed", "i": i})
    client.enqueue_droppable({"type": "perp_mark_tick"})  # full queue: the tick is dropped, nothing reliable lost
    assert client.queue.qsize() == client.queue.maxsize
    assert all(m["type"] == "closed_bar_committed" for m in list(client.queue._queue))
