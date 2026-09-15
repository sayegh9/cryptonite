# Milestone 4: Paper Trading, Authoritative Risk Guardrails & Event Bridge

**Cryptonite Architecture Version**: Hardened v6.1  
**Status**: Completed & Verified  
**Date**: September 2026  

---

## 1. Executive Summary & Core Principles

Milestone 4 establishes the real-time simulation and paper trading execution layer for **Cryptonite**, powered by NautilusTrader `2.0.0rc4`.

The implementation strictly honors Cryptonite's foundational architectural governing principles:
- **AI and UI can propose.**
- **Deterministic rules and risk controls decide.**
- **NautilusTrader executes.**

### Key Invariants Enforced
1. **Zero-DB I/O Hot Callback Path**:
   Under no circumstances does the Nautilus event loop or strategy callback thread execute synchronous database queries, network calls, or disk I/O. All execution events are placed on a bounded, thread-safe memory queue (`queue.Queue(maxsize=10_000)`).
2. **Fail-Closed Hard-Cap Overflow (100%)**:
   If the queue ever reaches 10,000 items, the callback thread performs zero database I/O: it atomically latches the `INTEGRITY_FAILED` flag, trips the in-memory kill switch, snapshots state in memory, and signals a background supervisor. The supervisor persists the incident to PostgreSQL and forces `/ready` to return `HTTP 503 Service Unavailable`.
3. **Gate-Owned Deduplicating Bar Router**:
   A deterministic SHA-256 deduplication key prevents duplicate bar processing and duplicate indicator updates (such as double EMA/RSI calculations) during rewarm and reconnection:
   $$\text{dedupe\_key} = \text{SHA256}(\text{instrument\_id} + \text{":"} + \text{bar\_type} + \text{":"} + \text{bar\_open\_ns} + \text{":"} + \text{payload\_checksum})$$
4. **8-Step Lossless Watermark Handoff Protocol**:
   Subscribes at stream head $S$, reads catalog watermark $C$, rewinds and warms strategy indicators with catalog bars $\le C$, discards buffered stream bars $\le C$, processes bars $> C$, persists the actual processed high-water mark to PostgreSQL, and opens the live gate only if the database commit succeeds.
5. **Dual Market Data Freshness Verification**:
   Before any order can be dispatched:
   - `cryptonite:ingress:health` must have published a heartbeat within the last 5 seconds.
   - In-memory `node.cache.quote(instrument_id)` must exist and have been updated within the last 5 seconds.
6. **Authoritative Pre-Trade Risk Engine**:
   - **Spot Cash Invariant**: Enforces `HOLDINGS >= 0` across all spot instruments; short sales and sell orders exceeding available base asset holdings are rejected unconditionally.
   - **Asymmetric Dynamic Price Collars**: Limits aggressive buy limit orders to $\le \text{quote.ask} \times (1 + \text{collar\_pct})$ and sell orders to $\ge \text{quote.bid} \times (1 - \text{collar\_pct})$.
   - **Conservative Cash Reservation**: Buy orders atomically reserve $\text{notional} \times (1 + \text{buffer})$, where `buffer = 0.002` (0.20%) to account for potential slippage and taker commissions. Cash is reconciled upon fill or cancellation.
   - **UTC Rolling Daily Loss Limit**: Tracks realized + unrealized drawdown since `00:00:00 UTC`. If drawdown breaches `-3.0%`, the kill switch is tripped and latched for the remainder of the UTC trading day.

---

## 2. System Architecture & Component Diagram

```
                               ┌─────────────────────────────────────────────────────────────┐
                               │                 Web UI: Portfolio Console                   │
                               │  - Net Equity & PnL Summary  • Active Positions Table       │
                               │  - Open & Historical Orders  • Strategy Deployment Dialog   │
                               │  - Emergency Latched Kill Switch Button                     │
                               └──────────────────────────────┬──────────────────────────────┘
                                                              │ REST (Commands) / WS (/ws/stream)
                                                              ▼
                               ┌─────────────────────────────────────────────────────────────┐
                               │                     FastAPI Gateway                         │
                               │  - Read: SELECT on paper projections                        │
                               │  - Write: INSERT INTO execution_commands ONLY               │
                               │  - Fanout: paper_orders, paper_positions, risk alerts       │
                               └──────────────────────────────┬──────────────────────────────┘
                                                              │
                                       ┌──────────────────────┴──────────────────────┐
                                       │ INSERT (Command Only)                       │
                                       ▼                                             ▼
                        ┌──────────────────────────────┐              ┌──────────────────────────────┐
                        │      PostgreSQL Database     │              │         Redis Streams        │
                        │  - execution_commands        │              │  - nautilus:stream:bus       │
                        │    (attempt_id, lease-fenced)│              │    (Clean BusMessage records │
                        │  - paper_execution_events    │              │     QuoteTick, Bar, Pair)    │
                        │    (UNIQUE order, version)   │              │  - cryptonite:stream:        │
                        │  - integrity_incidents       │              │    ingress_health (Telemetry)│
                        │  - paper_accounts (Read)     │              │  - cryptonite:stream:        │
                        │  - paper_orders (Read)       │              │    ui_quote_tick (UI stream) │
                        │  - paper_positions (Read)    │              │  - cryptonite:stream:        │
                        │  - paper_trades (Read)       │              │    closed_bar_committed (M2) │
                        │  - execution_kill_switch     │              │  - outbox_events             │
                        └──────────────▲───────────────┘              └──────────────▲───────────────┘
                                       │                                             │
                        Batch Read/Write Projections & Outbox           Ingress Consume & Outbox Relay
                                       │                                             │
┌──────────────────────────────────────┴─────────────────────────────────────────────┴───────────────────────┐
│                    Dedicated Paper Executor Daemon (cryptonite_executor, Port 8084)                       │
│                                                                                                           │
│  [Async IO Loop]                                                                                          │
│  - IngressHealthMonitor: Consumes ingress_health, tracks (session_id, seq), evaluates is_fresh            │
│  - Async Command Poller: SELECT ... FOR UPDATE SKIP LOCKED -> pushes to NodeCommandMailbox                │
│  - EventBridgeWorker: Drains bounded thread-safe queue -> commits Journal + Projections in 1 DB tx        │
│  - Integrity Supervisor: Listens to failure signal -> persists integrity_incidents to DB, trips 503       │
│  - Coalesced Mark-to-Market Projector: updates current_price, unrealized PnL, equity every 250ms          │
│                                                                                                           │
│  [Thread-Safe Boundaries]                                                                                 │
│  - NodeCommandMailbox: queue.Queue bridging commands -> Node Thread                                       │
│  - EventBridge Channel: bounded queue.Queue + atomic counter bridging Node Callbacks -> Async Worker      │
│                                                                                                           │
│  [Dedicated Node Worker Thread: LiveNode.run()]                                                           │
│  - Version Check: asserts nautilus_trader.__version__ == "2.0.0rc4" at startup                             │
│  - Exactly ONE Nautilus LiveNode (Sandbox / Testnet profiles)                                             │
│  - Gate-Owned Bar Router: Dedupes by (instrument_id, bar_type, open_ns, checksum) before indicators       │
│  - Lossless Handoff: Subscribe $ -> Watermark C -> Rewarm -> Discard <= C -> Process > C                  │
│    Persists max_processed_bar_ns only after successful handling; live gate opens iff checkpoint committed  │
│  - Dual Freshness Check: Ingress health continuous AND node.cache.quote exists & ts_init >= health ts    │
│  - Synchronous GuardedOrderDispatcher:                                                                    │
│    - Evaluates live memory portfolio & quotes BEFORE submission (ZERO async locks, ZERO DB I/O)           │
│    - Synchronous in-memory cash/exposure reservation (conservative market buy Ask * (1 + collar + slip)) │
│    - Rejection on Latched Kill Switch, Spot Cash Invariant, Staleness, Collars, Daily-Loss Latch          │
│    - Atomically submits to Nautilus in node thread; releases reservation on terminal lifecycle event      │
│  - Hard-Cap Overflow: atomic INTEGRITY_FAILED flag, trips kill latch, signals supervisor (ZERO DB I/O)     │
│  - Consumes NodeCommandMailbox: executes PAUSE (cancel entry orders), STOP, CLOSE_POSITION, RESET_SWITCH  │
└───────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Detailed Component Breakdown

### 1. Version Locking & Verification
- **Module**: `packages/execution/cryptonite_execution/version_lock.py`
- Enforces strict pinning to `nautilus_trader==2.0.0rc4` at module import time and on engine startup via `assert_version_locked()`.

### 2. Market Data Ingress & Native Serialization
- **Modules**: `services/ingest/cryptonite_ingest/stream.py`, `packages/execution/cryptonite_execution/ingress.py`
- Preserves single market-data ownership: `services/ingest` is the sole consumer of exchange WebSockets.
- Publishes native Nautilus wire messages (`QuoteTick`, `Bar`, `CurrencyPair`) with canonical topics:
  - Quotes: `data.quotes.BINANCE.{symbol}`
  - Bars: `data.bars.{symbol}.BINANCE-{timeframe}-LAST-EXTERNAL`
- Publishes ingestion health telemetry to `cryptonite:stream:ingress_health` with sequence numbers and timestamps for dual-freshness verification.

### 3. Gate-Owned Bar Router & Startup Replay Buffer
- **Module**: `packages/execution/cryptonite_execution/router.py`
- Manages an 8-step startup sequence:
  1. Retrieve latest catalog watermark $C$.
  2. Record Redis stream subscription position $S$.
  3. Rewarm strategy indicators using historical catalog bars $\le C$.
  4. Stream real-time bars into an in-memory buffer while the gate is closed.
  5. Discard buffered bars where `bar_open_ns <= C`.
  6. Pass subsequent bars through the gate deduplicator.
  7. Persist the actual processed high-water mark to PostgreSQL.
  8. Open the gate for live execution.

### 4. Authoritative Risk Engine & Order Dispatcher
- **Module**: `packages/execution/cryptonite_execution/risk.py`
- Evaluates proposed orders in-memory synchronously:
  - **Spot Cash Invariant**: Rejects sell orders if `quantity > available_base_holdings`.
  - **Dynamic Collars**: Rejects buys $> \text{ask} \times (1 + \text{collar})$ or sells $< \text{bid} \times (1 - \text{collar})$.
  - **Cash Reservation**: Atomically reserves $100.2\%$ of notional value before dispatching to Nautilus.
  - **UTC Rolling Daily Loss**: Automatically trips and latches the kill switch if daily equity drops $\ge 3\%$.

### 5. Cross-Thread Event Bridge & Integrity Supervisor
- **Module**: `packages/execution/cryptonite_execution/bridge.py`
- Implements bounded `queue.Queue(maxsize=10_000)` with watermark thresholds:
  - 70% (7,000): Warning log.
  - 85% (8,500): Telemetry shedding.
  - 100% (10,000): Atomic `INTEGRITY_FAILED`, kill switch trip, supervisor alert, and 503 readiness.
- Asynchronous bridge worker flushes micro-batches (100 events or 50ms) to PostgreSQL within single transactions.

### 6. Emergency Kill Switch & Audit Trail
- **Module**: `packages/execution/cryptonite_execution/kill_switch.py`
- Latched boolean state with audited trip reasons.
- Triggers emergency position flattening and cancels resting limit orders.
- Unlatching requires explicit operator identity and resets risk counters.

### 7. Strategy AST Security Enforcer
- **Module**: `packages/execution/cryptonite_execution/ast_enforcer.py`
- Inspects strategy Python abstract syntax trees before deployment:
  - Enforces that strategies propose order intents through `self.dispatcher.propose(intent)` rather than attempting direct order submission.
  - Strictly prohibits direct calls to `self.submit_order()`, `self.submit_order_list()`, `self.cancel_order()`, or `self.cancel_all_orders()`.
  - Prohibits direct access to private execution engine or raw trader objects (`self._engine`, `self._trader`, `self._kernel`, `self._msgbus`).

### 8. Paper Trading Engine
- **Module**: `packages/execution/cryptonite_execution/engine.py`
- Manages a single `LiveNode` hosting multiple isolated strategy instances.
- Simulated exchange matching with zero execution lag, 0.10% maker/taker commissions, and market fill simulation.

### 9. Database Schema Migration 005 & Role Grants
- **Migration**: `infra/migrations/versions/005_paper_trading_schema.py`
- Created 10 relational tables:
  - `execution_commands`, `paper_execution_events`, `paper_accounts`, `paper_strategy_instances`, `paper_orders`, `paper_positions`, `paper_trades`, `paper_risk_events`, `execution_kill_switch`, `integrity_incidents`.
- Configured PostgreSQL role grants:
  - `cryptonite_api`: `SELECT` on projection tables, `INSERT` only on `execution_commands`.
  - `cryptonite_executor`: Full read/write on execution tables, `UPDATE` on `execution_commands`.

### 10. API Gateway & Portfolio Console UI
- **API Routes**: `services/api/cryptonite_api/routes/paper.py`
- **Frontend Dashboard**: `apps/web/src/components/PortfolioConsole.tsx`
  - Real-time account equity and PnL metrics.
  - Active positions table with 1-click flatten.
  - Orders journal with cancel controls.
  - Strategy deployment modal.
  - Latched emergency kill switch controls.

---

## 4. Test Verification Evidence

### Automated Backend Tests (pytest)
- Total Tests: **106 passed** (100% success rate in 25.50s)
- Modules Covered:
  - `test_nautilus_sandbox_spike.py` (5 tests)
  - `test_paper_api_permissions_and_fencing.py` (4 tests)
  - `test_paper_dual_freshness.py` (2 tests)
  - `test_paper_engine_version_lock.py` (3 tests)
  - `test_paper_event_bridge_cross_thread.py` (3 tests)
  - `test_paper_kill_switch.py` (1 test)
  - `test_paper_mark_to_market_projection.py` (2 tests)
  - `test_paper_risk_guardrails.py` (4 tests)
  - `test_paper_router_and_rewarm.py` (1 test)
  - `test_paper_strategy_ast_enforcement.py` (4 tests)
  - `test_paper_watermark_persistence.py` (2 tests)
  - `test_full_stack_paper_trading_e2e.py` (1 end-to-end integration test)
  - Pre-existing regression suites (Milestones 1-3): 74 tests

### Automated Web E2E Tests (Playwright)
- Total Tests: **10 passed** (100% success rate in 15.1s)
- Specs Covered:
  - `portfolio_console.spec.ts` (6 tests: navigation, metrics, flatten position, pause/resume/stop strategy, cancel order, kill switch trip/reset)
  - `research_lab.spec.ts` (1 test: backtest configuration & tearsheet)
  - `terminal.spec.ts` (3 tests: dashboard attribution, multi-chart layout, scanner navigation)
