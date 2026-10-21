"""The settings registry (ADR 0007), Phase 1: the read endpoint, the LLM budget write with its tier rule, and drift
guards against the code the registry describes."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptonite_agents.gateway.budget import DEFAULT_DAILY_CAP_USD
from cryptonite_api.routes import settings as settings_route
from cryptonite_api.routes.settings import (
    RegistryWriteRequest,
    ShadowPaidUpdateRequest,
    get_environment_health,
    get_settings_audit,
    get_settings_registry,
    get_shadow_allow_paid,
    update_registry_setting,
    update_shadow_allow_paid,
)
from cryptonite_api.settings_registry import (
    REGISTRY,
    WRITABLE_TIERS,
    WRITE_PATHS,
    Apply,
    Tier,
    WriteRefused,
    current_values,
    entry_view,
    health_warnings,
    validate_value,
    write_needs_totp,
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
from cryptonite_schemas.exit_thresholds import SETTING_KEYS
from cryptonite_schemas.runtime_settings import RUNTIME_BOUNDS
from fastapi import HTTPException


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


def test_only_keys_with_a_write_path_report_writable():
    assert WRITE_PATHS == {"LLM_DAILY_BUDGET_USD", *SETTING_KEYS, *RUNTIME_BOUNDS}
    out = {s.key: entry_view(s, None)["writable"] for s in REGISTRY}
    assert {key for key, writable in out.items() if writable} == WRITE_PATHS


def test_lowering_the_budget_is_free_and_raising_it_needs_totp():
    budget = by_key("LLM_DAILY_BUDGET_USD")
    assert write_needs_totp(budget, Decimal("25.00"), Decimal("10.00")) is False
    assert write_needs_totp(budget, Decimal("25.00"), Decimal("30.00")) is True


def test_unchanged_budget_needs_no_totp():
    budget = by_key("LLM_DAILY_BUDGET_USD")
    assert write_needs_totp(budget, Decimal("25.00"), Decimal("25.00")) is False


def test_tier_rules_for_a_write_path_are_applied_by_tier():
    budget = by_key("LLM_DAILY_BUDGET_USD")
    assert write_needs_totp(replace(budget, tier=Tier.TOTP), Decimal("1"), Decimal("1")) is True
    assert write_needs_totp(replace(budget, tier=Tier.SESSION), Decimal("1"), Decimal("9")) is False


def test_keys_without_a_write_path_or_a_writable_tier_are_refused():
    with pytest.raises(WriteRefused):
        write_needs_totp(by_key("alerts.routing"), Decimal("1"), Decimal("2"))  # read-only: no write path
    with pytest.raises(WriteRefused):
        write_needs_totp(by_key("guard.account_backstop_pct"), Decimal("20"), Decimal("19"))
    with pytest.raises(WriteRefused):
        write_needs_totp(replace(by_key("LLM_DAILY_BUDGET_USD"), key="universe.symbols"), Decimal("1"), Decimal("2"))


def test_budget_floor_is_above_zero_and_rejects_non_finite_values():
    budget = by_key("LLM_DAILY_BUDGET_USD")
    assert budget.minimum == Decimal("1.00")  # 0 would stop every non-critical AI call
    validate_value(budget, Decimal("1.00"))
    validate_value(budget, Decimal("500.00"))
    for bad in (Decimal("0"), Decimal("0.99"), Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            validate_value(budget, bad)


def test_fixed_guardrails_match_the_risk_module():
    assert by_key("guard.position_clamp_pct").default == MAX_POSITION_PCT_CAP
    assert by_key("guard.account_backstop_pct").default == ACCOUNT_BACKSTOP_DRAWDOWN_PCT
    assert by_key("guard.min_order_pct").default == MIN_ORDER_EQUITY_PCT


def test_entry_view_marks_service_owned_values_as_service_not_api():
    symbols = entry_view(by_key("universe.symbols"), None)
    assert (symbols["value"], symbols["value_source"], symbols["apply"], symbols["writable"]) == (None, "service", "restart", False)
    budget = entry_view(by_key("LLM_DAILY_BUDGET_USD"), "30.00")
    assert (budget["value"], budget["value_source"]) == ("30.00", "api")


def test_a_key_with_no_stored_value_says_the_default_applies():
    stop = entry_view(by_key("exit.stop_loss_pct"), None, "5")
    assert (stop["value"], stop["value_source"], stop["effective"], stop["writable"]) == (None, "default", "5", True)
    assert stop["apply"] == "hot" and stop["store"] == "system_settings"


def test_current_values_are_none_until_stored_except_the_risk_overrides():
    values = current_values({}, None)
    assert values["LLM_DAILY_BUDGET_USD"] is None
    assert values["exit.stop_loss_pct"] is None
    assert float(values["risk.max_daily_loss_pct"]) == 5
    assert float(values["risk.max_order_equity_pct"]) == 25


def test_current_values_apply_a_stored_override_and_keep_the_rest_at_default():
    row = {"max_daily_loss_pct": 3, "max_order_equity_pct": None, "cross_mandate_exposure_pct": None,
           "updated_by": "op", "updated_at": None, "reason": "tighten", "override_version": 1}
    values = current_values({"LLM_DAILY_BUDGET_USD": '"40.00"', "exit.stop_loss_pct": '"4.00"'}, row)
    assert values["LLM_DAILY_BUDGET_USD"] == "40.00"
    assert values["exit.stop_loss_pct"] == "4.00"
    assert float(values["risk.max_daily_loss_pct"]) == 3
    assert float(values["risk.cross_mandate_exposure_pct"]) == 25


class FakeConn:
    def __init__(self, stored, row):
        self.stored, self.row = stored, row

    async def fetch(self, sql, *args):
        return [{"key": k, "value": v} for k, v in self.stored.items() if k in args[0]]

    async def fetchrow(self, sql, *args):
        return self.row

    async def fetchval(self, sql, *args):
        for k, v in self.stored.items():
            if k in sql or (args and k in args):
                return v
        return None


class FakePool:
    def __init__(self, stored, row):
        self.conn = FakeConn(stored, row)

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
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool({}, None))))
    out = await get_settings_registry(request, auth={})
    assert {s["key"] for s in out["settings"]} == {s.key for s in REGISTRY}
    by = {s["key"]: s for s in out["settings"]}
    assert by["LLM_DAILY_BUDGET_USD"]["value"] is None and by["LLM_DAILY_BUDGET_USD"]["value_source"] == "default"
    assert by["exit.stop_loss_pct"]["value_source"] == "default" and by["exit.stop_loss_pct"]["effective"] is None
    assert by["exit.monitor_interval_s"]["value_source"] == "default" and by["exit.monitor_interval_s"]["writable"] is True
    assert by["universe.symbols"]["writable"] is False


class FakeRedis:
    def __init__(self, raw):
        self.raw = raw

    async def get(self, key):
        return self.raw


@pytest.mark.asyncio
async def test_registry_endpoint_shows_what_the_exit_monitor_applies():
    stored = {"exit.stop_loss_pct": '"4.00"', "LLM_DAILY_BUDGET_USD": '"20.00"'}
    heartbeat = ('{"stop_loss_pct": 4.0, "trail_activate_pct": 3.0, "trail_pct": 2.0, '
                 '"debate_drawdown_pct": 2.0, "funding_spike": 0.0005}')
    hub = SimpleNamespace(redis=FakeRedis(heartbeat))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(stored, None), stream_hub=hub)))
    out = await get_settings_registry(request, auth={})
    by = {s["key"]: s for s in out["settings"]}
    assert (by["exit.stop_loss_pct"]["value"], by["exit.stop_loss_pct"]["effective"]) == ("4.00", "4")
    assert by["exit.funding_spike"]["effective"] == "0.0005"
    assert by["LLM_DAILY_BUDGET_USD"]["value"] == "20.00"


class WriteConn:
    """Records what the write path executes. ``stored`` maps system_settings keys to the values the read returns."""

    def __init__(self, stored):
        self.stored, self.executed = stored, []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetch(self, sql, *args):
        return [{"key": k, "value": v} for k, v in self.stored.items() if k in args[0]]

    async def fetchval(self, sql, *args):
        for k, v in self.stored.items():
            if k in sql or (args and k in args):
                return v
        return None

    def transaction(self):
        return _Ctx()


class _Ctx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class WritePool:
    def __init__(self, stored):
        self.conn = WriteConn(stored)

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()

    def inserts(self):
        return [args for sql, args in self.conn.executed if "INSERT INTO system_settings" in sql]


class FakeAuthMgr:
    def __init__(self):
        self.events = []

    async def log_audit_event(self, **event):
        self.events.append(event)


OPERATOR = {"username": "op", "user_id": "00000000-0000-0000-0000-000000000001", "role": "operator", "csrf_token": "tok"}


async def put(key, value, *, pool, auth_mgr, totp=None, csrf="tok"):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)), client=SimpleNamespace(host="127.0.0.1"))
    body = RegistryWriteRequest(value=Decimal(value), reason="budget change for the test", totp_code=totp)
    return await update_registry_setting(key, body, request, auth=OPERATOR, auth_mgr=auth_mgr, x_csrf_token=csrf)


@pytest.mark.asyncio
async def test_lowering_the_budget_writes_without_totp_and_is_audited(monkeypatch):
    async def no_totp(*args, **kwargs):
        raise AssertionError("lowering must not ask for TOTP")

    monkeypatch.setattr(settings_route, "require_totp", no_totp)
    pool, auth_mgr = WritePool({}), FakeAuthMgr()  # no row: the default 25.00 is current
    out = await put("LLM_DAILY_BUDGET_USD", "10", pool=pool, auth_mgr=auth_mgr)
    assert out == {"success": True, "key": "LLM_DAILY_BUDGET_USD", "changed": True, "previous": "25.00",
                   "value": "10.00", "totp_verified": False, "applies_within_s": 10}
    assert pool.inserts() == [("LLM_DAILY_BUDGET_USD", '"10.00"')]  # JSON string, which parse_cap reads
    assert [e["action"] for e in auth_mgr.events] == ["SETTING_UPDATE"]
    assert auth_mgr.events[0]["details"]["before"] == "25.00" and auth_mgr.events[0]["details"]["after"] == "10.00"


@pytest.mark.asyncio
async def test_raising_the_budget_without_totp_is_refused_and_writes_nothing(monkeypatch):
    async def missing_totp(request, auth, code):
        raise HTTPException(status_code=401, detail="TOTP_REQUIRED")

    monkeypatch.setattr(settings_route, "require_totp", missing_totp)
    pool, auth_mgr = WritePool({"LLM_DAILY_BUDGET_USD": '"25.00"'}), FakeAuthMgr()
    with pytest.raises(HTTPException) as refused:
        await put("LLM_DAILY_BUDGET_USD", "30", pool=pool, auth_mgr=auth_mgr)
    assert refused.value.status_code == 401 and "TOTP_REQUIRED" in refused.value.detail
    assert pool.inserts() == []
    assert [e["action"] for e in auth_mgr.events] == ["SETTING_UPDATE_FAILED_AUTH"]


@pytest.mark.asyncio
async def test_raising_the_budget_with_totp_writes_and_records_it(monkeypatch):
    seen = {}

    async def good_totp(request, auth, code):
        seen["code"] = code

    monkeypatch.setattr(settings_route, "require_totp", good_totp)
    pool, auth_mgr = WritePool({"LLM_DAILY_BUDGET_USD": '"25.00"'}), FakeAuthMgr()
    out = await put("LLM_DAILY_BUDGET_USD", "30", pool=pool, auth_mgr=auth_mgr, totp="123456")
    assert seen["code"] == "123456"
    assert out["changed"] is True and out["totp_verified"] is True and out["value"] == "30.00"
    assert pool.inserts() == [("LLM_DAILY_BUDGET_USD", '"30.00"')]
    assert auth_mgr.events[0]["action"] == "SETTING_UPDATE" and auth_mgr.events[0]["details"]["totp_verified"] is True


@pytest.mark.asyncio
async def test_unchanged_budget_writes_nothing():
    pool, auth_mgr = WritePool({"LLM_DAILY_BUDGET_USD": '"25.00"'}), FakeAuthMgr()
    out = await put("LLM_DAILY_BUDGET_USD", "25", pool=pool, auth_mgr=auth_mgr)
    assert out["changed"] is False and pool.inserts() == [] and auth_mgr.events == []


@pytest.mark.asyncio
async def test_refusals_change_nothing():
    cases = [
        ("LLM_DAILY_BUDGET_USD", "10", "wrong-token", 403),   # CSRF
        ("LLM_DAILY_BUDGET_USD", "-1", "tok", 422),           # below the floor
        ("LLM_DAILY_BUDGET_USD", "0", "tok", 422),            # zero would pause all non-critical AI
        ("universe.symbols", "1", "tok", 403),                # read-only (service-owned)
        ("alerts.routing", "1", "tok", 403),                  # read-only (code-owned)
        ("guard.position_clamp_pct", "30", "tok", 403),       # fixed guardrail
        ("no.such.key", "1", "tok", 404),                     # unknown
    ]
    for key, value, csrf, code in cases:
        pool, auth_mgr = WritePool({"LLM_DAILY_BUDGET_USD": '"25.00"'}), FakeAuthMgr()
        with pytest.raises(HTTPException) as refused:
            await put(key, value, pool=pool, auth_mgr=auth_mgr, csrf=csrf)
        assert refused.value.status_code == code, key
        assert pool.inserts() == [] and auth_mgr.events == [], key


# --- health panel: the flags the gates read, and the warnings they produce -------------------------------------

def _codes(warnings):
    return [w["code"] for w in warnings]


def test_health_warnings_name_each_risky_flag_once():
    assert _codes(health_warnings(environment="DEMO", dev_totp_login=True, dev_totp_approvals=True,
                                  secure_cookie=False, kill_switch_active=True)) == [
        "DEV_TOTP_APPROVALS", "DEV_TOTP_LOGIN", "KILL_SWITCH_LATCHED"]  # PROD-only cookie warning is absent outside PROD


def test_insecure_cookie_is_a_warning_only_in_prod():
    assert _codes(health_warnings(environment="PROD", dev_totp_login=False, dev_totp_approvals=False,
                                  secure_cookie=False, kill_switch_active=False)) == ["INSECURE_COOKIE"]
    assert _codes(health_warnings(environment="DEMO", dev_totp_login=False, dev_totp_approvals=False,
                                  secure_cookie=False, kill_switch_active=False)) == []


def test_approval_dev_code_is_critical():
    (warning,) = health_warnings(environment="DEMO", dev_totp_login=False, dev_totp_approvals=True,
                                 secure_cookie=True, kill_switch_active=False)
    assert warning["code"] == "DEV_TOTP_APPROVALS" and warning["severity"] == "critical"


@pytest.mark.asyncio
async def test_environment_flags_follow_the_gate_env_vars(monkeypatch, tmp_path):
    from cryptonite_api.routes.settings import get_environment_health

    marker = tmp_path / "kill.marker"
    marker.write_text("latched")
    monkeypatch.setenv("EMERGENCY_MARKER_PATH", str(marker))

    # Defaults on an unconfigured host: the login accepts 000000 (ALLOW_DEV_TOTP defaults on, ENVIRONMENT is not PROD)
    for var in ("ENVIRONMENT", "ALLOW_DEV_TOTP", "AI_ALLOW_DEV_TOTP", "SECURE_COOKIE"):
        monkeypatch.delenv(var, raising=False)
    health = await get_environment_health(auth={})
    assert health["dev_totp_login"] is True and health["dev_totp_approvals"] is False and health["secure_cookie"] is False
    assert "DEV_TOTP_LOGIN" in _codes(health["warnings"]) and "KILL_SWITCH_LATCHED" in _codes(health["warnings"])

    # PROD refuses the login code whatever ALLOW_DEV_TOTP says, and an insecure cookie is then a warning
    monkeypatch.setenv("ENVIRONMENT", "PROD")
    monkeypatch.setenv("ALLOW_DEV_TOTP", "true")
    health = await get_environment_health(auth={})
    assert health["dev_totp_login"] is False
    assert _codes(health["warnings"]) == ["INSECURE_COOKIE", "KILL_SWITCH_LATCHED"]

    # A configured PROD host with nothing risky on: no warnings
    marker.unlink()
    monkeypatch.setenv("ALLOW_DEV_TOTP", "false")
    monkeypatch.setenv("SECURE_COOKIE", "true")
    assert (await get_environment_health(auth={}))["warnings"] == []


# --- exit thresholds (ADR 0007): stored, read every cycle, rules checked as a set ----------------------------------

def test_exit_lowering_is_free_and_raising_needs_totp():
    stop = by_key("exit.stop_loss_pct")
    assert write_needs_totp(stop, Decimal("5.00"), Decimal("4.00")) is False
    assert write_needs_totp(stop, Decimal("5.00"), Decimal("6.00")) is True


@pytest.mark.asyncio
async def test_exit_lowering_writes_without_totp(monkeypatch):
    async def no_totp(*args, **kwargs):
        raise AssertionError("lowering an exit threshold must not ask for TOTP")

    monkeypatch.setattr(settings_route, "require_totp", no_totp)
    pool, auth_mgr = WritePool({}), FakeAuthMgr()
    out = await put("exit.stop_loss_pct", "4", pool=pool, auth_mgr=auth_mgr)
    assert out["changed"] is True and out["previous"] == "5.00" and out["value"] == "4.00"
    assert pool.inserts() == [("exit.stop_loss_pct", '"4.00"')]


@pytest.mark.asyncio
async def test_exit_raising_without_totp_writes_nothing(monkeypatch):
    async def missing_totp(request, auth, code):
        raise HTTPException(status_code=401, detail="TOTP_REQUIRED")

    monkeypatch.setattr(settings_route, "require_totp", missing_totp)
    pool, auth_mgr = WritePool({}), FakeAuthMgr()
    with pytest.raises(HTTPException) as refused:
        await put("exit.trail_activate_pct", "4", pool=pool, auth_mgr=auth_mgr)
    assert refused.value.status_code == 401 and pool.inserts() == []


@pytest.mark.asyncio
async def test_exit_rules_are_checked_as_a_set_before_any_totp_question(monkeypatch):
    async def must_not_be_asked(*args, **kwargs):
        raise AssertionError("a rule breach is refused before the TOTP question")

    monkeypatch.setattr(settings_route, "require_totp", must_not_be_asked)
    pool, auth_mgr = WritePool({}), FakeAuthMgr()
    with pytest.raises(HTTPException) as refused:  # give-back 4 above the default activation 3
        await put("exit.trail_pct", "4", pool=pool, auth_mgr=auth_mgr)
    assert refused.value.status_code == 422
    assert refused.value.detail[0]["msg"] == "exit.trail_pct must not exceed exit.trail_activate_pct"
    with pytest.raises(HTTPException) as refused:  # the exit debate at the hard stop, not below it
        await put("exit.debate_drawdown_pct", "6", pool=pool, auth_mgr=auth_mgr)
    assert refused.value.status_code == 422
    assert pool.inserts() == [] and auth_mgr.events == []


@pytest.mark.asyncio
async def test_exit_rules_use_the_stored_partner_values(monkeypatch):
    async def lowering_is_free(*args, **kwargs):
        return None

    monkeypatch.setattr(settings_route, "require_totp", lowering_is_free)
    pool = WritePool({"exit.trail_activate_pct": '"1.00"'})  # the partner is stored at 1, not the default 3
    with pytest.raises(HTTPException) as refused:
        await put("exit.trail_pct", "1.50", pool=pool, auth_mgr=FakeAuthMgr())  # lower than 2, but above 1
    assert refused.value.status_code == 422 and pool.inserts() == []


@pytest.mark.asyncio
async def test_funding_spike_takes_four_decimals_and_no_more():
    pool, auth_mgr = WritePool({}), FakeAuthMgr()
    with pytest.raises(HTTPException) as refused:
        await put("exit.funding_spike", "0.00051", pool=pool, auth_mgr=auth_mgr)
    assert refused.value.status_code == 422 and "4 decimal places" in refused.value.detail[0]["msg"]
    out = await put("exit.funding_spike", "0.0004", pool=pool, auth_mgr=auth_mgr)  # lowering: free
    assert out["value"] == "0.0004" and pool.inserts() == [("exit.funding_spike", '"0.0004"')]


# --- runtime settings (ADR 0007): the values the services read every cycle -------------------------------------

def test_runtime_bounds_are_the_registry_bounds_and_are_whole_numbers():
    for key, bound in RUNTIME_BOUNDS.items():
        entry = by_key(key)
        # the slippage allowance (ADR 0009) is the one runtime value in % with two decimals
        whole = 2 if key == "leverage.slippage_allowance_pct" else 0
        assert entry.store == "system_settings" and entry.apply is Apply.HOT and entry.decimals == whole, key
        assert (float(entry.minimum), float(entry.maximum)) == (bound.low, bound.high), key


def test_heartbeat_grace_lower_is_free_and_raising_needs_totp():
    grace = by_key("alerts.heartbeat_grace_s")
    assert write_needs_totp(grace, Decimal("300"), Decimal("120")) is False
    assert write_needs_totp(grace, Decimal("300"), Decimal("900")) is True


def test_session_runtime_values_need_no_totp():
    for key in ("exit.monitor_interval_s", "alerts.sentiment_stale_s", "scanner.max_active_specs"):
        assert write_needs_totp(by_key(key), Decimal("10"), Decimal("20")) is False, key


def test_whole_number_values_refuse_decimals_and_bounds():
    grace = by_key("alerts.heartbeat_grace_s")
    validate_value(grace, Decimal("120"))
    for bad in (Decimal("300.5"), Decimal("59"), Decimal("3601")):
        with pytest.raises(ValueError):
            validate_value(grace, bad)


@pytest.mark.asyncio
async def test_a_runtime_value_is_written_as_a_whole_number_json_string(monkeypatch):
    async def no_totp(*args, **kwargs):
        raise AssertionError("lowering the grace must not ask for TOTP")

    monkeypatch.setattr(settings_route, "require_totp", no_totp)
    pool, auth_mgr = WritePool({"alerts.heartbeat_grace_s": '"300"'}), FakeAuthMgr()
    out = await put("alerts.heartbeat_grace_s", "120", pool=pool, auth_mgr=auth_mgr)
    assert out["previous"] == "300" and out["value"] == "120"
    assert pool.inserts() == [("alerts.heartbeat_grace_s", '"120"')]


# --- tighter direction, shadow-paid toggle, and audit trail ----------------------------------------------------

def test_entry_view_includes_tighter():
    budget = entry_view(by_key("LLM_DAILY_BUDGET_USD"), "25.00")
    assert budget["tighter"] == "lower"
    lockout = entry_view(by_key("auth.lockout_s"), "900")
    assert lockout["tighter"] == "higher"
    stop_loss = entry_view(by_key("exit.stop_loss_pct"), "5")
    assert stop_loss["tighter"] == "lower"


@pytest.mark.asyncio
async def test_environment_health_includes_shadow_allow_paid():
    # Without request / pool, defaults to False
    health_no_req = await get_environment_health(auth={})
    assert health_no_req["shadow_allow_paid"] is False

    # With pool having AI_SHADOW_ALLOW_PAID = 'true'
    pool_true = FakePool({"AI_SHADOW_ALLOW_PAID": "true"}, None)
    req_true = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool_true)))
    health_true = await get_environment_health(req_true, auth={})
    assert health_true["shadow_allow_paid"] is True

    # With pool having AI_SHADOW_ALLOW_PAID = 'false'
    pool_false = FakePool({"AI_SHADOW_ALLOW_PAID": "false"}, None)
    req_false = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool_false)))
    health_false = await get_environment_health(req_false, auth={})
    assert health_false["shadow_allow_paid"] is False


@pytest.mark.asyncio
async def test_get_shadow_allow_paid_default_and_stored():
    # When not set in system_settings
    pool_empty = FakePool({}, None)
    req_empty = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool_empty)))
    out = await get_shadow_allow_paid(req_empty, auth={})
    assert out == {"key": "AI_SHADOW_ALLOW_PAID", "allow_paid": False, "default": False}

    # When set to true
    pool_true = FakePool({"AI_SHADOW_ALLOW_PAID": "true"}, None)
    req_true = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool_true)))
    out_true = await get_shadow_allow_paid(req_true, auth={})
    assert out_true == {"key": "AI_SHADOW_ALLOW_PAID", "allow_paid": True, "default": False}


@pytest.mark.asyncio
async def test_update_shadow_allow_paid_success_and_audit():
    pool, auth_mgr = WritePool({}), FakeAuthMgr()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)), client=SimpleNamespace(host="127.0.0.1"))
    body = ShadowPaidUpdateRequest(allow_paid=True, reason="Enabling shadow test models")
    out = await update_shadow_allow_paid(body, request, auth=OPERATOR, auth_mgr=auth_mgr, x_csrf_token="tok")
    assert out == {"success": True, "key": "AI_SHADOW_ALLOW_PAID", "allow_paid": True}
    assert pool.inserts() == [("AI_SHADOW_ALLOW_PAID", "true")]
    assert len(auth_mgr.events) == 1
    event = auth_mgr.events[0]
    assert event["action"] == "SETTING_UPDATE"
    assert event["username"] == "op"
    assert event["details"] == {"key": "AI_SHADOW_ALLOW_PAID", "allow_paid": True, "reason": "Enabling shadow test models"}

    # Update back to False
    body_false = ShadowPaidUpdateRequest(allow_paid=False, reason="Disabling shadow test models")
    out_false = await update_shadow_allow_paid(body_false, request, auth=OPERATOR, auth_mgr=auth_mgr, x_csrf_token="tok")
    assert out_false == {"success": True, "key": "AI_SHADOW_ALLOW_PAID", "allow_paid": False}
    assert pool.inserts()[-1] == ("AI_SHADOW_ALLOW_PAID", "false")


@pytest.mark.asyncio
async def test_update_shadow_allow_paid_csrf_check():
    pool, auth_mgr = WritePool({}), FakeAuthMgr()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)), client=SimpleNamespace(host="127.0.0.1"))
    body = ShadowPaidUpdateRequest(allow_paid=True, reason="Valid test reason")
    with pytest.raises(HTTPException) as exc:
        await update_shadow_allow_paid(body, request, auth=OPERATOR, auth_mgr=auth_mgr, x_csrf_token="bad-csrf")
    assert exc.value.status_code == 403
    assert pool.inserts() == []
    assert auth_mgr.events == []


@pytest.mark.asyncio
async def test_get_settings_audit():
    import datetime
    import uuid

    event_id = uuid.uuid4()
    now = datetime.datetime.now(datetime.UTC)
    mock_events = [
        {
            "event_id": event_id,
            "username": "op",
            "action": "SETTING_UPDATE",
            "ip_address": "127.0.0.1",
            "details": '{"key": "AI_SHADOW_ALLOW_PAID", "allow_paid": true}',
            "occurred_at": now,
        },
        {
            "event_id": "string-id-123",
            "username": "admin",
            "action": "ROUTE_UPDATE",
            "ip_address": "192.168.1.1",
            "details": {"role": "hunter", "model": "gemini"},
            "occurred_at": now.isoformat(),
        },
    ]

    class FakeAuditConn:
        async def fetch(self, sql, *args):
            return mock_events

    class FakeAuditPool:
        def acquire(self):
            conn = FakeAuditConn()

            class _Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False

            return _Ctx()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakeAuditPool())))
    result = await get_settings_audit(request, auth=OPERATOR)
    assert "events" in result
    events = result["events"]
    assert len(events) == 2
    assert events[0]["event_id"] == str(event_id)
    assert events[0]["action"] == "SETTING_UPDATE"
    assert events[0]["details"] == {"key": "AI_SHADOW_ALLOW_PAID", "allow_paid": True}
    assert events[0]["occurred_at"] == now.isoformat()

    assert events[1]["event_id"] == "string-id-123"
    assert events[1]["details"] == {"role": "hunter", "model": "gemini"}
    assert events[1]["occurred_at"] == now.isoformat()

    # Without pool
    no_pool_req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    assert await get_settings_audit(no_pool_req, auth=OPERATOR) == {"events": []}

