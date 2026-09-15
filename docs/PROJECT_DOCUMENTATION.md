# Cryptonite: Enterprise Algorithmic Trading Infrastructure
## Comprehensive Technical Documentation & Architecture Reference

---

### Executive Overview

**Cryptonite** is an institutional-grade, low-latency quantitative algorithmic trading platform engineered for cryptocurrency markets. Designed to bridge the gap between high-frequency research and real-capital execution, Cryptonite couples a **Nautilus Trader** event-driven backtesting engine and a **Polars-powered vectorized scanner** with a resilient microservices architecture, strict margin risk controls, and a multi-agent AI operations fleet driven by the **Google Antigravity SDK & Google Gemini**.

The system is deployed locally or in high-availability clusters using containerized microservices backed by PostgreSQL, Redis 7, DuckDB, and Apache Parquet column-oriented time-series catalogs.

```mermaid
flowchart TB
    subgraph MarketData ["Market Data & Ingestion Layer"]
        BinanceWS["Binance WebSocket / REST API"] --> Ingest["cryptonite_ingest (Port 8080)"]
        Ingest --> ParquetCat["Nautilus Parquet Catalog (/data/nautilus_catalog)"]
        Ingest --> RedisPubSub["Redis Event Stream (redis:6379)"]
    end

    subgraph AnalyticalCores ["Vectorized Analytical & Research Cores"]
        RedisPubSub --> Scanner["cryptonite_scanner (Port 8082)<br/>Polars Vectorized Core"]
        ParquetCat --> Backtester["cryptonite_backtester (Port 8083)<br/>Nautilus Event-Driven Engine"]
    end

    subgraph ExecutionPlane ["Execution & Settlement Layer"]
        Executor["cryptonite_executor (Port 8084)<br/>Order State Machine & Risk Engine"]
        Outbox["cryptonite_outbox (Port 8081)<br/>Transactional Outbox Poller"]
        Executor --> Outbox
        Outbox --> BinanceLive["Binance Testnet / Live REST & WS"]
    end

    subgraph Persistence ["Persistence & Caching"]
        Postgres[(PostgreSQL 16 Engine<br/>Relational & Audit State)]
        RedisExec[(Redis 7 - Execution Stream)]
        RedisAgents[(Redis 7 - Agent Memory)]
    end

    subgraph AIOperations ["AI Operations Layer (Milestone 7)"]
        AgentWorker["cryptonite_agent_worker (Port 8085)<br/>Google Antigravity SDK + Gemini"]
        AgentWorker --> PostgresRO[(Postgres Read-Only Replica)]
        AgentWorker --> ParquetDuckDB[(Embedded DuckDB Engine)]
        AgentWorker --> RedisAgents
    end

    subgraph Interface ["Institutional Interface Layer"]
        FastAPI["cryptonite_api (Port 8000)<br/>Gateway REST & WS"]
        WebUI["Next.js 16 Web Terminal (Port 3000)<br/>TradingView, Terminal, Operator Console"]
    end

    FastAPI <--> Postgres
    FastAPI <--> RedisExec
    WebUI <--> FastAPI
    WebUI <--> AgentWorker
    Executor <--> Postgres
    Executor <--> RedisExec
    Scanner --> RedisPubSub
```

---

## 1. System Architecture & Component Breakdown

Cryptonite is organized as a unified monorepo divided into modular Python core packages (`packages/`), containerized FastAPI/worker microservices (`services/`), and a Next.js 16 web application (`apps/web/`).

### 1.1 Microservices Topology & Port Mapping

| Service Name | Container Name | Port | Base Framework | Primary Responsibilities |
| :--- | :--- | :--- | :--- | :--- |
| **API Gateway** | `cryptonite_api` | `8000` | FastAPI / Uvicorn | RESTful control gateway, operator authentication, backtest job dispatch, strategy management, and WebSocket streaming bridge. |
| **Ingestion Engine** | `cryptonite_ingest` | `8080` | Python / AsyncIO | Connects to Binance public streams, parses raw k-lines, enforces bar cadence, writes partition-aligned Parquet catalogs, and publishes closed bars. |
| **Outbox Relay** | `cryptonite_outbox` | `8081` | Python / AsyncIO | Guarantees transactional at-least-once message dispatch for outbound exchange orders using PostgreSQL outbox tables. |
| **Scanner Engine** | `cryptonite_scanner` | `8082` | Polars / Python | High-throughput vectorized technical screener running complex polars expressions across thousands of bars in under 10ms. |
| **Backtester Worker**| `cryptonite_backtester` | `8083` | Nautilus Trader / C++ | Executes deterministic, tick/bar-level discrete event backtests with slippage, latency models, and historical data cadence validation. |
| **Execution Engine** | `cryptonite_executor` | `8084` | Nautilus / Python | Live/testnet order execution engine managing order lifecycles, cross/isolated margin risk, position reconciliation, and fill tracking. |
| **AI Agent Worker** | `cryptonite_agent_worker` | `8085` | Google Antigravity SDK | Autonomous quantitative operations fleet (Research & Explainer Agents) equipped with read-only database query and DuckDB tools. |
| **Web Frontend** | Host Process | `3000` | Next.js 16 / React | High-density trading workstation with TradingView charts, real-time WebSocket feeds, research tear sheets, and operator control panel. |
| **Primary Database** | `cryptonite_postgres` | `5432` | PostgreSQL 16 | Durable state storage for users, strategies, backtest runs, order ledgers, and transactional outbox. |
| **Execution Redis** | `cryptonite_redis` | `6379` | Redis 7 Alpine | Ultra-low latency pub/sub bus for market data streams, order signals, and execution events. |
| **Agent Redis** | `cryptonite_redis_agents` | `6380` | Redis 7 Alpine | Dedicated isolated session cache and long-term conversational memory for AI agents. |

---

## 2. Core Packages & Domain Logic

The business and quantitative trading logic is encapsulated in modular Python packages under `packages/`:

### 2.1 `cryptonite_schemas`
Contains all Pydantic v2 data models, enums, and database serialization contracts.
* **Instruments & Orders**: `OrderSide`, `OrderType`, `TimeInForce`, `ExecutionStyle`, `OrderStatus`.
* **Risk & Margins**: `MarginMode` (`CROSS`, `ISOLATED`), `PositionSide` (`LONG`, `SHORT`, `BOTH`), `LeverageBracket`.
* **Backtest Contracts**: `BacktestRunRequest`, `BacktestRunResponse`, `TearsheetMetrics`.
* **Market Telemetry**: `Bar`, `KlineMessage`, `ScanResult`, `ReconciliationAuditReport`.

### 2.2 `cryptonite_storage`
Provides unified persistence layers for relational, column-oriented, and streaming telemetry.
* **`NautilusCatalogManager`**: Interacts with Nautilus Trader's `ParquetDataCatalog`, ensuring all ingested bars are stored under canonical directory layouts (`/data/nautilus_catalog/data/bars/{instrument}-{timeframe}-LAST-EXTERNAL/`).
* **`AnalyticsCatalogManager`**: Manages DuckDB queries over aggregated parquet files for ultra-fast SQL scans over multi-gigabyte datasets without saturating PostgreSQL.

### 2.3 `cryptonite_scanner`
* Vectorized screening engine utilizing **Polars LazyFrames**.
* Evaluates technical conditions (RSI, Bollinger Bands, EMA Crosses, Volume Spikes, Supertrend) over rolling sliding windows in single-digit milliseconds.
* Dispatches alert candidates to Redis topic `cryptonite:scanner:alerts` for consumption by the UI and automated strategies.

### 2.4 `cryptonite_backtester`
* Integrates Nautilus Trader's discrete-event simulation kernel.
* **Cadence Validator (`cadence.py`)**: Enforces temporal monotonicity ($ts_{i} - ts_{i-1} == \Delta t_{step}$). Detects missing bars or duplicate data feeds before starting execution.
* **Execution Profiles**: Simulates realistic execution environments:
  * `PERFECT`: Zero latency, zero slippage, zero fee benchmark.
  * `REALISTIC`: 50ms simulated network latency, 1 bps taker fee, and volume-weighted slippage.
  * `HIGH_VOLATILITY`: 150ms simulated network latency, 5 bps slippage, and adverse fill distributions.
* **Tearsheet Generator**: Extracts Sharpe Ratio, Sortino Ratio, Calmar Ratio, Max Drawdown, Win Rate, Profit Factor, and Monthly PnL Returns Heatmaps.

### 2.5 `cryptonite_execution`
* Implements the **Transactional Outbox Pattern** to prevent dual-write hazards and state desynchronization during network partitions.
* Coordinates with `BinanceDemoWebSocketApiClient` and REST execution adapters.
* Enforces pre-trade risk checks:
  * Maximum notional position size limits.
  * Maximum order rate limits (leaky bucket).
  * Instrument whitelist filtering.
  * Margin requirement enforcement.

### 2.6 `cryptonite_agents`
* Autonomous operations layer built on the **Google Antigravity SDK** (`google-antigravity`).
* Uses Google Gemini models (`gemini-2.5-pro`, `gemini-2.5-flash`) for real-time quantitative reasoning.
* Equipped with enterprise-grade safety tools:
  * `query_postgres_readonly`: Queries the database using an unprivileged `cryptonite_agent_ro` PostgreSQL role with session-level `default_transaction_read_only = on`.
  * `query_parquet_duckdb`: Executes analytical aggregations directly over Parquet files via DuckDB, avoiding token saturation.
  * `inspect_backtest_run`: Summarizes backtest tearsheets and identifies parameter sensitivities.

---

## 3. Data Flow & Execution Pipelines

### 3.1 Live Market Data Ingestion Pipeline
```mermaid
sequenceDiagram
    participant Exchange as Binance WebSocket
    participant Ingest as cryptonite_ingest
    participant Redis as Redis Pub/Sub
    participant Parquet as Parquet Catalog
    participant Web as Next.js Web UI

    Exchange->>Ingest: Raw Kline Stream (open/close)
    Ingest->>Ingest: Normalize to Canonical Bar (UTC Nanoseconds)
    alt Bar is Open
        Ingest->>Redis: Publish to 'open_kline'
    else Bar is Closed
        Ingest->>Parquet: Flush to Partitioned Parquet
        Ingest->>Redis: Publish to 'closed_bar_committed'
    end
    Redis->>Web: Stream over WebSocket (p95 < 16ms)
```

### 3.2 Transactional Outbox Execution Pipeline
```mermaid
sequenceDiagram
    participant Strategy as Strategy / Operator
    participant Executor as cryptonite_executor
    participant DB as PostgreSQL (Outbox)
    participant OutboxService as cryptonite_outbox
    participant Exchange as Binance Testnet API

    Strategy->>Executor: Submit Order Request
    Executor->>Executor: Evaluate Risk Limits & Margin Bounds
    Executor->>DB: ATOMIC TX: Insert Order + Insert Outbox Event
    DB-->>Executor: Commit OK
    OutboxService->>DB: Poll PENDING Outbox Events (FOR UPDATE SKIP LOCKED)
    OutboxService->>Exchange: Signed REST Order Submission
    Exchange-->>OutboxService: Execution Report (ACK / REJECT / FILLED)
    OutboxService->>DB: Update Order Status & Mark Outbox PROCESSED
```

---

## 4. Security, Governance & Risk Management

### 4.1 Operator Authentication & Role-Based Access Control
* **Argon2id Password Hashing**: Passwords stored using state-of-the-art memory-hard Argon2id hashing algorithms.
* **HTTP-Only Session Cookies**: Session tokens stored in encrypted HTTP-Only cookies with `SameSite=Lax` to prevent XSS exfiltration.
* **CSRF Protection**: Double-submit CSRF tokens validate all mutating POST/PUT/DELETE requests originating from the operator interface.

### 4.2 Global Execution Kill-Switch
The platform implements an institutional-grade hardware-style **Global Kill-Switch** (`KillSwitchPanel`):
1. **ARMED State**: All deployed strategy instances run and submit orders normally.
2. **TRIP Sequence**: When tripped by an authorized operator:
   * Increments the global kill-switch generation counter in Redis and PostgreSQL.
   * Immediately terminates all active order loops.
   * Cancels all open limit and stop orders across all connected exchanges.
   * Optionally executes market liquidation ("Flatten All") to eliminate open inventory risk.
   * Rejects any new inbound order signals with HTTP 423 Locked.

### 4.3 Multi-Asset Reconciliation Ledger
To detect exchange desynchronization, stale orders, or phantom fills, Cryptonite executes scheduled and on-demand reconciliation audits (`ReconciliationLedger`):
* Fetches live account balances and open orders from Binance via authenticated REST queries.
* Fetches internal ledger balances and registered active orders from PostgreSQL.
* Performs exact zero-tolerance delta analysis:
  $$\Delta_{Balance} = Balance_{Exchange} - Balance_{Ledger}$$
* Logs all deviations in `reconciliation_audit_logs` and flags anomalies in the operator dashboard.

---

## 5. Configuration & Environment Variables

The system is configured via a root `.env` file loaded by Docker Compose:

```ini
# Exchange Connectivity (Binance Futures / Spot Testnet)
BINANCE_DEMO_API_KEY=your_binance_api_key
BINANCE_DEMO_API_SECRET=your_binance_api_secret

# AI Operations & Antigravity SDK
GEMINI_API_KEY=your_google_gemini_api_key

# Cryptography & Sessions
CRYPTONITE_TOTP_AES_KEY=your_base64_aes256_key
OPERATOR_SESSION_SECRET=your_random_session_secret

# Database & Cache URLs
DATABASE_URL=postgresql://cryptonite:cryptonite@localhost:5432/cryptonite
REDIS_URL=redis://localhost:6379/0
REDIS_AGENTS_URL=redis://localhost:6380/0

# Storage Paths
NAUTILUS_CATALOG_PATH=./data/nautilus_catalog
```

---

## 6. Service Management & Operations Quick Reference

### Starting the Platform
```bash
# Start all supporting containers and microservices
cd infra && docker compose up -d

# Start the web interface
cd apps/web && pnpm dev
```

### Checking Service Health
```bash
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8085/health
```

### Rebuilding a Service after Code Changes
```bash
cd infra
docker compose build <service_name> && docker compose up -d <service_name>
# Examples: agent_worker, backtester, executor, api
```
