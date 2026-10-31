"""Migration 050 (ADR 0011, auto-trade profiles P0.3): profile columns and CHECKs, backfills, coin locks, decision log,
grants, the Brain's view, the refused downgrade and the round trip."""

import asyncio
import importlib.util
import os
import subprocess
import sys
import uuid

import asyncpg
import pytest
from migrated_db import REPO_ROOT, migrated_database

MIG_PATH = REPO_ROOT / "infra/migrations/versions/050_auto_trade_profiles.py"


def _alembic(url, *args, check=True):
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
                          cwd=REPO_ROOT, env=env, check=check, capture_output=True, text=True)


def _out(proc):
    return proc.stderr + proc.stdout


def _run(url, sql, *args):
    async def go():
        c = await asyncpg.connect(url)
        try:
            return await c.fetch(sql, *args)
        finally:
            await c.close()
    return asyncio.run(go())


def _val(url, sql, *args):
    rows = _run(url, sql, *args)
    return rows[0][0] if rows else None


def _insert(url, **cols):
    """Insert a mandate. Column names are the test's own literals."""
    base = {"name": f"m-{uuid.uuid4().hex[:6]}", "capital_budget_usd": 10000, "instruments": ["BTCUSDT.BINANCE"],
            "max_position_pct": 25, "max_drawdown_pct": 20, "allow_perps": False, "max_leverage": 1,
            "autonomy_level": 1, "created_by": "test"}
    base.update(cols)
    keys = list(base)
    return _val(url, f"INSERT INTO ai_mandates ({', '.join(keys)}) VALUES "
                     f"({', '.join(f'${i + 1}' for i in range(len(keys)))}) RETURNING id", *base.values())


@pytest.fixture
def db049():
    with migrated_database("cryptonite_m050") as url:
        _alembic(url, "downgrade", "049")
        yield url


def _load_local_base_asset():
    spec = importlib.util.spec_from_file_location("mig050", MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.base_asset


def test_local_base_asset_equals_contract():
    from cryptonite_schemas.profile import base_asset as contract
    local = _load_local_base_asset()
    cases = ["BTCUSDT", "ETHBTC", "BNBETH", "1000PEPEUSDT", "BTCFDUSD", "USDCUSDT", "WLDUSDT", "XRPUSDT",
             "BTCUSDT-PERP.BINANCE", "ETHUSDT-MARGIN.BINANCE", "btcusdt", "ethusdt-perp", "SOLTRY.BINANCE", "DOGEEUR"]
    for case in cases:
        assert local(case) == contract(case), case
    for bad in ["XYZ", "USDT", "", "BTC", "-PERP.BINANCE", "USDT-PERP.BINANCE"]:
        with pytest.raises(ValueError):
            contract(bad)
        with pytest.raises(RuntimeError):
            local(bad)


def test_backfill_and_locks(db049):
    url = db049
    spot = _insert(url, name="spot", instruments=["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"], status="PAUSED")
    perp = _insert(url, name="perp", instruments=["SOLUSDT-PERP.BINANCE"], allow_perps=True, max_leverage=3)
    mixed = _insert(url, name="mixed", instruments=["BNBUSDT.BINANCE", "XRPUSDT-PERP.BINANCE"], allow_perps=True,
                    max_risk_per_trade_pct=1, daily_loss_limit_pct=2)
    allow_only = _insert(url, name="allowonly", instruments=["ADAUSDT.BINANCE"], allow_perps=True)
    spot_lev = _insert(url, name="spotlev", instruments=["DOGEUSDT.BINANCE"], max_leverage=4, status="HALTED")
    archived = _insert(url, name="archived", instruments=["BTCUSDT.BINANCE"], status="ARCHIVED")
    _alembic(url, "upgrade", "050")

    markets = {r["name"]: (list(r["markets"]), r["allow_perps"], r["daily_loss_limit_pct"], r["max_leverage"])
               for r in _run(url, "SELECT name, markets, allow_perps, daily_loss_limit_pct, max_leverage FROM ai_mandates")}
    assert markets["spot"][0] == ["SPOT"]
    assert markets["perp"][0] == ["PERP"] and markets["perp"][1] is True
    assert markets["mixed"][0] == ["PERP", "SPOT"]
    assert markets["allowonly"][0] == ["PERP", "SPOT"]  # allow_perps adds PERP
    assert markets["spotlev"][3] == 1  # leverage was unusable on spot: clamped so the cross CHECK holds
    # D9: LEAST(3 * risk (default 1), drawdown 20) = 3; an existing limit stays; ARCHIVED stays NULL
    assert float(markets["spot"][2]) == 3 and float(markets["mixed"][2]) == 2
    assert markets["archived"][2] is None
    # defaults
    row = _run(url, "SELECT * FROM ai_mandates WHERE name = 'spot'")[0]
    assert (row["environment"], row["direction"], row["style"], row["max_open_positions"], row["max_debates_per_day"],
            float(row["exit_tp_r"]), float(row["exit_tp1_scale_pct"]), row["dry_run"], row["notify_level"],
            row["breaker_max_loss_streak"], row["breaker_max_reject_streak"]) == (
        "PAPER", "LONG", "SWING", 3, 6, 2.0, 50.0, False, "IMPORTANT", 4, 5)
    assert row["exit_trail_pct"] is None and row["exit_max_hold_hours"] is None and row["paused_reason"] is None

    locks = {r["base_asset"]: r["mandate_id"] for r in _run(url, "SELECT base_asset, mandate_id FROM ai_profile_coin_locks")}
    assert locks == {"BTC": spot, "ETH": spot, "SOL": perp, "BNB": mixed, "XRP": mixed, "ADA": allow_only, "DOGE": spot_lev}
    assert archived not in locks.values()  # ARCHIVED holds none


def test_overlap_raises_and_rolls_back(db049):
    url = db049
    _insert(url, name="alpha", instruments=["BTCUSDT.BINANCE"])
    _insert(url, name="beta", instruments=["BTCUSDT-PERP.BINANCE"], allow_perps=True, status="PAUSED")
    _insert(url, name="gamma", instruments=["ETHUSDT.BINANCE"])
    _insert(url, name="stopped-ok", instruments=["BTCUSDT.BINANCE"], status="ARCHIVED")
    up = _alembic(url, "upgrade", "050", check=False)
    text = _out(up)
    assert up.returncode != 0
    assert "RuntimeError: migration 050: base assets owned by more than one mandate" in text
    assert "BTC: alpha (ACTIVE), beta (PAUSED)" in text
    assert "ETH" not in text.split("BTC:")[1].split("Fix:")[0]
    assert "edit instruments or archive with `scripts/profile_overlap_resolve.py` (runbook O1)" in text
    assert "PAUSED and HALTED mandates still hold their coins" in text
    assert "pause, stop or edit" not in text
    # full rollback: still 049
    assert _val(url, "SELECT version_num FROM alembic_version") == "049"
    assert _val(url, "SELECT count(*) FROM information_schema.columns "
                     "WHERE table_name = 'ai_mandates' AND column_name = 'markets'") == 0
    assert _val(url, "SELECT to_regclass('ai_profile_coin_locks')") is None


def test_no_overlap_perp_and_spot_of_different_coins(db049):
    url = db049
    a = _insert(url, name="a", instruments=["BTCUSDT.BINANCE"])
    b = _insert(url, name="b", instruments=["ETHUSDT-PERP.BINANCE"], allow_perps=True)
    _alembic(url, "upgrade", "050")
    assert {r[0]: r[1] for r in _run(url, "SELECT base_asset, mandate_id FROM ai_profile_coin_locks")} == {"BTC": a, "ETH": b}


@pytest.fixture(scope="module")
def head():
    with migrated_database("cryptonite_m050h") as url:
        yield url


def test_trigger_defaults_for_old_writers(head):
    def row(**cols):
        mid = _insert(head, **cols)
        r = _run(head, "SELECT markets, allow_perps, daily_loss_limit_pct FROM ai_mandates WHERE id = $1", mid)[0]
        return list(r["markets"]), r["allow_perps"], r["daily_loss_limit_pct"]

    assert row()[0:2] == (["SPOT"], False)
    assert row(instruments=["SOLUSDT-PERP.BINANCE"], allow_perps=True)[0:2] == (["PERP"], True)
    assert row(instruments=["SOLUSDT-PERP.BINANCE"])[0:2] == (["SPOT"], False)  # a listed perp never grants perps
    assert row(instruments=["SOLUSDT.BINANCE", "ETHUSDT-PERP.BINANCE"])[0:2] == (["SPOT"], False)
    assert row(instruments=["SOLUSDT-MARGIN.BINANCE"])[0:2] == (["SPOT"], False)  # SPOT_MARGIN is never derived
    assert row(instruments=["SOLUSDT.BINANCE"], allow_perps=True)[0:2] == (["PERP", "SPOT"], True)
    assert float(row(max_risk_per_trade_pct=2)[2]) == 6  # ACTIVE without a limit: 3 x risk
    assert float(row(max_risk_per_trade_pct=2, max_drawdown_pct=5)[2]) == 5  # capped at the hard stop
    assert row(status="PAUSED")[2] is None
    assert row(markets=["SPOT", "SPOT_MARGIN"], daily_loss_limit_pct=2)[0] == ["SPOT", "SPOT_MARGIN"]
    with pytest.raises(asyncpg.CheckViolationError):  # an explicit markets that contradicts allow_perps still fails
        _insert(head, markets=["PERP"], allow_perps=False)


@pytest.mark.parametrize("bad", [
    {"environment": "LIVE"}, {"markets": []}, {"markets": ["FUTURES"]}, {"direction": "FLAT"}, {"style": "SCALP"},
    {"max_open_positions": 0}, {"max_open_positions": 21}, {"max_debates_per_day": -1}, {"max_debates_per_day": 49},
    {"exit_tp_r": 0.4}, {"exit_tp_r": 21}, {"exit_tp1_scale_pct": 101}, {"exit_trail_activate_pct": 0},
    {"exit_trail_activate_pct": 51}, {"exit_trail_pct": 0}, {"exit_trail_pct": 51},
    {"exit_trail_pct": 5, "exit_trail_activate_pct": 4}, {"exit_max_hold_hours": 0}, {"exit_max_hold_hours": 721},
    {"notify_level": "LOUD"}, {"breaker_max_loss_streak": 0}, {"breaker_max_loss_streak": 51},
    {"breaker_max_reject_streak": 0}, {"breaker_max_reject_streak": 51}, {"status": "RUNNING"},
    {"direction": "SHORT"},                                   # SPOT only
    {"direction": "BOTH", "markets": ["SPOT"]},
    {"max_leverage": 2},                                      # SPOT only
    {"markets": ["PERP"], "allow_perps": False},              # allow_perps must follow markets
    {"markets": ["SPOT"], "allow_perps": True},
])
def test_checks_reject_bad_rows(head, bad):
    cols = {"markets": ["SPOT"], "daily_loss_limit_pct": 3, **bad}
    with pytest.raises(asyncpg.CheckViolationError):
        _insert(head, **cols)


def test_an_allow_perps_only_update_is_a_noop_not_a_check_failure(head):
    """M2: markets is the truth. A pre-P0.4 writer (an old orchestrator's hard stop during the deploy window) that sets
    only allow_perps=false on a perps mandate must succeed and leave allow_perps true."""
    mid = _insert(head, markets=["PERP"], allow_perps=True, max_leverage=3, daily_loss_limit_pct=3)
    _run(head, "UPDATE ai_mandates SET allow_perps = false, status = 'HALTED' WHERE id = $1", mid)
    r = _run(head, "SELECT markets, allow_perps, status FROM ai_mandates WHERE id = $1", mid)[0]
    assert list(r["markets"]) == ["PERP"] and r["allow_perps"] is True and r["status"] == "HALTED"
    # a write that changes markets still moves allow_perps with it
    _run(head, "UPDATE ai_mandates SET markets = ARRAY['SPOT'], allow_perps = false, max_leverage = 1 WHERE id = $1", mid)
    assert _run(head, "SELECT allow_perps FROM ai_mandates WHERE id = $1", mid)[0]["allow_perps"] is False
    # a contradicting explicit write that changes markets still fails the CHECK
    with pytest.raises(asyncpg.CheckViolationError):
        _run(head, "UPDATE ai_mandates SET markets = ARRAY['PERP'], allow_perps = false WHERE id = $1", mid)


def test_checks_accept_good_rows(head):
    _insert(head, markets=["SPOT", "SPOT_MARGIN"], direction="BOTH", max_leverage=3, daily_loss_limit_pct=3,
            exit_trail_activate_pct=4, exit_trail_pct=4, exit_max_hold_hours=720, status="STOPPING")
    _insert(head, markets=["PERP"], allow_perps=True, autonomy_level=3, max_leverage=5, daily_loss_limit_pct=3)  # D4
    _insert(head, markets=["SPOT"], status="STOPPED", daily_loss_limit_pct=None)
    _insert(head, markets=["SPOT"], status="PAUSED", daily_loss_limit_pct=None)


def test_grants_and_view(head):
    for role in ("cryptonite_api", "cryptonite_agent_ro"):
        assert _val(head, "SELECT count(*) FROM pg_roles WHERE rolname = $1", role) == 1, role
    mid = _insert(head, name="grants", markets=["SPOT"], daily_loss_limit_pct=3, instruments=["LTCUSDT.BINANCE"])

    def priv(role, obj, p):
        return _val(head, "SELECT has_table_privilege($1, $2, $3)", role, obj, p)

    for p in ("SELECT", "INSERT", "DELETE"):
        assert priv("cryptonite_api", "ai_profile_coin_locks", p)
    assert not priv("cryptonite_api", "ai_profile_coin_locks", "UPDATE")
    assert priv("cryptonite_api", "ai_profile_decisions", "SELECT") and priv("cryptonite_api", "ai_profile_decisions", "INSERT")
    assert not priv("cryptonite_api", "ai_profile_decisions", "DELETE")
    assert _val(head, "SELECT has_sequence_privilege('cryptonite_api', 'ai_profile_decisions_id_seq', 'USAGE')")
    for t in ("ai_profile_coin_locks", "ai_profile_decisions", "ai_mandates"):
        assert not priv("cryptonite_agent_ro", t, "SELECT"), t

    async def as_role(role, fn):
        c = await asyncpg.connect(head)
        try:
            await c.execute(f"SET ROLE {role}")
            return await fn(c)
        finally:
            await c.close()

    async def api(c):
        await c.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('LTC', $1)", mid)
        await c.execute("INSERT INTO ai_profile_decisions (mandate_id, stage, outcome) VALUES ($1, 'SCAN', 'CANDIDATE')", mid)
        with pytest.raises(asyncpg.UniqueViolationError):  # exclusivity is the primary key
            await c.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('LTC', $1)", mid)
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO ai_profile_decisions (mandate_id, stage, outcome) VALUES ($1, 'NOPE', 'x')", mid)
        await c.execute("DELETE FROM ai_profile_coin_locks WHERE base_asset = 'LTC'")
    asyncio.run(as_role("cryptonite_api", api))

    async def agent(c):
        return await c.fetchrow("SELECT environment, markets, direction, style, max_open_positions, dry_run, paused_reason, "
                                "daily_loss_limit_pct, session_pnl FROM agent_safe_mandate_health WHERE id = $1", mid)
    row = asyncio.run(as_role("cryptonite_agent_ro", agent))
    assert row["environment"] == "PAPER" and list(row["markets"]) == ["SPOT"] and row["direction"] == "LONG"
    assert row["style"] == "SWING" and row["max_open_positions"] == 3 and row["dry_run"] is False
    assert _val(head, "SELECT reloptions::text FROM pg_class WHERE relname = 'agent_safe_mandate_health'") == "{security_barrier=true}"


def test_cascade_deletes_locks_and_decisions(db049):
    url = db049
    mid = _insert(url, name="casc", instruments=["BTCUSDT.BINANCE"])
    _alembic(url, "upgrade", "050")
    _run(url, "INSERT INTO ai_profile_decisions (mandate_id, stage, outcome) VALUES ($1, 'GATE', 'PASS')", mid)
    _run(url, "DELETE FROM ai_mandates WHERE id = $1", mid)
    assert _val(url, "SELECT count(*) FROM ai_profile_coin_locks") == 0
    assert _val(url, "SELECT count(*) FROM ai_profile_decisions") == 0


@pytest.mark.parametrize("cols,needle", [
    ({"status": "STOPPING"}, "status=STOPPING"),
    ({"status": "STOPPED"}, "status=STOPPED"),
    ({"markets": ["SPOT_MARGIN"], "max_leverage": 2}, "SPOT_MARGIN"),
    ({"markets": ["PERP"], "allow_perps": True, "autonomy_level": 2}, "level=2"),
])
def test_downgrade_refusals_then_round_trip(head_per_case, cols, needle):
    url = head_per_case
    head = _val(url, "SELECT version_num FROM alembic_version")
    _insert(url, name="keeper", instruments=["ETHUSDT.BINANCE"], markets=["SPOT"], daily_loss_limit_pct=3)
    bad = _insert(url, name="blocker", instruments=["BTCUSDT.BINANCE"], **{"markets": ["SPOT"], "daily_loss_limit_pct": 3, **cols})
    down = _alembic(url, "downgrade", "049", check=False)
    assert down.returncode != 0 and "migration 050 downgrade refused" in _out(down) and "blocker" in _out(down)
    assert needle in _out(down)
    assert _val(url, "SELECT version_num FROM alembic_version") == head  # the failed downgrade is atomic
    _run(url, "DELETE FROM ai_mandates WHERE id = $1", bad)
    _alembic(url, "downgrade", "049")
    assert _val(url, "SELECT version_num FROM alembic_version") == "049"
    assert _val(url, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'ai_mandates' "
                     "AND column_name IN ('markets', 'direction', 'paused_reason')") == 0
    assert _val(url, "SELECT to_regclass('ai_profile_decisions')") is None
    # 049's status CHECK (4 values) and 048's perps-level CHECK are back; the daily loss backfill stays
    with pytest.raises(asyncpg.CheckViolationError):
        _insert(url, status="STOPPING")
    with pytest.raises(asyncpg.CheckViolationError):
        _insert(url, allow_perps=True, autonomy_level=2)
    assert float(_val(url, "SELECT daily_loss_limit_pct FROM ai_mandates WHERE name = 'keeper'")) == 3
    cols_049 = {r[0] for r in _run(url, "SELECT column_name FROM information_schema.columns "
                                        "WHERE table_name = 'agent_safe_mandate_health'")}
    assert "session_pnl" in cols_049 and "markets" not in cols_049
    _alembic(url, "upgrade", "head")
    assert _val(url, "SELECT count(*) FROM ai_profile_coin_locks") == 1  # keeper's ETH is locked again


@pytest.fixture
def head_per_case():
    with migrated_database("cryptonite_m050d") as url:
        yield url
