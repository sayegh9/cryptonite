"""P1.7 per-profile exit policy regressions (ADR 0011 D13)."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_scanner_service.exit_monitor import ExitAction, Position
from cryptonite_schemas.exit_thresholds import ExitThresholds

DEFAULTS = ExitThresholds(
    stop_loss_pct=5.0,
    trail_activate_pct=3.0,
    trail_pct=2.0,
    debate_drawdown_pct=2.0,
    funding_spike=0.0005,
)


def profile_position(side: str, **overrides) -> Position:
    values = {
        "instrument_id": "BTCUSDT.BINANCE",
        "side": side,
        "entry_price": 100.0,
        "current_price": 118.0 if side == "LONG" else 82.0,
        "ai_owned": True,
        "opened_ts": 1_000,
        "owner_instance": "AI_MANDATE_00000000000000000000000000000001",
        "stop_price": 90.0 if side == "LONG" else 110.0,
        "take_profit_price": None,
        "quantity": 2.0,
        "profile_tp_r": 1.5,
        "profile_tp1_scale_pct": 25.0,
    }
    values.update(overrides)
    return Position(**values)


@pytest.mark.parametrize(
    ("side", "target"),
    [("LONG", 115.0), ("SHORT", 85.0)],
)
def test_profile_take_profit_is_exit_tp_r_times_structural_stop_distance(side, target):
    pos = em.apply_profile_exit_levels(profile_position(side))

    assert pos.take_profit_price == target
    assert em.evaluate_exit(pos, best_price=pos.current_price, debate_armed=False, t=DEFAULTS) == [
        ExitAction("REDUCE_TP1", "TAKE_PROFIT_1")
    ]


def test_profile_tp1_reduction_uses_profile_scale_percentage():
    pos = em.apply_profile_exit_levels(profile_position("LONG"))

    assert em.tp1_reduce_quantity(pos) == "0.5"


def test_profile_trailing_values_override_globals_and_null_values_fall_back():
    pos = profile_position("LONG", current_price=105.0,
                           profile_trail_activate_pct=6.0, profile_trail_pct=1.0)
    profile_thresholds = em.position_thresholds(pos, DEFAULTS)
    assert (profile_thresholds.trail_activate_pct, profile_thresholds.trail_pct) == (6.0, 1.0)
    assert em.evaluate_exit(pos, best_price=105.0, debate_armed=False, t=profile_thresholds) == []
    assert em.evaluate_exit(replace(pos, current_price=105.5), best_price=107.0,
                            debate_armed=False, t=profile_thresholds) == [
        ExitAction("CLOSE", "TRAILING_STOP")
    ]

    fallback = profile_position("LONG", profile_trail_activate_pct=None, profile_trail_pct=None)
    assert em.position_thresholds(fallback, DEFAULTS) == DEFAULTS
    assert em.evaluate_exit(replace(fallback, current_price=101.8), best_price=104.0,
                            debate_armed=False, t=DEFAULTS) == [ExitAction("CLOSE", "TRAILING_STOP")]


def test_profile_max_hold_closes_as_reduce_only_close():
    pos = profile_position("LONG", current_price=101.0, profile_max_hold_hours=2)

    assert em.max_hold_action(pos, now_ts=1_000 + 2 * 3600) == ExitAction("CLOSE", "MAX_HOLD")
    assert em.max_hold_action(pos, now_ts=1_000 + 2 * 3600 - 1) is None
    payload = em.close_payload(pos, "MAX_HOLD")
    assert payload == {"instrument_id": "BTCUSDT.BINANCE", "reason": "MAX_HOLD"}


def test_unowned_and_manual_positions_keep_existing_take_profit_behavior():
    manual = Position("BTCUSDT.BINANCE", "LONG", 100.0, 115.0, False, stop_price=90.0,
                      take_profit_price=110.0, quantity=2.0)
    unchanged = em.apply_profile_exit_levels(manual)

    assert unchanged == manual
    assert em.evaluate_exit(unchanged, best_price=115.0, debate_armed=False, t=DEFAULTS) == [
        ExitAction("ALERT", "TAKE_PROFIT_1")
    ]


def test_no_profile_position_exit_snapshot_is_unchanged():
    pos = Position("BTCUSDT.BINANCE", "LONG", 100.0, 101.0, True, stop_price=95.0,
                   take_profit_price=110.0, quantity=1.0)

    assert em.apply_profile_exit_levels(pos) == pos
    assert em.position_thresholds(pos, DEFAULTS) == DEFAULTS
    assert em.evaluate_exit(pos, best_price=103.0, debate_armed=True, t=DEFAULTS) == []
    assert em.evaluate_exit(replace(pos, current_price=110.0), best_price=110.0,
                            debate_armed=True, t=DEFAULTS) == [ExitAction("REDUCE_TP1", "TAKE_PROFIT_1")]


class ProfileConn:
    def __init__(self):
        self.fetch_calls = 0

    async def fetch(self, query, profile_ids):
        self.fetch_calls += 1
        assert "exit_tp_r" in query
        return [{"id": profile_ids[0], "exit_tp_r": 3, "exit_tp1_scale_pct": 40,
                 "exit_trail_activate_pct": 7, "exit_trail_pct": 2, "exit_max_hold_hours": 12}]


class PositionConn:
    async def fetch(self, query, *args):
        if "FROM ai_mandates" in query:
            return [{"id": args[0][0], "exit_tp_r": 2, "exit_tp1_scale_pct": 25,
                     "exit_trail_activate_pct": None, "exit_trail_pct": None,
                     "exit_max_hold_hours": 4}]
        return [{"instrument_id": "BTCUSDT.BINANCE", "side": "LONG", "owner":
                 "AI_MANDATE_00000000000000000000000000000001", "quantity": 2,
                 "entry_price": 100, "opened_at": datetime.now(UTC), "ai_owned": True,
                 "shared": False, "leverage": 1, "liquidation_price": None,
                 "isolated_margin": None}]

    async def fetchrow(self, *args):
        return None


class EmptyRedis:
    async def get(self, key):
        if key.startswith("exit:plan:"):
            return '{"stop_price": "90", "take_profit_price": "130"}'
        return None


async def test_load_positions_attaches_cached_profile_settings_and_uses_structural_stop():
    em.clear_profile_exit_cache()
    quotes = em.QuoteBook()
    quotes.apply({"instrument_id": "BTCUSDT.BINANCE", "bid_price": "101", "ask_price": "102"})

    positions = await em.load_positions(PositionConn(), EmptyRedis(), quotes)

    assert len(positions) == 1
    assert positions[0].profile_tp_r == 2
    assert positions[0].profile_tp1_scale_pct == 25
    assert positions[0].profile_max_hold_hours == 4
    assert positions[0].stop_price == 90.0
    assert positions[0].take_profit_price == 120.0


async def test_profile_exit_settings_are_cached_for_30_seconds(monkeypatch):
    import uuid

    profile_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    owner = f"AI_MANDATE_{profile_id.hex}"
    conn = ProfileConn()
    em.clear_profile_exit_cache()
    monkeypatch.setattr(em.time, "monotonic", lambda: 10.0)

    first = await em.load_profile_exits(conn, [owner])
    second = await em.load_profile_exits(conn, [owner])
    assert conn.fetch_calls == 1
    assert first == second and first[profile_id].tp_r == 3

    monkeypatch.setattr(em.time, "monotonic", lambda: 40.1)
    await em.load_profile_exits(conn, [owner])
    assert conn.fetch_calls == 2
