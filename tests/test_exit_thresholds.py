"""Exit thresholds (ADR 0007): the rules a stored set must pass, the last-valid fallback, and what the monitor applies.
Pure and no database, except that the monitor module is imported as the scanner runs it."""

import dataclasses
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from cryptonite_api.settings_registry import heartbeat_exit_values
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_scanner_service.exit_monitor import ExitAction, Position
from cryptonite_schemas.exit_thresholds import SETTING_KEYS, ExitThresholds, settled, violations

DEFAULT = ExitThresholds(stop_loss_pct=5.0, trail_activate_pct=3.0, trail_pct=2.0, debate_drawdown_pct=2.0,
                         funding_spike=0.0005)


def test_the_default_set_is_valid_and_matches_the_monitor_defaults():
    assert violations(DEFAULT) == []
    assert em.default_thresholds().stop_loss_pct == em.STOP_LOSS_PCT
    assert em.default_thresholds().funding_spike == em.FUNDING_SPIKE


def test_each_bound_is_enforced_and_nan_is_refused():
    assert violations(replace(DEFAULT, stop_loss_pct=0.0))
    assert violations(replace(DEFAULT, stop_loss_pct=25.0))
    assert violations(replace(DEFAULT, funding_spike=0.02))
    assert violations(replace(DEFAULT, trail_pct=float("nan")))


def test_give_back_cannot_exceed_its_activation_and_the_debate_must_stay_below_the_stop():
    assert violations(replace(DEFAULT, trail_pct=3.5)) == [
        "exit.trail_pct must not exceed exit.trail_activate_pct"]
    assert violations(replace(DEFAULT, debate_drawdown_pct=5.0)) == [
        "exit.debate_drawdown_pct must be below exit.stop_loss_pct"]
    assert violations(replace(DEFAULT, debate_drawdown_pct=4.9)) == []


def test_missing_keys_take_the_defaults_and_stored_text_is_parsed():
    applied, problems = settled({"exit.stop_loss_pct": '"4.00"'}, DEFAULT, last_good=DEFAULT)
    assert problems == [] and applied.stop_loss_pct == 4.0 and applied.trail_pct == 2.0


def test_a_bare_number_is_accepted_too():
    applied, problems = settled({"exit.stop_loss_pct": 4}, DEFAULT, last_good=DEFAULT)
    assert problems == [] and applied.stop_loss_pct == 4.0


def test_a_value_that_does_not_parse_keeps_the_last_valid_set():
    last = replace(DEFAULT, stop_loss_pct=4.5)
    applied, problems = settled({"exit.stop_loss_pct": '"banana"'}, DEFAULT, last_good=last)
    assert applied == last and len(problems) == 1 and "not a number" in problems[0]


def test_a_stored_set_that_breaks_a_rule_is_refused_as_a_whole():
    last = replace(DEFAULT, stop_loss_pct=4.5)
    applied, problems = settled({"exit.stop_loss_pct": '"3.00"', "exit.trail_pct": '"4.00"'}, DEFAULT, last_good=last)
    assert applied == last  # the valid stop change is refused with the rest: one set, one decision
    assert problems == ["exit.trail_pct must not exceed exit.trail_activate_pct"]


def test_the_heartbeat_round_trips_through_the_registry_view():
    payload = json.dumps(dataclasses.asdict(DEFAULT))
    assert set(dataclasses.asdict(DEFAULT)) == set(SETTING_KEYS.values())
    assert heartbeat_exit_values(payload) == {
        "exit.stop_loss_pct": "5", "exit.trail_activate_pct": "3", "exit.trail_pct": "2",
        "exit.debate_drawdown_pct": "2", "exit.funding_spike": "0.0005"}
    assert heartbeat_exit_values(None) == {} and heartbeat_exit_values("not json") == {}


def test_evaluate_exit_applies_the_thresholds_it_is_given():
    pos = Position(instrument_id="BTCUSDT.BINANCE", side="LONG", entry_price=100.0, current_price=95.5, ai_owned=True)
    assert em.evaluate_exit(pos, 100.0, debate_armed=False) == []  # the default hard stop is 5%
    tight = replace(DEFAULT, stop_loss_pct=4.0)
    assert em.evaluate_exit(pos, 100.0, debate_armed=False, t=tight) == [ExitAction("CLOSE", "HARD_STOP")]


def test_evaluate_exit_applies_the_funding_threshold_it_is_given():
    pos = Position(instrument_id="BTCUSDT.BINANCE", side="LONG", entry_price=100.0, current_price=100.0,
                   ai_owned=True, funding_rate=0.0003)
    assert em.evaluate_exit(pos, 100.0, debate_armed=True) == []  # below the default 0.05%
    low = replace(DEFAULT, funding_spike=0.0002)
    assert em.evaluate_exit(pos, 100.0, debate_armed=True, t=low) == [ExitAction("EXIT_DEBATE", "FUNDING_SPIKE")]


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.asked = None

    async def fetch(self, sql, *args):
        assert "system_settings" in sql
        self.asked = set(args[0])
        return self.rows


class _Pool:
    def __init__(self, rows=None, error=None):
        self.conn, self.error = _Conn(rows or []), error

    @asynccontextmanager
    async def acquire(self):
        if self.error is not None:
            raise self.error
        yield self.conn


@pytest.mark.asyncio
async def test_each_cycle_reads_the_stored_set_for_the_five_keys():
    pool = _Pool(rows=[{"key": "exit.stop_loss_pct", "value": '"4.00"'}])
    applied = await em.load_thresholds(pool, last_good=DEFAULT)
    assert applied.stop_loss_pct == 4.0
    assert pool.conn.asked == set(SETTING_KEYS)


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_last_valid_set():
    last = replace(DEFAULT, stop_loss_pct=4.5)
    assert await em.load_thresholds(_Pool(error=RuntimeError("db down")), last_good=last) == last


@pytest.mark.asyncio
async def test_a_refused_stored_set_keeps_the_last_valid_set():
    last = replace(DEFAULT, stop_loss_pct=4.5)
    pool = _Pool(rows=[{"key": "exit.trail_pct", "value": '"4.00"'}])
    assert await em.load_thresholds(pool, last_good=last) == last
