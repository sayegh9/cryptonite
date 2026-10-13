"""Operator session and login policy (ADR 0007, Phase 2): read from system_settings, applied without a restart, with
the direction rules the registry uses."""

from decimal import Decimal

import pytest
from cryptonite_api.auth import OperatorAuthManager
from cryptonite_api.auth_policy import AUTH_KEYS, refresh_auth_policy
from cryptonite_api.settings_registry import REGISTRY, write_needs_totp
from cryptonite_schemas.runtime_settings import RUNTIME_BOUNDS


class _Manager:
    def __init__(self):
        self.applied = []

    def apply_policy(self, **values):
        self.applied.append(values)


class _Conn:
    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows or [], error

    async def fetch(self, sql, *args):
        if self.error is not None:
            raise self.error
        assert set(args[0]) == set(AUTH_KEYS)
        return self.rows


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _defaults():
    return {key: RUNTIME_BOUNDS[key].default for key in AUTH_KEYS}


@pytest.mark.asyncio
async def test_stored_values_are_applied_and_missing_ones_take_the_defaults():
    manager = _Manager()
    pool = _Pool(_Conn(rows=[{"key": "auth.idle_timeout_s", "value": '"900"'},
                             {"key": "auth.max_login_attempts", "value": '"3"'}]))
    applied = await refresh_auth_policy(pool, manager, _defaults())
    assert applied["auth.idle_timeout_s"] == 900 and applied["auth.max_login_attempts"] == 3
    assert applied["auth.session_ttl_s"] == 43200  # missing: the default
    assert manager.applied[-1] == {"session_ttl_s": 43200, "idle_timeout_s": 900, "lockout_s": 300, "max_login_attempts": 3}


@pytest.mark.asyncio
async def test_a_refused_value_keeps_the_last_valid_one_and_the_rest_still_apply():
    manager = _Manager()
    last = _defaults() | {"auth.session_ttl_s": 7200.0}
    pool = _Pool(_Conn(rows=[{"key": "auth.session_ttl_s", "value": '"86400"'},  # above the 12 h ceiling
                             {"key": "auth.lockout_s", "value": '"600"'}]))
    applied = await refresh_auth_policy(pool, manager, last)
    assert applied["auth.session_ttl_s"] == 7200.0 and applied["auth.lockout_s"] == 600


@pytest.mark.asyncio
async def test_a_failed_read_keeps_everything_that_was_in_force():
    manager = _Manager()
    last = _defaults() | {"auth.idle_timeout_s": 600.0}
    applied = await refresh_auth_policy(_Pool(_Conn(error=RuntimeError("db down"))), manager, last)
    assert applied == last and manager.applied == []


def test_the_auth_manager_reports_and_takes_its_policy_without_a_restart():
    manager = OperatorAuthManager(db_pool=None, redis_client=None, totp_aes_key=b"x" * 32)
    assert manager.policy == {"session_ttl_s": 43200, "idle_timeout_s": 1800, "lockout_s": 300, "max_login_attempts": 5}
    manager.apply_policy(session_ttl_s=3600, idle_timeout_s=600, lockout_s=900, max_login_attempts=4)
    assert manager.policy == {"session_ttl_s": 3600, "idle_timeout_s": 600, "lockout_s": 900, "max_login_attempts": 4}


def _entry(key):
    return next(s for s in REGISTRY if s.key == key)


def test_session_lengths_can_only_be_shortened_from_their_ceilings():
    ttl = _entry("auth.session_ttl_s")
    assert write_needs_totp(ttl, Decimal("43200"), Decimal("3600")) is False  # shorter: tighter
    assert write_needs_totp(ttl, Decimal("3600"), Decimal("7200")) is True  # longer: looser, needs TOTP


def test_a_longer_failed_login_window_is_the_tighter_one():
    window = _entry("auth.lockout_s")
    assert write_needs_totp(window, Decimal("300"), Decimal("600")) is False  # longer window: tighter
    assert write_needs_totp(window, Decimal("300"), Decimal("60")) is True  # shorter window: looser
    attempts = _entry("auth.max_login_attempts")
    assert write_needs_totp(attempts, Decimal("5"), Decimal("3")) is False  # fewer attempts: tighter
