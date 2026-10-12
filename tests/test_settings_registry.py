"""The settings registry (ADR 0007), Phase 1: read-only, with drift guards against the code it describes."""

from types import SimpleNamespace

import pytest
from cryptonite_agents.gateway.budget import DEFAULT_DAILY_CAP_USD
from cryptonite_api.routes.settings import get_settings_registry
from cryptonite_api.settings_registry import (
    REGISTRY,
    WRITABLE_TIERS,
    Apply,
    Tier,
    current_values,
    entry_view,
)
from cryptonite_execution.risk_limits import (
    ACCOUNT_BACKSTOP_DRAWDOWN_PCT,
    DEFAULTS,
    MAX_POSITION_PCT_CAP,
    MIN_ORDER_EQUITY_PCT,
)
from cryptonite_orchestrator import ops_monitor
from cryptonite_scanner.models import MAX_ACTIVE_SPECS
from cryptonite_scanner_service import exit_monitor as em


def by_key(key):
    return next(s for s in REGISTRY if s.key == key)


def test_keys_are_unique_and_every_entry_names_its_adr_and_store():
    keys = [s.key for s in REGISTRY]
    assert len(keys) == len(set(keys))
    assert all(s.adr and s.store for s in REGISTRY)


def test_defaults_sit_inside_their_bounds():
    for s in REGISTRY:
        if s.minimum is not None:
            assert s.default >= s.minimum, s.key
        if s.maximum is not None:
            assert s.default <= s.maximum, s.key


def test_only_the_adjustable_tiers_are_writable():
    assert WRITABLE_TIERS == {Tier.T_L, Tier.TOTP, Tier.SESSION}
    assert all(s.writable == (s.tier in WRITABLE_TIERS) for s in REGISTRY)
    assert not any(s.writable for s in REGISTRY if s.tier in {Tier.R, Tier.FIXED, Tier.G})


def test_a_value_read_at_import_never_claims_a_hot_apply():
    # Exit and alert values are read at import today (ADR 0007 notes). They stay `restart` until their readers move.
    for s in REGISTRY:
        if s.store == "env":
            assert s.apply is not Apply.HOT, s.key


def test_risk_defaults_are_the_dispatcher_defaults():
    assert by_key("risk.max_daily_loss_pct").default == DEFAULTS.max_daily_loss_pct
    assert by_key("risk.max_order_equity_pct").default == DEFAULTS.max_order_equity_pct
    assert by_key("risk.cross_mandate_exposure_pct").default == DEFAULTS.cross_mandate_exposure_pct


def test_exit_and_alert_defaults_match_the_services():
    assert by_key("exit.stop_loss_pct").default == em.STOP_LOSS_PCT
    assert by_key("exit.trail_activate_pct").default == em.TRAIL_ACTIVATE_PCT
    assert by_key("exit.trail_pct").default == em.TRAIL_PCT
    assert by_key("exit.debate_drawdown_pct").default == em.DEBATE_DRAWDOWN_PCT
    assert by_key("exit.funding_spike").default == em.FUNDING_SPIKE
    assert by_key("exit.monitor_interval_s").default == em.INTERVAL_S
    assert by_key("alerts.heartbeat_grace_s").default == ops_monitor.HEARTBEAT_GRACE_S
    assert by_key("alerts.sentiment_stale_s").default == ops_monitor.SENTIMENT_STALE_S
    assert by_key("scanner.max_active_specs").default == MAX_ACTIVE_SPECS


def test_budget_default_is_the_budget_guard_default():
    assert by_key("LLM_DAILY_BUDGET_USD").default == DEFAULT_DAILY_CAP_USD


def test_nothing_reports_writable_until_a_write_path_exists():
    out = [entry_view(s, None) for s in REGISTRY]
    assert all(entry["writable"] is False for entry in out)


def test_fixed_guardrails_match_the_risk_module():
    assert by_key("guard.position_clamp_pct").default == MAX_POSITION_PCT_CAP
    assert by_key("guard.account_backstop_pct").default == ACCOUNT_BACKSTOP_DRAWDOWN_PCT
    assert by_key("guard.min_order_pct").default == MIN_ORDER_EQUITY_PCT


def test_entry_view_marks_service_owned_values_as_service_not_api():
    stop = entry_view(by_key("exit.stop_loss_pct"), None)
    assert (stop["value"], stop["value_source"], stop["apply"], stop["writable"]) == (None, "service", "restart", False)
    budget = entry_view(by_key("LLM_DAILY_BUDGET_USD"), "30.00")
    assert (budget["value"], budget["value_source"]) == ("30.00", "api")


def test_current_values_fall_back_to_the_default_budget_and_the_dispatcher_limits():
    values = current_values(None, None)
    assert values["LLM_DAILY_BUDGET_USD"] == "25.00"
    assert float(values["risk.max_daily_loss_pct"]) == 5
    assert float(values["risk.max_order_equity_pct"]) == 25


def test_current_values_apply_a_stored_override_and_keep_the_rest_at_default():
    row = {"max_daily_loss_pct": 3, "max_order_equity_pct": None, "cross_mandate_exposure_pct": None,
           "updated_by": "op", "updated_at": None, "reason": "tighten", "override_version": 1}
    values = current_values("40.00", row)
    assert values["LLM_DAILY_BUDGET_USD"] == "40.00"
    assert float(values["risk.max_daily_loss_pct"]) == 3
    assert float(values["risk.cross_mandate_exposure_pct"]) == 25


class FakeConn:
    def __init__(self, budget, row):
        self.budget, self.row = budget, row

    async def fetchval(self, sql, *args):
        return self.budget

    async def fetchrow(self, sql, *args):
        return self.row


class FakePool:
    def __init__(self, budget, row):
        self.conn = FakeConn(budget, row)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_registry_endpoint_lists_every_setting_with_its_tier_and_value():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(None, None))))
    out = await get_settings_registry(request, auth={})
    assert {s["key"] for s in out["settings"]} == {s.key for s in REGISTRY}
    by = {s["key"]: s for s in out["settings"]}
    assert by["LLM_DAILY_BUDGET_USD"]["value"] == "25.00" and by["LLM_DAILY_BUDGET_USD"]["value_source"] == "api"
    assert by["exit.stop_loss_pct"]["value_source"] == "service"
    assert by["universe.symbols"]["writable"] is False
