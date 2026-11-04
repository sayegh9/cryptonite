"""The position gauge shows what the exit monitor enforces: the monitor publishes its levels each cycle and the
positions API returns exactly those, never an estimate from fixed percentages."""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_api.main import create_app
from cryptonite_api.routes.auth import require_auth, require_operator
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_schemas.exit_rules import (
    DEFAULT_THRESHOLDS,
    MANUAL_LEVELS_OWNER,
    Position,
    apply_profile_exit_levels,
    enforced_levels,
    evaluate_exit,
    exit_levels_key,
    position_thresholds,
)
from fastapi.testclient import TestClient

ETH = "ETHUSDT.BINANCE"
OWNER = "AI_MANDATE_abc"


def eth_long(price: float = 2446.07, stop: float | None = 2285.29) -> Position:
    """The 2026-11-04 ETH trade: entry 2317.04, structural stop 2285.29, 2R / 50% TP1, trail 3% / 2%."""
    return apply_profile_exit_levels(Position(
        ETH, "LONG", 2317.04, price, True, 1_793_795_118, owner_instance=OWNER, stop_price=stop, quantity=0.21579,
        profile_tp_r=Decimal("2"), profile_tp1_scale_pct=Decimal("50"),
        profile_trail_activate_pct=Decimal("3"), profile_trail_pct=Decimal("2")))


def test_levels_after_tp1_are_the_structural_stop_the_trail_and_a_done_tp1():
    pos = eth_long()
    levels = enforced_levels(pos, 2445.65, position_thresholds(pos, DEFAULT_THRESHOLDS), True)
    assert levels["stop_price"] == 2285.29
    assert levels["trailing_stop_price"] == pytest.approx(2445.65 * 0.98)
    assert levels["tp1_price"] == pytest.approx(2317.04 + 2 * (2317.04 - 2285.29))
    assert levels["tp1_scale_pct"] == 50.0
    assert levels["tp1_done"] is True
    assert levels["alert_only"] is False


def test_trail_is_absent_until_active_and_a_short_mirrors():
    pos = eth_long(price=2320.0)
    assert enforced_levels(pos, 2320.0, position_thresholds(pos, DEFAULT_THRESHOLDS), False)["trailing_stop_price"] is None
    short = apply_profile_exit_levels(Position(
        "BTCUSDT-PERP.BINANCE", "SHORT", 77291.3, 75000.0, True, 1, owner_instance=OWNER, stop_price=77886.86,
        profile_tp_r=Decimal("2")))
    levels = enforced_levels(short, 74000.0, DEFAULT_THRESHOLDS, False)
    assert levels["stop_price"] == 77886.86
    assert levels["trailing_stop_price"] == pytest.approx(74000.0 * 1.02)
    assert levels["tp1_price"] == pytest.approx(77291.3 - 2 * (77886.86 - 77291.3))


def test_no_structural_stop_shows_the_hard_stop():
    pos = eth_long(price=2320.0, stop=None)
    levels = enforced_levels(pos, 2320.0, DEFAULT_THRESHOLDS, False)
    assert levels["stop_price"] == pytest.approx(2317.04 * 0.95)
    assert levels["tp1_price"] is None


def test_published_levels_are_where_evaluate_exit_closes():
    """A price at the published stop or trail closes the position; just inside it does not."""
    pos = eth_long()
    t = position_thresholds(pos, DEFAULT_THRESHOLDS)
    best = 2445.65
    levels = enforced_levels(pos, best, t, True)
    nearest = max(levels["stop_price"], levels["trailing_stop_price"])
    at = evaluate_exit(eth_long(price=nearest), best, False, True, t, tp1_done=True)
    inside = evaluate_exit(eth_long(price=nearest + 1), best, False, True, t, tp1_done=True)
    assert [a.kind for a in at] == ["CLOSE"]
    assert inside == []


def test_monitor_publishes_with_a_ttl_and_a_redis_failure_never_stops_a_check():
    pos = eth_long()
    redis = MagicMock()
    redis.set = AsyncMock()
    asyncio.run(em.publish_levels(redis, pos, 2445.65, DEFAULT_THRESHOLDS, True))
    args, kwargs = redis.set.await_args
    assert args[0] == exit_levels_key(ETH, "LONG", OWNER)
    assert json.loads(args[1])["tp1_done"] is True
    assert kwargs["ex"] == em.EXIT_LEVELS_TTL_S
    redis.set = AsyncMock(side_effect=ConnectionError("down"))
    asyncio.run(em.publish_levels(redis, pos, 2445.65, DEFAULT_THRESHOLDS, True))


def test_a_manual_position_publishes_under_the_alert_only_key():
    pos = Position(ETH, "LONG", 2317.04, 2320.0, False, 1)
    redis = MagicMock()
    redis.set = AsyncMock()
    asyncio.run(em.publish_levels(redis, pos, 2320.0, DEFAULT_THRESHOLDS, False))
    assert redis.set.await_args.args[0] == exit_levels_key(ETH, "LONG", MANUAL_LEVELS_OWNER)
    assert json.loads(redis.set.await_args.args[1])["alert_only"] is True


def api(store: dict[str, str] | None, status: str = "OPEN"):
    app = create_app()
    row = {"position_id": "P1", "account_id": "A", "instance_id": OWNER, "instrument_id": ETH, "side": "LONG",
           "entry_price": 2317.04, "quantity": 0.21579, "current_price": 2446.07, "unrealized_pnl": 27.84,
           "realized_pnl": 13.4, "status": status, "opened_at": "2026-11-04T12:25:18Z", "closed_at": None,
           "strategy_name": "AIMandate"}
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[row])
    app.state.pool = pool
    redis = None
    if store is not None:
        redis = MagicMock()
        redis.hgetall = AsyncMock(return_value={})
        redis.mget = AsyncMock(side_effect=lambda keys: [store.get(k) for k in keys])
    app.state.stream_hub = SimpleNamespace(redis=redis)
    app.dependency_overrides[require_auth] = lambda: {"type": "session", "role": "operator"}
    app.dependency_overrides[require_operator] = lambda: {"type": "session", "role": "operator"}
    res = TestClient(app).get("/api/v1/paper/positions")
    assert res.status_code == 200
    return res.json()[0]


LEVELS = {"stop_price": 2285.29, "trailing_stop_price": 2396.74, "tp1_price": 2380.54, "tp1_scale_pct": 50.0,
          "tp1_done": True, "alert_only": False}


def test_api_returns_the_monitors_levels():
    p = api({exit_levels_key(ETH, "LONG", OWNER): json.dumps(LEVELS)})
    assert p["stop_loss_price"] == 2285.29
    assert p["trailing_stop_price"] == 2396.74
    assert p["take_profit_price"] == 2380.54
    assert p["tp1_done"] is True
    assert p["exit_levels_known"] is True


def test_api_falls_back_to_the_alert_only_key():
    levels = {**LEVELS, "alert_only": True}
    p = api({exit_levels_key(ETH, "LONG", MANUAL_LEVELS_OWNER): json.dumps(levels)})
    assert p["exit_levels_alert_only"] is True
    assert p["stop_loss_price"] == 2285.29


@pytest.mark.parametrize("store", [None, {}])
def test_unknown_levels_are_null_not_an_estimate(store):
    p = api(store)
    assert p["exit_levels_known"] is False
    assert p["stop_loss_price"] is None
    assert p["take_profit_price"] is None


def test_a_closed_position_reads_no_levels():
    p = api({exit_levels_key(ETH, "LONG", OWNER): json.dumps(LEVELS)}, status="CLOSED")
    assert p["stop_loss_price"] is None
    assert "exit_levels_known" not in p
