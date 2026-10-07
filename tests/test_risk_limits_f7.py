"""F7: configurable, tighten-only risk limits. Pure rules, the orchestrator applying them to the
dispatcher (tighter only), migration 035, and POST/GET /api/v1/control/risk-limits on a throwaway DB
with a real operator and TOTP. No model, no dev DB."""

import inspect
import uuid
from decimal import Decimal
from types import SimpleNamespace

import asyncpg
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution import risk_limits as rl
from cryptonite_execution.risk import GuardedOrderDispatcher
from cryptonite_orchestrator import ops_monitor, risk_overrides
from migrated_db import migrated_database

D = Decimal
V = rl.RiskLimitValues
URL = "/api/v1/control/risk-limits"


# --- pure rules ------------------------------------------------------------------------------------

def test_code_defaults_are_the_dispatcher_defaults_and_the_invariants():
    params = inspect.signature(GuardedOrderDispatcher.__init__).parameters
    assert params["max_daily_loss_pct"].default * 100 == rl.DEFAULT_MAX_DAILY_LOSS_PCT == D("5")
    assert params["max_position_concentration_pct"].default * 100 == rl.DEFAULT_MAX_ORDER_EQUITY_PCT == D("25")
    assert params["min_order_equity_pct"].default * 100 == rl.MIN_ORDER_EQUITY_PCT
    assert rl.DEFAULT_CROSS_MANDATE_EXPOSURE_PCT == rl.DEFAULT_MAX_ORDER_EQUITY_PCT  # F4 cap = single-order clamp
    assert rl.NOT_CONFIGURABLE == {"account_backstop_drawdown_pct": D("20")}
    assert "account_backstop_drawdown_pct" not in rl.LIMIT_NAMES


def test_tightening_from_the_defaults_needs_no_totp():
    c = rl.validate_change(V(), V(D("2.5"), D("10"), D("15")))
    assert c.ok and not c.needs_totp
    assert set(c.tightened) == set(rl.LIMIT_NAMES)


def test_nothing_goes_past_the_code_defaults_even_with_totp():
    c = rl.validate_change(V(), V(D("5.01"), D("26"), D("30")))
    assert not c.ok and set(c.errors) == set(rl.LIMIT_NAMES)
    assert "above the code default" in c.errors["max_daily_loss_pct"]


@pytest.mark.parametrize("values,field", [
    (V(max_daily_loss_pct=D("0")), "max_daily_loss_pct"),
    (V(max_daily_loss_pct=D("-1")), "max_daily_loss_pct"),
    (V(max_order_equity_pct=D("0.5")), "max_order_equity_pct"),  # below the 1% minimum order
    (V(cross_mandate_exposure_pct=D("0")), "cross_mandate_exposure_pct"),
    (V(max_daily_loss_pct=D("NaN")), "max_daily_loss_pct"),
])
def test_out_of_range_values_are_refused(values, field):
    c = rl.validate_change(V(), values)
    assert field in c.errors and not c.ok


def test_order_cap_may_equal_the_minimum_order():
    assert rl.validate_change(V(), V(max_order_equity_pct=D("1"))).ok


def test_loosening_toward_the_default_needs_totp():
    stored = V(D("2"), D("10"), D("10"))
    c = rl.validate_change(stored, V(D("3"), D("10"), D("10")))
    assert c.ok and c.loosened == ("max_daily_loss_pct",)
    c = rl.validate_change(stored, V(None, D("10"), D("10")))  # removing an override = back to the default
    assert c.needs_totp and c.loosened == ("max_daily_loss_pct",)
    c = rl.validate_change(stored, V(D("1"), D("5"), D("10")))
    assert c.ok and not c.needs_totp and set(c.tightened) == {"max_daily_loss_pct", "max_order_equity_pct"}
    assert not rl.validate_change(stored, stored).needs_totp  # no change


def test_dispatcher_targets_are_never_looser_than_the_baseline():
    base = rl.DispatcherBaseline(D("0.05"), D("0.25"))
    assert rl.dispatcher_targets(base, V()) == (D("0.05"), D("0.25"))
    assert rl.dispatcher_targets(base, V(D("2"), D("10"))) == (D("0.02"), D("0.10"))
    # a row that somehow holds a looser value (no CHECK) still cannot loosen anything
    assert rl.dispatcher_targets(base, V(D("9"), D("40"))) == (D("0.05"), D("0.25"))
    # a dispatcher built tighter than the defaults keeps its own values
    tight = rl.DispatcherBaseline.capture(SimpleNamespace(max_daily_loss_pct=D("0.03"), max_position_concentration_pct=D("0.5")))
    assert (tight.max_daily_loss, tight.max_order_equity) == (D("0.03"), D("0.25"))
    assert rl.cross_mandate_cap_pct(V(cross_mandate_exposure_pct=D("40"))) == D("25")
    assert rl.cross_mandate_cap_pct(V(cross_mandate_exposure_pct=D("12"))) == D("12")


# --- orchestrator: read, apply tighter only, publish ------------------------------------------------

class OneRowConn:
    def __init__(self, row=None, fail=False):
        self.row, self.fail = row, fail

    async def fetchrow(self, sql, *args):
        if self.fail:
            raise ConnectionError("db down")
        assert "risk_limit_overrides" in sql
        return self.row


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return Ctx()


def engine_with_defaults():
    return SimpleNamespace(account_id="PAPER-ACCOUNT-001", dispatcher=SimpleNamespace(
        max_daily_loss_pct=D("0.05"), max_position_concentration_pct=D("0.25"), min_order_equity_pct=D("0.01"),
        taker_fee=D("0.001")))


async def test_orchestrator_applies_overrides_tighter_only_and_publishes_them():
    state = risk_overrides.RiskState()
    engine = engine_with_defaults()
    row = {"max_daily_loss_pct": D("2.5"), "max_order_equity_pct": D("10"), "cross_mandate_exposure_pct": D("15")}
    await risk_overrides.refresh_risk_limits(engine, Pool(OneRowConn(row)), state, env="PAPER")
    d = engine.dispatcher
    assert (d.max_daily_loss_pct, d.max_position_concentration_pct, state.cross_cap_pct) == (D("0.025"), D("0.10"), D("15"))
    paper = ops_monitor.effective_limits(d, cross_mandate_exposure_pct=state.cross_cap_pct)["paper"]
    assert (paper["max_daily_loss_pct"], paper["max_order_equity_pct"], paper["cross_mandate_exposure_pct"]) == ("2.5", "10", "15")
    assert paper["min_order_equity_pct"] == "1" and paper["account_backstop_drawdown_pct"] == "20"

    # A failed read keeps what is applied (never loosens)
    await risk_overrides.refresh_risk_limits(engine, Pool(OneRowConn(fail=True)), state, env="PAPER")
    assert (d.max_daily_loss_pct, d.max_position_concentration_pct) == (D("0.025"), D("0.10"))

    # Looser-than-default values cannot get through
    looser = {"max_daily_loss_pct": D("8"), "max_order_equity_pct": D("40"), "cross_mandate_exposure_pct": D("60")}
    await risk_overrides.refresh_risk_limits(engine, Pool(OneRowConn(looser)), state, env="PAPER")
    assert (d.max_daily_loss_pct, d.max_position_concentration_pct, state.cross_cap_pct) == (D("0.05"), D("0.25"), D("25"))

    # Removing the overrides (a TOTP-confirmed loosening) restores the baseline, nothing more
    await risk_overrides.refresh_risk_limits(engine, Pool(OneRowConn(row)), state, env="PAPER")
    await risk_overrides.refresh_risk_limits(engine, Pool(OneRowConn(None)), state, env="PAPER")
    assert (d.max_daily_loss_pct, d.max_position_concentration_pct, state.cross_cap_pct) == (D("0.05"), D("0.25"), D("25"))


async def test_ops_monitor_applies_overrides_before_its_first_publish(monkeypatch):
    calls = []

    async def fake_refresh(engine, pool):
        calls.append("limits")
        engine.dispatcher.max_daily_loss_pct = D("0.02")

    async def fake_betas(pool):
        calls.append("betas")

    async def fake_publish(engine, redis):
        calls.append(("publish", engine.dispatcher.max_daily_loss_pct))
        raise KeyboardInterrupt  # stop the loop after the first publish

    monkeypatch.setattr(risk_overrides, "refresh_risk_limits", fake_refresh)
    monkeypatch.setattr(risk_overrides, "refresh_betas", fake_betas)
    monkeypatch.setattr(ops_monitor, "publish_effective_limits", fake_publish)
    with pytest.raises(KeyboardInterrupt):
        await ops_monitor.ops_monitor(engine_with_defaults(), None, None)
    assert calls == ["limits", "betas", ("publish", D("0.02"))]


# --- migration 035 + API ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_risk_limits") as url:
        yield url


async def test_migration_035_repeats_the_invariants(db_url):
    conn = await asyncpg.connect(db_url)
    try:
        acct = f"A-{uuid.uuid4().hex[:6]}"
        await conn.execute(
            "INSERT INTO risk_limit_overrides (environment, account_id, max_daily_loss_pct, updated_by, reason, override_version) VALUES ('PAPER', $1, 2, 'op', 'tighten', 1)",
            acct)
        for col, val in [("max_daily_loss_pct", "5.5"), ("max_order_equity_pct", "26"), ("max_order_equity_pct", "0.5"),
                         ("cross_mandate_exposure_pct", "25.01"), ("cross_mandate_exposure_pct", "0")]:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(f"UPDATE risk_limit_overrides SET {col} = {val} WHERE account_id = $1", acct)
        grants = {r["grantee"]: r["privs"] for r in await conn.fetch(
            """SELECT grantee, array_agg(privilege_type::text ORDER BY privilege_type) AS privs
               FROM information_schema.role_table_grants WHERE table_name = 'risk_limit_overrides' GROUP BY grantee""")}
        if "cryptonite_api" in grants:
            assert set(grants["cryptonite_api"]) == {"INSERT", "SELECT", "UPDATE"}
        if "cryptonite_executor" in grants:
            assert set(grants["cryptonite_executor"]) == {"SELECT"}
        assert "cryptonite_agent_ro" not in grants
    finally:
        await conn.close()


async def test_least_privilege_grants(db_url):
    conn = await asyncpg.connect(db_url)
    try:
        priv = {(role, p): await conn.fetchval("SELECT has_table_privilege($1, 'risk_limit_overrides', $2)", role, p)
                for role, p in [("cryptonite_agent_ro", "SELECT"), ("cryptonite_api", "SELECT"), ("cryptonite_api", "UPDATE"),
                                ("cryptonite_api", "DELETE"), ("cryptonite_executor", "SELECT"), ("cryptonite_executor", "UPDATE")]}
    finally:
        await conn.close()
    assert priv == {("cryptonite_agent_ro", "SELECT"): False, ("cryptonite_api", "SELECT"): True, ("cryptonite_api", "UPDATE"): True,
                    ("cryptonite_api", "DELETE"): False, ("cryptonite_executor", "SELECT"): True, ("cryptonite_executor", "UPDATE"): False}


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url, heartbeat=False)
    try:
        yield harness
    finally:
        await close_harness(harness)


async def test_risk_limits_api_tightens_freely_and_loosens_only_with_totp(h):
    acct = f"PAPER-{uuid.uuid4().hex[:6]}"
    body = {"environment": "PAPER", "account_id": acct, "reason": "tighten for the L2 review"}
    async with h.client() as c:
        # CSRF first
        assert (await c.post(URL, json={**body, "max_daily_loss_pct": "2.5"})).status_code == 403

        view = (await c.get(URL, params={"account_id": acct})).json()
        assert view["overrides"] == {"max_daily_loss_pct": None, "max_order_equity_pct": None, "cross_mandate_exposure_pct": None}
        assert view["defaults"] == {"max_daily_loss_pct": "5", "max_order_equity_pct": "25", "cross_mandate_exposure_pct": "25"}
        assert view["not_configurable"] == {"account_backstop_drawdown_pct": "20"}

        # Tighten: no TOTP
        res = await c.post(URL, json={**body, "max_daily_loss_pct": "2.5", "max_order_equity_pct": "10",
                                      "cross_mandate_exposure_pct": "15"}, headers=h.headers)
        assert res.status_code == 200, res.text
        assert res.json()["effective"] == {"max_daily_loss_pct": "2.5", "max_order_equity_pct": "10", "cross_mandate_exposure_pct": "15"}
        assert set(res.json()["tightened"]) == {"max_daily_loss_pct", "max_order_equity_pct", "cross_mandate_exposure_pct"}
        row = await h.pool.fetchrow("SELECT * FROM risk_limit_overrides WHERE account_id = $1", acct)
        assert (row["max_daily_loss_pct"], row["updated_by"], row["reason"]) == (D("2.5"), h.username, "tighten for the L2 review")

        # Past the invariant: refused with one error per field, nothing stored, even with TOTP
        res = await c.post(URL, json={**body, "max_daily_loss_pct": "6", "max_order_equity_pct": "30",
                                      "totp_code": h.totp.now()}, headers=h.headers)
        assert res.status_code == 422
        assert {e["loc"][1] for e in res.json()["detail"]} == {"max_daily_loss_pct", "max_order_equity_pct"}
        assert "above the code default" in res.json()["detail"][0]["msg"]
        assert (await h.pool.fetchval("SELECT max_daily_loss_pct FROM risk_limit_overrides WHERE account_id = $1", acct)) == D("2.5")

        # Loosen without / with a wrong TOTP: 401, nothing stored
        loosen = {**body, "max_daily_loss_pct": "4", "max_order_equity_pct": "10", "cross_mandate_exposure_pct": "15",
                  "reason": "back toward the default"}
        res = await c.post(URL, json=loosen, headers=h.headers)
        assert res.status_code == 401 and "TOTP_REQUIRED" in res.json()["detail"]
        assert (await c.post(URL, json={**loosen, "totp_code": "123456" if h.totp.now() != "123456" else "654321"},
                             headers=h.headers)).status_code == 401
        assert (await h.pool.fetchval("SELECT max_daily_loss_pct FROM risk_limit_overrides WHERE account_id = $1", acct)) == D("2.5")

        # With the operator's TOTP: stored
        res = await c.post(URL, json={**loosen, "totp_code": h.totp.now()}, headers=h.headers)
        assert res.status_code == 200, res.text
        assert res.json()["loosened"] == ["max_daily_loss_pct"]
        assert (await h.pool.fetchval("SELECT max_daily_loss_pct FROM risk_limit_overrides WHERE account_id = $1", acct)) == D("4")

    actions = [r["action"] for r in await h.pool.fetch(
        "SELECT action FROM auth_audit_events WHERE username = $1 AND action LIKE 'RISK_LIMITS%' ORDER BY occurred_at", h.username)]
    assert actions.count("RISK_LIMITS_UPDATE") == 2 and actions.count("RISK_LIMITS_UPDATE_FAILED_AUTH") == 2


async def test_orchestrator_reads_what_the_api_stored(h):
    acct = f"PAPER-{uuid.uuid4().hex[:6]}"
    async with h.client() as c:
        res = await c.post(URL, json={"account_id": acct, "reason": "tighten daily loss", "max_daily_loss_pct": "3",
                                      "cross_mandate_exposure_pct": "20"}, headers=h.headers)
        assert res.status_code == 200, res.text
    engine = engine_with_defaults()
    engine.account_id = acct
    state = risk_overrides.RiskState()
    await risk_overrides.refresh_risk_limits(engine, h.pool, state, env="PAPER")
    assert (engine.dispatcher.max_daily_loss_pct, engine.dispatcher.max_position_concentration_pct) == (D("0.03"), D("0.25"))
    assert state.cross_cap_pct == D("20")
