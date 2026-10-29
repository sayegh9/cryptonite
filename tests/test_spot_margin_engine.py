"""Spot-margin instrument aliasing and mandate decisions."""

import uuid
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import MagicMock

import asyncpg
import pytest
from cryptonite_execution.bridge import EventBridge, EventBridgeWorker
from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_execution.ingress import IngressHealthMonitor
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_execution.mandates import MandateExposure, MandateOrder, action_class, check_mandate
from cryptonite_execution.risk import OrderIntent
from cryptonite_schemas.identifiers import (
    parse_instrument_id,
    to_margin_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_schemas.mandate import ActionClass, Mandate, MandateReject, MandateStatus
from migrated_db import migrated_database
from test_paper_risk_guardrails import make_fresh_dispatcher

MARGIN = "BTCUSDT-MARGIN.BINANCE"
SPOT = "BTCUSDT.BINANCE"
ACCOUNT = "PAPER-ACCOUNT-001"
INSTANCE = "INST-1"


@pytest.fixture(scope="module")
def margin_db_url():
    with migrated_database("cryptonite_ci_margin_engine") as url:
        yield url


@pytest.fixture
async def margin_bridge(margin_db_url):
    pool = await asyncpg.create_pool(margin_db_url, min_size=1, max_size=2)
    strat_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE paper_execution_events, paper_trades, paper_positions, paper_orders, "
            "paper_margin_interest, paper_margin_accounts, paper_strategy_instances, paper_accounts CASCADE"
        )
        await conn.execute(
            "INSERT INTO strategies (id, slug, name, description, author) VALUES ($1, $2, $2, 'test', 'test')",
            strat_id, f"margin-{strat_id.hex[:8]}",
        )
        await conn.execute(
            """INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit,
                   default_parameters, parameter_schema) VALUES ($1, $1, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb)""",
            strat_id,
        )
        await conn.execute(
            """INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance,
                   total_equity, realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
               VALUES ($1, 'BINANCE', 'USDT', 10000, 10000, 10000, 0, 0, FALSE, now())""",
            ACCOUNT,
        )
        await conn.execute(
            """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id,
                   strategy_source_hash, risk_policy_hash, strategy_name, instrument_id, timeframe,
                   resolved_parameters, allocation_usd, status)
               VALUES ($1, $2, $3, 's', 'r', 'Margin', $4, '1m', '{}'::jsonb, 1000, 'RUNNING')""",
            INSTANCE, ACCOUNT, strat_id, MARGIN,
        )
    bridge = EventBridge(LatchedKillSwitch())
    worker = EventBridgeWorker(bridge, pool, engine_run_id=f"RUN-{uuid.uuid4().hex[:8]}")
    yield {"pool": pool, "bridge": bridge, "worker": worker, "dispatcher": make_fresh_dispatcher()[0]}
    await pool.close()


class QuoteCache:
    def __init__(self):
        self.requested = []

    def quote(self, instrument):
        self.requested.append(str(instrument))
        return SimpleNamespace(ts_init=1_000_000_000)


def test_margin_instrument_round_trips_and_parses_as_non_perpetual():
    assert to_margin_instrument_id("btcusdt") == MARGIN
    assert parse_instrument_id(MARGIN) == ("BTCUSDT", "BINANCE", False)
    assert to_spot_instrument_id(MARGIN) == SPOT


def test_margin_freshness_uses_the_fresh_underlying_spot_quote_and_health():
    monitor = IngressHealthMonitor(max_staleness_s=5)
    monitor.update_health(SPOT, "session", 1, 1_000_000_000, 1_000_000_000)
    cache = QuoteCache()

    assert monitor.is_fresh(MARGIN, cache, now_ns=2_000_000_000)
    assert cache.requested == [SPOT]


def make_mandate(status=MandateStatus.ACTIVE):
    return Mandate(id=uuid.uuid4(), name="margin", capital_budget_usd=D("10000"),
                   instruments=["BTCUSDT-MARGIN"], markets=["SPOT_MARGIN"], max_leverage=3,
                   max_position_pct=D("25"), max_drawdown_pct=D("20"), direction="BOTH", status=status)


@pytest.mark.parametrize("status", [MandateStatus.PAUSED, MandateStatus.STOPPING, MandateStatus.HALTED])
def test_spot_margin_entries_are_allowed_by_market_and_exits_remain_allowed_latched(status):
    entry = MandateOrder(MARGIN, "SELL", D("0.01"), D("50000"), order_type="LIMIT", leverage=2,
                         margin_mode="ISOLATED")
    assert action_class(entry) is ActionClass.ADD_RISK
    assert check_mandate(make_mandate(), entry, MandateExposure(), approval_ok=True, autotrade_on=True) is None

    close = MandateOrder(MARGIN, "BUY", D("0.05"), D("50000"), order_type="LIMIT", leverage=2,
                         margin_mode="ISOLATED", reduce_only=True)
    assert action_class(close) is ActionClass.REDUCE_RISK
    exposure = MandateExposure(instrument_qty=D("0.1"), instrument_side="SHORT")
    assert check_mandate(make_mandate(status), close, exposure,
                         approval_ok=False, autotrade_on=False) is None


def test_margin_entry_cannot_claim_kill_switch_exit_to_bypass_latch():
    dispatcher, _ = make_fresh_dispatcher()
    dispatcher.kill_switch.trip("test", "latched")
    entry = OrderIntent(
        client_order_id="M-ENTRY-KILL-FLAG", instance_id=INSTANCE, instrument_id=MARGIN,
        side="BUY", order_type="MARKET", quantity=D("0.01"), leverage=2,
        margin_mode="ISOLATED", stop_loss=D("47000"), require_stop=True,
        is_kill_switch_exit=True,
    )

    approved, reason, _ = dispatcher.evaluate_intent(entry)

    assert not approved and reason == "KILL_SWITCH_ENGAGED"
    assert dispatcher.is_entry_order({"is_margin": True, "reduce_only": False})


def test_margin_reduce_only_close_does_not_depend_on_pair_entry_settings():
    dispatcher, _ = make_fresh_dispatcher()
    entry = OrderIntent(
        client_order_id="M-OPEN-FOR-EXIT", instance_id=INSTANCE, instrument_id=MARGIN,
        side="BUY", order_type="MARKET", quantity=D("0.01"), leverage=2,
        margin_mode="ISOLATED", stop_loss=D("47000"), require_stop=True,
    )
    assert dispatcher.dispatch(entry, lambda _: None, None)[0]
    dispatcher.handle_fill("M-OPEN-FOR-EXIT", D("0.01"), D("50000"), D("0.5"),
                            side="BUY", instrument_id=MARGIN)
    dispatcher.margin_pair_settings.pop("BTCUSDT")
    dispatcher.kill_switch.trip("test", "latched")
    dispatcher.daily_loss_latched = True
    close = OrderIntent(
        client_order_id="M-CLOSE-NO-PAIR-SETTINGS", instance_id=INSTANCE, instrument_id=MARGIN,
        side="SELL", order_type="MARKET", quantity=D("0.005"), leverage=2,
        margin_mode="ISOLATED", reduce_only=True,
    )

    assert dispatcher.evaluate_intent(close) == (True, "APPROVED", D("0"))


def test_plain_spot_short_is_still_refused():
    dispatcher, _ = make_fresh_dispatcher()
    intent = OrderIntent(
        client_order_id="SPOT-SHORT", instance_id="INST-1", instrument_id=SPOT, side="SELL", order_type="MARKET",
        quantity=D("0.01"), stop_loss=D("55000"),
    )
    approved, reason, _ = dispatcher.evaluate_intent(intent)
    assert not approved
    assert reason.startswith("SPOT_INVARIANT_VIOLATION")


def test_margin_unsupported_pair_and_missing_side_cap_fail_closed():
    dispatcher, _ = make_fresh_dispatcher()
    unsupported = OrderIntent(
        client_order_id="UNSUPPORTED", instance_id="INST-1", instrument_id="DOGEUSDT-MARGIN.BINANCE",
        side="BUY", order_type="MARKET", quantity=D("0.01"), leverage=2,
        margin_mode="ISOLATED", stop_loss=D("0.09"), require_stop=True,
    )
    ok, reason, _ = dispatcher.evaluate_intent(unsupported)
    assert not ok and reason == "MARGIN_PAIR_NOT_SUPPORTED"

    pair_settings = dispatcher.margin_pair_settings["BTCUSDT"]
    pair_settings["max_borrow_USDT"] = D("0")
    no_quote_borrow = OrderIntent(
        client_order_id="NO-CAP", instance_id="INST-1", instrument_id=MARGIN, side="BUY", order_type="MARKET",
        quantity=D("0.01"), leverage=2, margin_mode="ISOLATED", stop_loss=D("47000"), require_stop=True,
    )
    ok, reason, _ = dispatcher.evaluate_intent(no_quote_borrow)
    assert not ok and reason == "MARGIN_BORROW_LIMIT"


def test_margin_reduce_only_wrong_side_is_refused():
    close = MandateOrder(MARGIN, "SELL", D("0.05"), D("50000"), order_type="LIMIT", leverage=2,
                         margin_mode="ISOLATED", reduce_only=True)
    exposure = MandateExposure(instrument_qty=D("0.1"), instrument_side="SHORT")
    assert check_mandate(make_mandate(), close, exposure,
                         approval_ok=True, autotrade_on=True) is MandateReject.POSITION_NOT_OWNED


def test_margin_fill_moves_only_collateral_and_partial_close_conserves_exact_cash():
    dispatcher, cache = make_fresh_dispatcher()
    intent = OrderIntent(
        client_order_id="M-LONG", instance_id="INST-1", instrument_id=MARGIN, side="BUY", order_type="MARKET",
        quantity=D("0.01"), leverage=2, margin_mode="ISOLATED", stop_loss=D("47000"), require_stop=True,
    )
    ok, reason, reserve = dispatcher.evaluate_intent(intent)
    assert ok, reason
    assert reserve == D("250.55010")
    assert cache.quote.call_args.args[0].__str__() == SPOT

    dispatcher.dispatch(intent, lambda _: None, None)
    dispatcher.handle_fill("M-LONG", D("0.01"), D("50000"), D("0.5"), side="BUY", instrument_id=MARGIN)
    assert dispatcher.cash_balance == D("9749.5")
    assert dispatcher.margin_accounts[MARGIN].borrowed_quote == D("250")
    assert dispatcher.get_margin_position_qty(MARGIN) == D("0.01")

    dispatcher.handle_fill("PARTIAL", D("0.005"), D("51000"), D("0.255"), side="SELL", instrument_id=MARGIN)
    assert dispatcher.margin_accounts[MARGIN].borrowed_quote == D("125")
    assert dispatcher.margin_accounts[MARGIN].base_qty == D("0.005")
    dispatcher.handle_fill("CLOSE", D("0.005"), D("52000"), D("0.26"), side="SELL", instrument_id=MARGIN)
    assert dispatcher.cash_balance == D("10013.985")
    assert MARGIN not in dispatcher.margin_accounts


def test_margin_entry_requires_a_stop():
    dispatcher, _ = make_fresh_dispatcher()
    no_stop = OrderIntent(
        client_order_id="M-NO-STOP", instance_id="INST-1", instrument_id=MARGIN, side="BUY", order_type="MARKET",
        quantity=D("0.01"), leverage=2, margin_mode="ISOLATED", require_stop=True,
    )
    ok, reason, _ = dispatcher.evaluate_intent(no_stop)
    assert not ok and reason == "STOP_REQUIRED"


def test_kill_switch_flattens_margin_at_the_underlying_spot_quote():
    engine = PaperTradingEngine(
        account_id=ACCOUNT, trader_id="TRADER-1", engine_run_id="RUN-MARGIN-1", pg_pool=None, redis_client=None,
    )
    dispatcher, cache = make_fresh_dispatcher()
    engine.dispatcher = dispatcher
    engine.kill_switch = dispatcher.kill_switch
    dispatcher.node_cache = cache
    intent = OrderIntent(
        client_order_id="M-KILL", instance_id=INSTANCE, instrument_id=MARGIN, side="BUY", order_type="MARKET",
        quantity=D("0.01"), leverage=2, margin_mode="ISOLATED", stop_loss=D("47000"), require_stop=True,
    )
    ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
    assert ok, reason
    dispatcher.handle_fill("M-KILL", D("0.01"), D("50000"), D("0.5"), side="BUY", instrument_id=MARGIN)

    engine.mailbox.push({"command_type": "TRIP_KILL_SWITCH", "requester_id": "test", "payload": {}})
    engine.process_mailbox_commands()
    assert engine.kill_switch.is_latched
    assert MARGIN not in dispatcher.margin_accounts
    assert cache.quote.call_args.args[0].__str__() == SPOT


def test_short_margin_repay_returns_collateral_and_realized_gain_as_exact_decimal():
    dispatcher, _ = make_fresh_dispatcher()
    intent = OrderIntent(
        client_order_id="M-SHORT", instance_id="INST-1", instrument_id=MARGIN, side="SELL", order_type="MARKET",
        quantity=D("0.01"), leverage=2, margin_mode="ISOLATED", stop_loss=D("55000"), require_stop=True,
    )
    ok, reason, reserve = dispatcher.evaluate_intent(intent)
    assert ok, reason
    assert reserve == D("500.5")
    dispatcher.dispatch(intent, lambda _: None, None)
    dispatcher.handle_fill("M-SHORT", D("0.01"), D("50000"), D("0.5"), side="SELL", instrument_id=MARGIN)
    assert dispatcher.margin_accounts[MARGIN].borrowed_base == D("0.01")
    assert dispatcher.cash_balance == D("9499.5")

    dispatcher.handle_fill("M-CLOSE", D("0.01"), D("45000"), D("0.45"), side="BUY", instrument_id=MARGIN)
    assert dispatcher.cash_balance == D("10049.05")
    assert MARGIN not in dispatcher.margin_accounts


async def test_margin_bridge_persists_exact_account_and_position_state_idempotently(margin_bridge):
    dispatcher = margin_bridge["dispatcher"]
    bridge, worker, pool = margin_bridge["bridge"], margin_bridge["worker"], margin_bridge["pool"]

    async def fill(cid, side, qty, price, fee, *, reduce_only=False, native_id=None):
        qty, price, fee = D(qty), D(price), D(fee)
        if not reduce_only:
            intent = OrderIntent(
                client_order_id=cid, instance_id=INSTANCE, instrument_id=MARGIN, side=side,
                order_type="MARKET", quantity=qty, leverage=2, margin_mode="ISOLATED",
                stop_loss=D("47000") if side == "BUY" else D("55000"), require_stop=True,
            )
            ok, reason = dispatcher.dispatch(intent, MagicMock(), MagicMock())
            assert ok, reason
        realized = dispatcher.handle_fill(cid, qty, price, fee, side=side, instrument_id=MARGIN)
        event = {
            "event_type": "ORDER_FILLED", "event_version": 1, "event_id": uuid.uuid4(),
            "native_event_id": native_id or str(uuid.uuid4()), "client_order_id": cid,
            "account_id": ACCOUNT, "instance_id": INSTANCE, "instrument_id": MARGIN, "side": side,
            "price": str(price), "quantity": str(qty), "commission": str(fee), "filled_qty": str(qty),
            "avg_fill_price": str(price), "status": "FILLED",
            **dispatcher.fill_event_fields(MARGIN, realized, price),
        }
        bridge.enqueue_event(event)
        assert await worker.drain_and_commit_batch() == 1
        return event

    duplicate_source = await fill("M-DB-OPEN", "BUY", "0.01", "50000", "0.5", native_id="NATIVE-M-OPEN")
    account = await pool.fetchrow("SELECT * FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT'", ACCOUNT)
    position = await pool.fetchrow("SELECT * FROM paper_positions WHERE instrument_id=$1 AND status='OPEN'", MARGIN)
    paper_account = await pool.fetchrow("SELECT * FROM paper_accounts WHERE account_id=$1", ACCOUNT)
    assert (account["base_qty"], account["borrowed_quote"], account["collateral_usdt"]) == (
        D("0.01"), D("250"), D("250"),
    )
    assert (position["side"], position["quantity"], position["margin_mode"]) == ("LONG", D("0.01"), "ISOLATED")
    assert paper_account["cash_balance"] == D("9749.5")

    bridge.enqueue_event(duplicate_source)
    assert await worker.drain_and_commit_batch() == 1
    assert await pool.fetchval("SELECT count(*) FROM paper_trades WHERE client_order_id='M-DB-OPEN'") == 1

    await fill("M-DB-PARTIAL", "SELL", "0.005", "51000", "0.255", reduce_only=True)
    account = await pool.fetchrow("SELECT * FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT'", ACCOUNT)
    position = await pool.fetchrow("SELECT * FROM paper_positions WHERE instrument_id=$1 AND status='OPEN'", MARGIN)
    assert (account["base_qty"], account["borrowed_quote"], position["quantity"]) == (
        D("0.005"), D("125"), D("0.005"),
    )

    await fill("M-DB-CLOSE", "SELL", "0.005", "52000", "0.26", reduce_only=True)
    account = await pool.fetchrow("SELECT * FROM paper_margin_accounts WHERE account_id=$1 AND pair='BTCUSDT'", ACCOUNT)
    position = await pool.fetchrow("SELECT * FROM paper_positions WHERE instrument_id=$1", MARGIN)
    paper_account = await pool.fetchrow("SELECT * FROM paper_accounts WHERE account_id=$1", ACCOUNT)
    assert (account["status"], account["base_qty"], account["borrowed_quote"]) == ("CLOSED", D("0"), D("0"))
    assert (position["status"], position["side"], position["quantity"]) == ("CLOSED", "FLAT", D("0"))
    assert paper_account["cash_balance"] == dispatcher.cash_balance == D("10013.985")


def test_margin_resting_stop_is_reduce_only_and_fills_on_the_underlying_spot_mark():
    dispatcher, _ = make_fresh_dispatcher()
    intent = OrderIntent(
        client_order_id="M-STOP-LONG", instance_id="INST-1", instrument_id=MARGIN, side="BUY", order_type="MARKET",
        quantity=D("0.01"), leverage=2, margin_mode="ISOLATED", stop_loss=D("47000"), require_stop=True,
    )
    dispatcher.dispatch(intent, lambda _: None, None)
    dispatcher.handle_fill("M-STOP-LONG", D("0.01"), D("50000"), D("0.5"), side="BUY", instrument_id=MARGIN)
    ok, reason = dispatcher.place_resting_stop("M-STOP", MARGIN, D("49000"), "INST-1")
    assert ok, reason
    fills, cancelled = dispatcher.triggered_stops({SPOT: D("48000")})
    assert cancelled == []
    assert [(f.side, f.quantity, f.price) for f in fills] == [("SELL", D("0.01"), D("48000"))]
    dispatcher.handle_fill("M-STOP", D("0.01"), D("48000"), D("0"), side="SELL", instrument_id=MARGIN)
    assert MARGIN not in dispatcher.margin_accounts
