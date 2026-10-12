"""Runtime settings (ADR 0007): the values the services read every cycle, and the rule each reader follows: a missing
value takes the default, a valid stored value applies, an invalid one keeps the last valid value."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from cryptonite_orchestrator import ops_monitor
from cryptonite_scanner.models import MAX_ACTIVE_SPECS, active_spec_cap
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_schemas.runtime_settings import RUNTIME_BOUNDS, Bound, read_number


def test_a_missing_value_takes_the_default_and_a_valid_one_applies():
    bound = Bound(default=300, low=60, high=3600)
    assert read_number(None, bound, last_good=120) == (300, None)
    assert read_number('"120"', bound, last_good=300) == (120.0, None)
    assert read_number("120", bound, last_good=300) == (120.0, None)  # a bare number is accepted too


def test_an_unparseable_or_out_of_bounds_value_keeps_the_last_valid_one():
    bound = Bound(default=300, low=60, high=3600)
    value, problem = read_number('"banana"', bound, last_good=120)
    assert value == 120 and "not a number" in problem
    for raw in ('"30"', '"3601"', '"nan"', '"inf"'):
        value, problem = read_number(raw, bound, last_good=120)
        assert value == 120 and problem is not None, raw


def test_every_runtime_bound_has_a_sane_default_inside_its_range():
    for key, bound in RUNTIME_BOUNDS.items():
        assert bound.low <= bound.default <= bound.high, key


class _Db:
    def __init__(self, raw=None, error=None):
        self.raw, self.error = raw, error

    async def fetchval(self, sql, *args):
        if self.error is not None:
            raise self.error
        assert "scanner.max_active_specs" in sql
        return self.raw


@pytest.mark.asyncio
async def test_the_spec_cap_is_the_stored_value_or_the_default():
    assert await active_spec_cap(_Db(raw='"20"')) == 20
    assert await active_spec_cap(_Db(raw=None)) == MAX_ACTIVE_SPECS


@pytest.mark.asyncio
async def test_a_refused_or_unreadable_spec_cap_falls_back_to_the_default():
    assert await active_spec_cap(_Db(raw='"500"')) == MAX_ACTIVE_SPECS  # outside 1..50
    assert await active_spec_cap(_Db(raw='"ten"')) == MAX_ACTIVE_SPECS
    assert await active_spec_cap(_Db(error=RuntimeError("no grant"))) == MAX_ACTIVE_SPECS


class _Conn:
    def __init__(self, rows=None, raw=None, error=None):
        self.rows, self.raw, self.error = rows or [], raw, error

    async def fetch(self, sql, *args):
        if self.error is not None:
            raise self.error
        return self.rows

    async def fetchval(self, sql, *args):
        if self.error is not None:
            raise self.error
        return self.raw


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
async def test_the_ops_monitor_reads_the_alert_values_each_pass():
    pool = _Pool(_Conn(rows=[{"key": "alerts.heartbeat_grace_s", "value": '"900"'},
                             {"key": "alerts.sentiment_stale_s", "value": '"7200"'}]))
    assert await ops_monitor.load_alert_settings(pool, (300.0, 86400.0)) == (900.0, 7200.0)


@pytest.mark.asyncio
async def test_the_ops_monitor_keeps_the_last_valid_alert_values_on_failure_or_refusal():
    last = (300.0, 86400.0)
    assert await ops_monitor.load_alert_settings(_Pool(_Conn(error=RuntimeError("db"))), last) == last
    refused = _Pool(_Conn(rows=[{"key": "alerts.heartbeat_grace_s", "value": '"5"'}]))  # below 60
    assert await ops_monitor.load_alert_settings(refused, last) == (300.0, 86400.0)


@pytest.mark.asyncio
async def test_the_exit_monitor_reads_its_interval_each_cycle():
    assert await em.load_interval(_Pool(_Conn(raw='"60"')), last_good=30.0) == 60.0
    assert await em.load_interval(_Pool(_Conn(raw=None)), last_good=30.0) == em.INTERVAL_S
    assert await em.load_interval(_Pool(_Conn(raw='"2"')), last_good=30.0) == 30.0  # below 5
    assert await em.load_interval(_Pool(_Conn(error=RuntimeError("db"))), last_good=45.0) == 45.0


def test_the_bounds_the_readers_use_are_the_registry_bounds():
    assert replace(RUNTIME_BOUNDS["exit.monitor_interval_s"], default=em.INTERVAL_S).low == 5
    assert RUNTIME_BOUNDS["alerts.heartbeat_grace_s"].default == ops_monitor.HEARTBEAT_GRACE_S
