"""F4: correlation-aware exposure across mandates. The beta math and the cap check are pure; the refusal
is tested through the orchestrator's AI order path (``command_poller`` -> ``gate_ai_order``), with the
fake connection of ``test_ai_mandates`` and on a throwaway migrated DB with two mandates. No model, no
dev DB."""

import asyncio
import datetime
import json
import math
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import asyncpg
import polars as pl
import pytest
from cryptonite_execution import cross_exposure as cx
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_orchestrator import risk_overrides
from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance
from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
from cryptonite_schemas.mandate import (
    BREACH_CODES,
    AutonomyLevel,
    Mandate,
    MandateReject,
    MandateStatus,
    mandate_instance_id,
)
from migrated_db import migrated_database, seed_paper_account
from nautilus_trader.model import InstrumentId, Price, Quantity, QuoteTick
from test_ai_mandates import mandate_row, run_ai_command
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher

D = Decimal
H = 3_600_000_000_000
BTC, ETH = "BTCUSDT.BINANCE", "ETHUSDT.BINANCE"
ACCOUNT = "PAPER-ACCOUNT-001"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """The orchestrator's F4/F7 state is process-wide: every test starts from the defaults."""
    for name, value in (("betas", {}), ("betas_at", None), ("cross_cap_pct", cx.DEFAULT_CROSS_MANDATE_EXPOSURE_PCT),
                        ("baseline", None), ("overrides", risk_overrides.RiskLimitValues())):
        monkeypatch.setattr(risk_overrides.STATE, name, value)


# --- beta ---------------------------------------------------------------------------------------------

def series(returns, start=100.0, t0=0):
    out, px = [(t0, start)], start
    for i, r in enumerate(returns, 1):
        px *= 1 + r
        out.append((t0 + i * H, px))
    return out


def btc_returns(n=400):
    return [0.01 * math.sin(i * 0.7) + 0.004 * math.cos(i * 1.9) for i in range(n)]


def test_beta_of_a_scaled_copy_is_the_scale():
    rb = btc_returns()
    assert cx.beta_from_closes(series([0.8 * r for r in rb]), series(rb)) == D("0.8")
    assert cx.beta_from_closes(series(rb), series(rb)) == D("1")
    assert cx.beta_from_closes(series([-0.5 * r for r in rb]), series(rb)) == D("-0.5")


def test_beta_ignores_order_and_only_uses_aligned_consecutive_bars():
    rb = btc_returns()
    asset = series([1.2 * r for r in rb])
    asset_shuffled = list(reversed(asset))
    # drop every 10th BTC bar: the returns that span the gap are not formed, beta is unchanged
    btc = [p for i, p in enumerate(series(rb)) if i % 10 != 5]
    assert cx.beta_from_closes(asset_shuffled, btc) == D("1.2")


def test_beta_is_none_without_enough_data_or_a_moving_benchmark():
    rb = btc_returns(100)  # < a week of hourly returns
    assert cx.beta_from_closes(series(rb), series(rb)) is None
    assert cx.beta_from_closes([], series(btc_returns())) is None
    flat = series([0.0] * 400)
    assert cx.beta_from_closes(series(btc_returns()), flat) is None
    # different hours: nothing aligns
    assert cx.beta_from_closes(series(btc_returns(), t0=H // 2), series(btc_returns())) is None


def test_beta_symbols():
    assert cx.beta_symbol(ETH) == "ETHUSDT" == cx.beta_symbol("ETHUSDT-PERP.BINANCE")
    assert cx.instrument_beta({}, BTC) == D("1")
    assert cx.instrument_beta({"BTCUSDT": D("0.3")}, BTC) == D("1")  # BTC is the benchmark
    assert cx.instrument_beta({}, ETH) == cx.FALLBACK_BETA == D("1")  # missing data: 1.0
    assert cx.instrument_beta({"ETHUSDT": D("-0.7")}, ETH) == D("0.7")  # never offsets


# --- the cap ------------------------------------------------------------------------------------------

def test_exposure_is_beta_weighted_and_capped_at_25_pct_of_equity():
    equity = D("100000")
    held = {BTC: D("20000")}
    assert cx.beta_weighted_exposure({BTC: D("20000"), ETH: D("10000")}, {"ETHUSDT": D("0.8")}) == D("28000")
    refusal = cx.check_cross_mandate_exposure(held, ETH, D("6000"), {}, equity)
    assert refusal.startswith(cx.CROSS_MANDATE_EXPOSURE) and "$26,000" in refusal and "25% of equity" in refusal
    assert cx.check_cross_mandate_exposure(held, ETH, D("6000"), {"ETHUSDT": D("0.5")}, equity) is None  # 23,000
    assert cx.check_cross_mandate_exposure(held, ETH, D("5000"), {}, equity) is None  # exactly at the cap
    # the cap can be lowered (F7) but never raised above the default
    assert cx.check_cross_mandate_exposure(held, ETH, D("1000"), {}, equity, D("20")) is not None
    assert cx.check_cross_mandate_exposure(held, ETH, D("6000"), {}, equity, D("50")) is not None
    assert cx.check_cross_mandate_exposure({}, ETH, D("1"), {}, D("0")) is not None


# --- through the orchestrator's AI order path (fake connection) --------------------------------------

def mandate(**kw) -> Mandate:
    base = dict(id=uuid.uuid4(), name="m", capital_budget_usd=D("10000"), instruments=[BTC], max_position_pct=D("25"),
                autonomy_level=AutonomyLevel.L3)
    return Mandate(**{**base, **kw})


async def test_entry_over_the_cap_is_refused_before_the_dispatcher_and_is_not_a_breach():
    m = mandate()
    held = [{"instance_id": mandate_instance_id(m.id), "instrument_id": BTC, "side": "LONG",
             "quantity": D("0.03"), "current_price": D("50000")}]  # $1,500 of a 10,000 equity
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "quantity": "0.008", "mandate_id": str(m.id)}  # ~$410
    conn, engine = await run_ai_command(payload, mandate_row=mandate_row(m), positions=held)
    assert conn.outcome() == ("COMPLETED", None)  # 1,910 < 2,500

    risk_overrides.STATE.cross_cap_pct = D("15")  # F7: cap lowered to $1,500
    conn, engine = await run_ai_command(payload, mandate_row=mandate_row(m), positions=held)
    status, reason = conn.outcome()
    assert status == "REJECTED" and reason.startswith("CROSS_MANDATE_EXPOSURE")
    assert engine.dispatcher.open_orders == {} and engine.dispatcher.reserved_cash == 0
    assert conn.events == []  # no BREACH, no demotion
    assert m.instance_id not in conn.instances  # refused before anything was created
    assert "CROSS_MANDATE_EXPOSURE" not in {c.value for c in MandateReject}  # not a mandate code
    assert all(c.value != "CROSS_MANDATE_EXPOSURE" for c in BREACH_CODES)


async def test_an_exit_is_never_refused_even_over_the_cap():
    m = mandate(status=MandateStatus.HALTED)
    held = [{"instance_id": mandate_instance_id(m.id), "instrument_id": BTC, "side": "LONG",
             "quantity": D("0.1"), "current_price": D("50000")}]  # $5,000: 50% of equity, far over any cap
    risk_overrides.STATE.cross_cap_pct = D("1")
    payload = {"instrument_id": "BTCUSDT", "side": "SELL", "quantity": "0.05", "mandate_id": str(m.id)}
    conn, engine = await run_ai_command(payload, mandate_row=mandate_row(m), positions=held, dispatcher_position=D("0.1"))
    status, reason = conn.outcome()
    assert not (reason or "").startswith("CROSS_MANDATE_EXPOSURE")
    assert status == "COMPLETED"


# --- integration: two mandates on a migrated DB, the real command poller -----------------------------

def quote(inst: str, bid: str, ask: str) -> QuoteTick:
    return QuoteTick(instrument_id=InstrumentId.from_str(inst), bid_price=Price.from_str(bid), ask_price=Price.from_str(ask),
                     bid_size=Quantity.from_str("100.0"), ask_size=Quantity.from_str("100.0"),
                     ts_event=1_700_000_000_000_000_000, ts_init=1_700_000_000_000_000_000)


def two_asset_engine():
    d, cache = make_fresh_dispatcher(starting_cash=D("80000"))  # + 0.4 BTC at 50,000 = equity 100,000
    quotes = {BTC: quote(BTC, "50000.00", "50010.00"), ETH: quote(ETH, "2500.00", "2501.00")}
    cache.quote.side_effect = lambda inst: quotes.get(str(inst))
    d.positions[BTC] = D("0.4")  # mandate A's coins
    engine = make_engine(d)
    engine.bridge = MagicMock()
    q = SimpleQuoteCache()
    q.set_quote(BTC, 50000.0, 50010.0)
    q.set_quote(ETH, 2500.0, 2501.0)
    return engine, q


@pytest.fixture
def db_url():
    with migrated_database("cryptonite_crossmandate") as url:
        yield url


@pytest.fixture
async def db(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await seed_paper_account(conn)
    try:
        yield pool
    finally:
        await pool.close()


async def make_mandate(pool, inst: str, budget: int = 30000):
    async with pool.acquire() as conn:
        mid = await conn.fetchval(
            """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct, autonomy_level)
               VALUES ($1, $2, ARRAY[$3], 25, 10, 3) RETURNING id""", f"m-{uuid.uuid4().hex[:6]}", budget, inst)
        m = await load_mandate(conn, mid)
        assert await ensure_mandate_instance(conn, m, ACCOUNT)
    return m


async def hold(pool, instance_id: str, inst: str, qty: str, px: str):
    cid = f"T-{uuid.uuid4().hex[:10]}"
    await pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity,
               filled_qty, avg_fill_price, status) VALUES ($1, $2, $3, $4, 'BUY', 'MARKET', $5, $5, $6, 'FILLED')""",
        cid, instance_id, ACCOUNT, inst, D(qty), D(px))
    await pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity, current_price, status)
           VALUES ($1, $2, $3, $4, 'LONG', $5, $6, $5, 'OPEN')""", uuid.uuid4().hex, ACCOUNT, instance_id, inst, D(px), D(qty))


async def queue(pool, m, payload):
    cid = uuid.uuid4()
    await pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id, payload,
               status, environment, account_id, mandate_id) VALUES ($1, $2, 'SUBMIT_ORDER', $3, 'ai:debate_worker', $4::jsonb, 'PENDING', 'PAPER', $5, $6)""",
        cid, f"t:{cid}", m.instance_id, json.dumps({**payload, "mandate_id": str(m.id)}), ACCOUNT, m.id)
    return cid


async def drain(pool, engine, quotes, cid):
    task = asyncio.create_task(command_poller(pool, engine, quotes))
    try:
        for _ in range(200):
            row = await pool.fetchrow("SELECT status, error_message FROM execution_commands WHERE command_id = $1", cid)
            if row["status"] in ("COMPLETED", "REJECTED"):
                return row["status"], row["error_message"]
            await asyncio.sleep(0.05)
        raise AssertionError("command not processed")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_two_mandates_cannot_together_exceed_the_btc_cap(db):
    a = await make_mandate(db, BTC)
    b = await make_mandate(db, ETH)
    await hold(db, a.instance_id, BTC, "0.4", "50000")  # A: $20,000 of BTC, inside its own mandate limits
    engine, quotes = two_asset_engine()  # equity ~100,000 -> cap $25,000
    eth_entry = {"instrument_id": "ETHUSDT", "side": "BUY", "quantity": "2.8",
                 "proposed_stop_loss": "2450"}  # ~$7,180 worst case, inside B's $7,500

    # No lake data: ETH counts at beta 1.0 -> 20,000 + 7,180 > 25,000
    status, reason = await drain(db, engine, quotes, await queue(db, b, dict(eth_entry)))
    assert status == "REJECTED" and reason.startswith("CROSS_MANDATE_EXPOSURE"), reason
    assert engine.dispatcher.open_orders == {} and engine.dispatcher.get_position_qty(ETH) == 0
    assert await db.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE kind IN ('BREACH', 'DEMOTED')") == 0
    async with db.acquire() as conn:
        cur = await load_mandate(conn, b.id)
    assert (cur.status, cur.autonomy_level) == (MandateStatus.ACTIVE, AutonomyLevel.L3)

    # With ETH's measured beta at 0.5 it fits: 20,000 + 3,590 < 25,000
    risk_overrides.STATE.betas = {"ETHUSDT": D("0.5")}
    status, reason = await drain(db, engine, quotes, await queue(db, b, dict(eth_entry)))
    assert (status, reason) == ("COMPLETED", None)
    assert engine.dispatcher.get_position_qty(ETH) == D("2.8")

    # F7 lowers the cap to 10%: new entries stop, exits still go through
    await db.execute(
        """INSERT INTO risk_limit_overrides (environment, account_id, cross_mandate_exposure_pct, updated_by, reason, override_version)
           VALUES ('PAPER', $1, 10, 'op', 'test the cap', 1)""", ACCOUNT)
    await risk_overrides.refresh_risk_limits(engine, db, env="PAPER")
    assert risk_overrides.STATE.cross_cap_pct == D("10")
    status, reason = await drain(db, engine, quotes, await queue(db, b, {**eth_entry, "quantity": "0.1"}))
    assert status == "REJECTED" and reason.startswith("CROSS_MANDATE_EXPOSURE")
    status, reason = await drain(db, engine, quotes, await queue(db, a, {"instrument_id": "BTCUSDT", "side": "SELL", "quantity": "0.1"}))
    assert (status, reason) == ("COMPLETED", None)
    assert await db.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE kind = 'BREACH'") == 0


async def test_betas_are_refreshed_from_the_lake_hourly(db):
    await make_mandate(db, ETH)
    rb = btc_returns(24 * 30)
    now_ns = int(datetime.datetime.now(datetime.UTC).timestamp()) // 3600 * 3600 * 10**9
    t0 = now_ns - len(rb) * H

    class Lake:
        calls = 0

        def query_bars_df(self, symbol, timeframe, start_ns, end_ns):
            Lake.calls += 1
            assert timeframe == "1h" and end_ns - start_ns == 30 * 86_400 * 10**9
            data = {"BTCUSDT": series(rb, t0=t0), "ETHUSDT": series([1.1 * r for r in rb], t0=t0)}.get(symbol, [])
            return pl.DataFrame({"ts_event_ns": [t for t, _ in data], "close": [p for _, p in data]})

    state = risk_overrides.RiskState()
    betas = await risk_overrides.refresh_betas(db, state, query_engine=Lake(), now_s=1000.0)
    assert betas == {"ETHUSDT": D("1.1")}
    calls = Lake.calls
    await risk_overrides.refresh_betas(db, state, query_engine=Lake(), now_s=1000.0 + 60)  # not due yet
    assert Lake.calls == calls


@pytest.fixture(autouse=True)
def _autotrade_switch_on(monkeypatch):
    """ADR 0011 D10 applies at runtime: with the master switch off a Veto window or Fully automatic mandate behaves as
    Ask me. These tests exercise the switch-on behaviour of those levels."""
    from cryptonite_execution import profile_lifecycle
    from cryptonite_orchestrator import mandate_gate, server

    async def on(conn):
        return True

    monkeypatch.setattr(profile_lifecycle, "autotrade_switch_on", on)
    monkeypatch.setattr(mandate_gate, "fresh_autotrade_switch_on", on)
    monkeypatch.setattr(server, "fresh_autotrade_switch_on", on)
