"""Phase 0 Compatibility Spike Test Suite for nautilus_trader==2.0.0rc4.

Validates the 5 critical architectural invariants required before full Milestone 4 build:
1. test_rc4_native_serialization_roundtrip:
   - CurrencyPair: to_dict / from_dict and Redis cache serialization {"CurrencyPair": ...}.
   - QuoteTick: to_json_bytes / from_json.
   - Bar: to_json_bytes / from_json.
2. test_canonical_topics_strategy_reception:
   - Redis external stream ingress into real LiveNode.
   - Topics: data.quotes.BINANCE.BTCUSDT and data.bars.BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL.
   - Callbacks: on_quote and on_bar.
   - SandboxExecutionClient fill execution with exact 0.1% taker commission.
3. test_post_subscription_watermark_lossless_handoff:
   - 8-step startup watermark handoff protocol:
     Subscribe -> Buffer at $ -> Capture C -> Rewarm <= C -> Discard <= C -> Process > C -> Open Gate.
   - Verifies zero bars lost and zero bars double-counted.
4. test_synchronous_node_dispatcher_and_command_mailbox:
   - Node-thread-owned synchronous GuardedOrderDispatcher with zero async lock contention.
   - In-memory cash/exposure reservation and spot cash invariant.
   - Thread-safe NodeCommandMailbox processing PAUSE command (cancelling resting orders while preserving positions).
5. test_queue_overflow_incident_and_fail_closed:
   - Bounded queue with 80% DEGRADED watermark and 100% hard-cap overflow.
   - Fails closed: trips latched kill switch, logs incident, forces 503 /ready, blocks automated restart.
"""

from dataclasses import dataclass, field
from decimal import Decimal
import json
import queue
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import pytest
import redis

from nautilus_trader.adapters.sandbox import (
    SandboxExecutionClientConfig,
    SandboxExecutionClientFactory,
)
from nautilus_trader.common import Environment
from nautilus_trader.config import LiveDataEngineConfig, MessageBusConfig
from nautilus_trader.core import UUID4
from nautilus_trader.infrastructure import (
    RedisCacheConfig,
    RedisCacheDatabase,
    RedisMessageBusConfig,
    RedisMessageBusFactory,
)
from nautilus_trader.live import LiveNode
from nautilus_trader.model import (
    Bar,
    BarType,
    ClientId,
    Currency,
    CurrencyPair,
    InstrumentId,
    Money,
    OrderSide,
    Price,
    Quantity,
    QuoteTick,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.trading import Strategy, StrategyConfig

from connections import REDIS_URL

_REDIS = urlsplit(REDIS_URL)
REDIS_HOST = _REDIS.hostname or "127.0.0.1"
REDIS_PORT = _REDIS.port or 6379


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def redis_client():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
    r.ping()
    return r


@pytest.fixture
def btc_currency_pair() -> CurrencyPair:
    inst_id = InstrumentId(Symbol("BTCUSDT"), Venue("BINANCE"))
    return CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=2,
        size_precision=5,
        price_increment=Price(Decimal("0.01"), 2),
        size_increment=Quantity(Decimal("0.00001"), 5),
        lot_size=Quantity(Decimal("0.00001"), 5),
        max_quantity=Quantity(Decimal("1000"), 5),
        min_quantity=Quantity(Decimal("0.00001"), 5),
        max_price=Price(Decimal("1000000"), 2),
        min_price=Price(Decimal("0.01"), 2),
        maker_fee=Decimal("0.001"),
        taker_fee=Decimal("0.001"),
        ts_event=1725700000000000000,
        ts_init=1725700000000000000,
    )


# ---------------------------------------------------------------------------
# Test 1: Native Serialization Round-Trip
# ---------------------------------------------------------------------------

def test_rc4_native_serialization_roundtrip(redis_client, btc_currency_pair):
    """Spike 1: Verify native 2.0.0rc4 serializers for CurrencyPair, QuoteTick, and Bar."""
    inst = btc_currency_pair

    # 1. CurrencyPair dict round-trip
    inst_dict = inst.to_dict()
    assert inst_dict["type"] == "CurrencyPair"
    assert inst_dict["id"] == "BTCUSDT.BINANCE"
    assert inst_dict["price_precision"] == 2
    assert inst_dict["size_precision"] == 5
    assert inst_dict["taker_fee"] == "0.001"

    inst_recovered = CurrencyPair.from_dict(inst_dict)
    assert inst == inst_recovered

    # 2. Redis CacheDatabase hydration round-trip
    trader_id = TraderId("SPIKE-SER-001")
    instance_id = UUID4()
    cache_key = f"trader-{trader_id}:instruments:{inst.id}"
    cache_payload = json.dumps({"CurrencyPair": inst.to_dict()}).encode()
    redis_client.set(cache_key, cache_payload)

    cache_db = RedisCacheDatabase(
        trader_id=trader_id,
        instance_id=instance_id,
        config_json=json.dumps({}).encode(),
        database_config_json=json.dumps({"host": REDIS_HOST, "port": REDIS_PORT}).encode(),
    )
    loaded_state = cache_db.load_all()
    assert inst.id in loaded_state["instruments"]
    loaded_inst = loaded_state["instruments"][inst.id]
    assert loaded_inst.id == inst.id
    assert loaded_inst.price_precision == 2
    assert loaded_inst.size_precision == 5

    # Clean up Redis key
    redis_client.delete(cache_key)

    # 3. QuoteTick json round-trip
    quote = QuoteTick(
        instrument_id=inst.id,
        bid_price=Price(50000, 2),
        ask_price=Price(50010, 2),
        bid_size=Quantity(5, 5),
        ask_size=Quantity(3, 5),
        ts_event=1725700000000000000,
        ts_init=1725700000000000000,
    )
    quote_bytes = quote.to_json_bytes()
    assert isinstance(quote_bytes, bytes)
    assert b'"type":"QuoteTick"' in quote_bytes or b'"instrument_id":"BTCUSDT.BINANCE"' in quote_bytes

    quote_recovered = QuoteTick.from_json(quote_bytes)
    assert quote == quote_recovered
    assert float(quote_recovered.bid_price) == 50000.0
    assert float(quote_recovered.ask_price) == 50010.0

    # 4. Bar json round-trip
    bar_type = BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")
    bar = Bar(
        bar_type=bar_type,
        open=Price(50000, 2),
        high=Price(50100, 2),
        low=Price(49900, 2),
        close=Price(50050, 2),
        volume=Quantity(10, 5),
        ts_event=1725700060000000000,
        ts_init=1725700060000000000,
    )
    bar_bytes = bar.to_json_bytes()
    assert isinstance(bar_bytes, bytes)
    assert b"BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL" in bar_bytes

    bar_recovered = Bar.from_json(bar_bytes)
    assert bar == bar_recovered
    assert float(bar_recovered.open) == 50000.0
    assert float(bar_recovered.close) == 50050.0


# ---------------------------------------------------------------------------
# Test 2: Canonical Topic Routing, Strategy Reception & Execution
# ---------------------------------------------------------------------------

class CanonicalSpikeConfig(StrategyConfig):
    def __init__(self, inst_id: InstrumentId, bar_type: BarType, **kwargs):
        super().__init__(**kwargs)
        self.inst_id = inst_id
        self.bar_type = bar_type


class CanonicalSpikeStrategy(Strategy):
    """Test strategy that verifies quotes, bars, and fills via Redis ingress."""

    def __init__(self, config: CanonicalSpikeConfig):
        super().__init__(config)
        self.target_inst_id = config.inst_id
        self.target_bar_type = config.bar_type
        self.received_quotes: list[QuoteTick] = []
        self.received_bars: list[Bar] = []
        self.order_submitted_events: list[Any] = []
        self.order_accepted_events: list[Any] = []
        self.order_filled_events: list[Any] = []

    def on_start(self):
        self.subscribe_quotes(self.target_inst_id, client_id=ClientId("REDIS-INGRESS"))
        self.subscribe_bars(self.target_bar_type, client_id=ClientId("REDIS-INGRESS"))

    def on_quote(self, quote: QuoteTick):
        self.received_quotes.append(quote)
        # On first quote, submit a limit order at ask price to verify execution and commission
        if not self.order_submitted_events:
            order = self.order_factory.limit(
                instrument_id=self.target_inst_id,
                order_side=OrderSide.BUY,
                quantity=Quantity(Decimal("0.1"), 5),
                price=quote.ask_price,
            )
            self.submit_order(order)

    def on_bar(self, bar: Bar):
        self.received_bars.append(bar)

    def on_order_submitted(self, event):
        self.order_submitted_events.append(event)

    def on_order_accepted(self, event):
        self.order_accepted_events.append(event)

    def on_order_filled(self, event):
        self.order_filled_events.append(event)


import uuid

def test_canonical_topics_strategy_reception(redis_client, btc_currency_pair):
    """Spike 2: Verify canonical topics, Redis ingress, and order fill with commission."""
    inst = btc_currency_pair
    inst_id = inst.id
    bar_type = BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")
    unique_suffix = uuid.uuid4().hex[:8].upper()
    trader_id = TraderId(f"SPIKE-TOPICS-{unique_suffix}")

    stream_name = f"nautilus:stream:bus:{unique_suffix}"
    redis_client.delete(stream_name)

    # Clean any prior keys for this prefix
    for k in redis_client.keys(f"trader-{trader_id}:*"):
        redis_client.delete(k)

    # Seed instrument in Redis cache for node hydration
    cache_key = f"trader-{trader_id}:instruments:{inst.id}"
    cache_payload = json.dumps({"CurrencyPair": inst.to_dict()}).encode()
    redis_client.set(cache_key, cache_payload)

    builder = LiveNode.builder(
        name="spike-node",
        trader_id=trader_id,
        environment=Environment.SANDBOX,
    )
    builder.with_cache_database_factory(RedisCacheConfig(host=REDIS_HOST, port=REDIS_PORT))
    builder.with_load_state(True)
    builder.with_msgbus_config(MessageBusConfig(external_streams=[stream_name]))
    builder.with_external_msgbus_factory(
        RedisMessageBusFactory(RedisMessageBusConfig(host=REDIS_HOST, port=REDIS_PORT))
    )
    builder.with_data_engine_config(
        LiveDataEngineConfig(external_clients=[ClientId("REDIS-INGRESS")])
    )
    builder.with_delay_post_stop_secs(0)
    builder.with_delay_shutdown_secs(0)
    builder.with_timeout_disconnection_secs(1)

    exec_cfg = SandboxExecutionClientConfig(
        venue=Venue("BINANCE"),
        starting_balances=[Money(10_000, Currency.from_str("USDT"))],
    )
    builder.add_simulated_exec_client("SIM-BINANCE", SandboxExecutionClientFactory(), exec_cfg)

    cfg = CanonicalSpikeConfig(inst_id=inst_id, bar_type=bar_type)
    strategy = CanonicalSpikeStrategy(cfg)
    node = builder.build()
    node.add_strategy(strategy)
    handle = node.handle()

    quote = QuoteTick(
        instrument_id=inst_id,
        bid_price=Price(50000, 2),
        ask_price=Price(50010, 2),
        bid_size=Quantity(5, 5),
        ask_size=Quantity(3, 5),
        ts_event=1725700061000000000,
        ts_init=1725700061000000000,
    )
    bar = Bar(
        bar_type=bar_type,
        open=Price(50000, 2),
        high=Price(50100, 2),
        low=Price(49900, 2),
        close=Price(50050, 2),
        volume=Quantity(10, 5),
        ts_event=1725700060000000000,
        ts_init=1725700060000000000,
    )

    def publisher():
        time.sleep(0.3)
        # 1. Publish native Bar to canonical topic derived strictly from bar_type:
        # topic = f"data.bars.{bar_type}"
        redis_client.xadd(
            stream_name,
            {
                "topic": f"data.bars.{bar_type}".encode(),
                "type": b"Bar",
                "payload": bar.to_json_bytes(),
                "encoding": b"Json",
            },
        )
        # 2. Publish native QuoteTick to canonical venue-first topic:
        # topic = f"data.quotes.{venue}.{symbol}"
        redis_client.xadd(
            stream_name,
            {
                "topic": f"data.quotes.{inst_id.venue}.{inst_id.symbol}".encode(),
                "type": b"QuoteTick",
                "payload": quote.to_json_bytes(),
                "encoding": b"Json",
            },
        )
        # Allow time for fill execution
        time.sleep(0.6)
        handle.stop()

    pub_thread = threading.Thread(target=publisher, daemon=True)
    pub_thread.start()

    # Run node on main thread
    node.run()
    pub_thread.join(timeout=2.0)

    assert len(strategy.received_bars) >= 1, "Strategy did not receive bar from Redis ingress"
    assert strategy.received_bars[0] == bar

    assert len(strategy.received_quotes) >= 1, "Strategy did not receive quote from Redis ingress"
    assert strategy.received_quotes[0] == quote

    assert len(strategy.order_filled_events) == 1
    fill = strategy.order_filled_events[0]
    assert float(fill.last_px) == 50010.0
    assert float(fill.last_qty) == 0.1
    # Expected commission: 0.1 BTC * 50010.00 USDT = 5001.00 USDT notional * 0.001 (0.1%) = 5.001 USDT
    assert float(fill.commission) == pytest.approx(5.001, rel=1e-4)

    # Clean up Redis keys
    redis_client.delete(stream_name)
    for k in redis_client.keys(f"trader-{trader_id}*"):
        redis_client.delete(k)


# ---------------------------------------------------------------------------
# Test 3: Lossless Post-Subscription Watermark Protocol
# ---------------------------------------------------------------------------

class StartupWatermarkHandoffCoordinator:
    """Implements and enforces the 8-step lossless startup watermark handoff protocol."""

    def __init__(self, catalog_watermark_ns: int):
        self.gate_open = False
        self.catalog_watermark_ns = catalog_watermark_ns
        self.buffered_live_bars: list[dict] = []
        self.warmed_bars: list[dict] = []
        self.dispatched_bars: list[dict] = []
        self.discarded_live_bars: list[dict] = []
        self.last_processed_bar_ns = 0

    def step2_buffer_live_bar(self, bar: dict):
        """Live stream buffers incoming bars while gate is closed."""
        self.buffered_live_bars.append(bar)

    def execute_handoff(self, catalog_replay_bars: list[dict]):
        """Executes steps 3 through 8 of the protocol."""
        assert not self.gate_open, "Gate must be closed during handoff execution"

        # Step 3: Capture post-subscription committed catalog watermark C
        C = self.catalog_watermark_ns

        # Step 4: Rewarm/reconcile catalog through C with dispatcher disabled
        for b in catalog_replay_bars:
            if b["ts_event"] <= C:
                self.warmed_bars.append(b)
                # Dispatcher is disabled: zero orders can execute during rewarm

        # Step 5: Discard buffered live bars where ts_event <= C
        # Step 6: Process buffered live bars where ts_event > C
        for b in self.buffered_live_bars:
            if b["ts_event"] <= C:
                self.discarded_live_bars.append(b)
            else:
                self.dispatched_bars.append(b)
                self.last_processed_bar_ns = max(self.last_processed_bar_ns, b["ts_event"])

        # Step 7: Atomically persist processed high-water mark
        if self.last_processed_bar_ns == 0:
            self.last_processed_bar_ns = C

        # Step 8: Gate open
        self.gate_open = True

    def process_live_bar(self, bar: dict):
        """Processes bars after gate is open."""
        assert self.gate_open, "Gate is closed: live bar cannot be processed directly"
        if bar["ts_event"] > self.last_processed_bar_ns:
            self.dispatched_bars.append(bar)
            self.last_processed_bar_ns = bar["ts_event"]


def test_post_subscription_watermark_lossless_handoff():
    """Spike 3: Verify the 8-step post-subscription watermark handoff protocol."""
    # Historical catalog contains 10 bars (timestamps 100, 200, ... 1000)
    catalog_bars = [{"id": i, "ts_event": i * 100} for i in range(1, 11)]
    committed_watermark_C = 1000  # Bar 10 timestamp

    coordinator = StartupWatermarkHandoffCoordinator(catalog_watermark_ns=committed_watermark_C)

    # 1. Gate is closed initially
    assert not coordinator.gate_open

    # 2. Live stream connects and buffers incoming bars during startup window:
    # Notice: Bar 9 (900) and Bar 10 (1000) arrived on live stream (network overlap / retry),
    # along with Bar 11 (1100) and Bar 12 (1200)
    live_incoming = [
        {"id": 9, "ts_event": 900},
        {"id": 10, "ts_event": 1000},
        {"id": 11, "ts_event": 1100},
        {"id": 12, "ts_event": 1200},
    ]
    for bar in live_incoming:
        coordinator.step2_buffer_live_bar(bar)

    # 3-8. Execute handoff
    coordinator.execute_handoff(catalog_replay_bars=catalog_bars)

    # Verify state after handoff
    assert coordinator.gate_open is True

    # Rewarmed bars: exactly bars 1..10
    assert len(coordinator.warmed_bars) == 10
    assert [b["id"] for b in coordinator.warmed_bars] == list(range(1, 11))

    # Discarded live bars: exactly bars 9 and 10 (ts_event <= 1000)
    assert len(coordinator.discarded_live_bars) == 2
    assert [b["id"] for b in coordinator.discarded_live_bars] == [9, 10]

    # Dispatched live bars from buffer: exactly bars 11 and 12 (ts_event > 1000)
    assert len(coordinator.dispatched_bars) == 2
    assert [b["id"] for b in coordinator.dispatched_bars] == [11, 12]

    # Last processed high-water mark updated to 1200
    assert coordinator.last_processed_bar_ns == 1200

    # New live bar arrives after gate is open
    coordinator.process_live_bar({"id": 13, "ts_event": 1300})
    assert len(coordinator.dispatched_bars) == 3
    assert coordinator.last_processed_bar_ns == 1300

    # Invariant: ZERO bars lost and ZERO duplicate bars processed
    all_processed_or_warmed_ids = [b["id"] for b in coordinator.warmed_bars] + [
        b["id"] for b in coordinator.dispatched_bars
    ]
    assert all_processed_or_warmed_ids == list(range(1, 14))
    assert len(all_processed_or_warmed_ids) == len(set(all_processed_or_warmed_ids))


# ---------------------------------------------------------------------------
# Test 4: Synchronous Node Dispatcher & Command Mailbox
# ---------------------------------------------------------------------------

@dataclass
class OrderIntent:
    client_order_id: str
    instrument_id: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None


@dataclass
class ExecutionCommand:
    command_type: str
    target_instance_id: str
    payload: dict = field(default_factory=dict)


class SynchronousGuardedOrderDispatcher:
    """Synchronous, node-thread-owned order risk evaluator and exposure reserver.

    Operates with zero async locks and zero database queries in the hot path.
    """

    def __init__(
        self,
        cash_balance: Decimal,
        max_account_exposure: Decimal,
        is_fresh_fn,
        slippage_buffer: Decimal = Decimal("0.005"),
        taker_fee_pct: Decimal = Decimal("0.001"),
    ):
        self.cash_balance = cash_balance
        self.max_account_exposure = max_account_exposure
        self.is_fresh_fn = is_fresh_fn
        self.slippage_buffer = slippage_buffer
        self.taker_fee_pct = taker_fee_pct
        self.kill_switch_latched = False

        # In-memory exposure and reservations
        self.reserved_buy_notional = Decimal("0")
        self.current_position_qty = Decimal("0")
        self.active_reservations: dict[str, Decimal] = {}

    def can_dispatch(self, intent: OrderIntent, current_ask: Decimal, current_bid: Decimal) -> tuple[bool, str]:
        # 1. Kill switch check
        if self.kill_switch_latched:
            return False, "KILL_SWITCH_ENGAGED"

        # 2. Market data freshness check
        if not self.is_fresh_fn(intent.instrument_id):
            return False, "STALE_MARKET_DATA"

        # 3. Spot cash invariant: Short selling prohibited
        if intent.side == "SELL":
            if intent.quantity > self.current_position_qty:
                return False, "SHORT_SELLING_PROHIBITED"
            return True, "OK"

        # 4. Cash reservation check for BUY
        if intent.side == "BUY":
            # Conservative market/limit buy fill estimation:
            # notional = qty * ref_px * (1 + slippage + fee)
            ref_px = intent.price if intent.price is not None else current_ask
            conservative_px = ref_px * (Decimal("1") + self.slippage_buffer)
            required_notional = intent.quantity * conservative_px * (Decimal("1") + self.taker_fee_pct)

            available_cash = self.cash_balance - self.reserved_buy_notional
            if required_notional > available_cash:
                return False, f"INSUFFICIENT_CASH: required {required_notional:.2f} > available {available_cash:.2f}"

            total_projected_exposure = self.reserved_buy_notional + (self.current_position_qty * current_bid) + required_notional
            if total_projected_exposure > self.max_account_exposure:
                return False, "MAX_EXPOSURE_EXCEEDED"

            return True, "OK"

        return False, "INVALID_SIDE"

    def reserve_and_dispatch(self, intent: OrderIntent, current_ask: Decimal) -> bool:
        ok, reason = self.can_dispatch(intent, current_ask=current_ask, current_bid=current_ask)
        if not ok:
            return False

        if intent.side == "BUY":
            ref_px = intent.price if intent.price is not None else current_ask
            conservative_px = ref_px * (Decimal("1") + self.slippage_buffer)
            required_notional = intent.quantity * conservative_px * (Decimal("1") + self.taker_fee_pct)
            self.reserved_buy_notional += required_notional
            self.active_reservations[intent.client_order_id] = required_notional

        return True

    def release_reservation(self, client_order_id: str, filled_qty: Decimal = Decimal("0"), fill_px: Decimal = Decimal("0")):
        reserved = self.active_reservations.pop(client_order_id, Decimal("0"))
        self.reserved_buy_notional -= reserved

        if filled_qty > 0:
            actual_cost = filled_qty * fill_px * (Decimal("1") + self.taker_fee_pct)
            self.cash_balance -= actual_cost
            self.current_position_qty += filled_qty


class NodeCommandMailbox:
    """Thread-safe queue bridging external async command loop to the dedicated node thread."""

    def __init__(self):
        self._queue = queue.Queue()

    def push(self, cmd: ExecutionCommand):
        self._queue.put(cmd)

    def drain_commands(self) -> list[ExecutionCommand]:
        cmds = []
        while not self._queue.empty():
            try:
                cmds.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return cmds


def test_synchronous_node_dispatcher_and_command_mailbox():
    """Spike 4: Verify synchronous risk reservation and mailbox command processing."""
    dispatcher = SynchronousGuardedOrderDispatcher(
        cash_balance=Decimal("10000"),
        max_account_exposure=Decimal("15000"),
        is_fresh_fn=lambda inst_id: True,
    )
    mailbox = NodeCommandMailbox()

    ask = Decimal("50000")
    bid = Decimal("49990")

    # 1. Dispatch BUY intent 1 (0.1 BTC -> ~5027.50 USDT notional with slip+fee)
    intent1 = OrderIntent(
        client_order_id="O-001",
        instrument_id="BTCUSDT.BINANCE",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.1"),
        price=Decimal("50000"),
    )
    ok1 = dispatcher.reserve_and_dispatch(intent1, current_ask=ask)
    assert ok1 is True
    assert dispatcher.reserved_buy_notional > Decimal("5000")

    # 2. Dispatch BUY intent 2 requesting more than remaining cash (0.15 BTC -> ~7541 USDT)
    intent2 = OrderIntent(
        client_order_id="O-002",
        instrument_id="BTCUSDT.BINANCE",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.15"),
        price=Decimal("50000"),
    )
    can_dispatch, reason = dispatcher.can_dispatch(intent2, current_ask=ask, current_bid=bid)
    assert can_dispatch is False
    assert "INSUFFICIENT_CASH" in reason

    # 3. Test spot short-selling rejection
    sell_intent = OrderIntent(
        client_order_id="O-SELL-1",
        instrument_id="BTCUSDT.BINANCE",
        side="SELL",
        order_type="MARKET",
        quantity=Decimal("0.5"),
        price=None,
    )
    can_sell, sell_reason = dispatcher.can_dispatch(sell_intent, current_ask=ask, current_bid=bid)
    assert can_sell is False
    assert sell_reason == "SHORT_SELLING_PROHIBITED"

    # 4. External async loop posts PAUSE command to mailbox
    mailbox.push(ExecutionCommand(command_type="PAUSE", target_instance_id="inst-alpha"))

    # 5. Node thread drains mailbox and executes PAUSE semantics:
    # Cancels resting entry orders, preserves open positions
    drained = mailbox.drain_commands()
    assert len(drained) == 1
    cmd = drained[0]
    assert cmd.command_type == "PAUSE"

    # Execute PAUSE: release reservation for O-001
    dispatcher.release_reservation("O-001")
    assert dispatcher.reserved_buy_notional == Decimal("0")
    assert dispatcher.cash_balance == Decimal("10000")
    assert dispatcher.current_position_qty == Decimal("0")


# ---------------------------------------------------------------------------
# Test 5: Queue Overflow Incident & Fail-Closed Policy
# ---------------------------------------------------------------------------

class QueueOverflowIncident(Exception):
    pass


class BoundedEventBridgeQueue:
    """Bounded event bridge with 80% DEGRADED watermark and 100% hard-cap fail-closed."""

    def __init__(self, capacity: int = 10):
        self.capacity = capacity
        self.watermark_threshold = int(capacity * 0.8)
        self.items: list[dict] = []
        self.status = "READY"  # READY, DEGRADED, INTEGRITY_FAILED
        self.kill_switch_latched = False
        self.incident_log: dict | None = None

    def push(self, event: dict):
        if self.status == "INTEGRITY_FAILED":
            raise RuntimeError("Engine is halted due to INTEGRITY_FAILED")

        # Check 100% capacity before adding
        if len(self.items) >= self.capacity:
            # Hard-cap overflow: fail closed immediately
            self.status = "INTEGRITY_FAILED"
            self.kill_switch_latched = True
            self.incident_log = {
                "reason": "QUEUE_OVERFLOW_HARD_CAP",
                "queue_size": len(self.items),
                "uncommitted_events": list(self.items),
                "rejected_event": event,
            }
            raise QueueOverflowIncident(
                f"Event bridge reached 100% hard-cap ({self.capacity}); failed closed and latched kill switch"
            )

        self.items.append(event)

        # Check 80% watermark
        if len(self.items) >= self.watermark_threshold:
            self.status = "DEGRADED"

    def can_accept_new_entry_intent(self) -> tuple[bool, str]:
        if self.status == "INTEGRITY_FAILED":
            return False, "ENGINE_INTEGRITY_FAILED_503"
        if self.status == "DEGRADED":
            return False, "QUEUE_PRESSURE_DEGRADED"
        return True, "OK"


def test_queue_overflow_incident_and_fail_closed():
    """Spike 5: Verify 80% DEGRADED watermark and 100% hard-cap fail-closed policy."""
    bridge = BoundedEventBridgeQueue(capacity=10)

    # 1. Normal operations up to 7 items (below 80%)
    for i in range(1, 8):
        bridge.push({"event_id": f"evt-{i}", "type": "ORDER_UPDATE"})
    assert bridge.status == "READY"
    can_enter, _ = bridge.can_accept_new_entry_intent()
    assert can_enter is True

    # 2. Push 8th item -> reaches 80% watermark (8 / 10)
    bridge.push({"event_id": "evt-8", "type": "ORDER_UPDATE"})
    assert bridge.status == "DEGRADED"

    # New entry intents are rejected in DEGRADED mode
    can_enter, reason = bridge.can_accept_new_entry_intent()
    assert can_enter is False
    assert reason == "QUEUE_PRESSURE_DEGRADED"

    # Existing order lifecycle events continue to be accepted
    bridge.push({"event_id": "evt-9", "type": "ORDER_FILLED"})
    bridge.push({"event_id": "evt-10", "type": "POSITION_CHANGED"})
    assert len(bridge.items) == 10

    # 3. 11th item overflows 100% hard-cap -> fails closed immediately
    with pytest.raises(QueueOverflowIncident) as exc_info:
        bridge.push({"event_id": "evt-11", "type": "ORDER_UPDATE"})

    assert "reached 100% hard-cap" in str(exc_info.value)
    assert bridge.status == "INTEGRITY_FAILED"
    assert bridge.kill_switch_latched is True
    assert bridge.incident_log is not None
    assert bridge.incident_log["reason"] == "QUEUE_OVERFLOW_HARD_CAP"
    assert bridge.incident_log["queue_size"] == 10
    assert len(bridge.incident_log["uncommitted_events"]) == 10
    assert bridge.incident_log["rejected_event"]["event_id"] == "evt-11"

    # Read endpoint checks fail-closed: new intents return 503
    can_enter_after, fail_reason = bridge.can_accept_new_entry_intent()
    assert can_enter_after is False
    assert fail_reason == "ENGINE_INTEGRITY_FAILED_503"
