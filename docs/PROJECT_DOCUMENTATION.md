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

    subgraph AIOperations ["AI Operations Layer (Google Antigravity & Gemini)"]
        AgentWorker["cryptonite_agent_worker (Port 8085)<br/>Google Antigravity SDK + Gemini API"]
        AgentWorker --> PostgresRO[(Postgres Read-Only Replica<br/>cryptonite_agent_ro)]
        AgentWorker --> ParquetDuckDB[(Embedded DuckDB Engine<br/>Security Sandboxed)]
        AgentWorker --> RedisAgents
    end

    subgraph Interface ["Institutional Interface Layer"]
        FastAPI["cryptonite_api (Port 8000, 0.0.0.0)<br/>Gateway REST & WS (/ws/stream)"]
        WebUI["Next.js 16 Web Terminal (Port 3000)<br/>TradingView, Terminal, Operator Console, AI Copilot"]
    end

    FastAPI <--> Postgres
    FastAPI <--> RedisExec
    WebUI <-->|REST & /agent-api (300s timeout)| FastAPI
    FastAPI -->|HMAC-SHA256 Signed Requests| AgentWorker
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
| **API Gateway** | `cryptonite_api` | `8000` (Bound to `0.0.0.0`) | FastAPI / Uvicorn | RESTful control gateway, operator authentication, backtest job dispatch, strategy management, and WebSocket streaming bridge (`/ws/stream`). Accessible over LAN. |
| **Ingestion Engine** | `cryptonite_ingest` | `8080` | Python / AsyncIO | Connects to Binance public streams, parses raw k-lines, enforces bar cadence, writes partition-aligned Parquet catalogs, and publishes closed bars. |
| **Outbox Relay** | `cryptonite_outbox` | `8081` | Python / AsyncIO | Guarantees transactional at-least-once message dispatch for outbound exchange orders using PostgreSQL outbox tables. |
| **Scanner Engine** | `cryptonite_scanner` | `8082` | Polars / Python | High-throughput vectorized technical screener running complex polars expressions across thousands of bars in under 10ms. |
| **Backtester Worker**| `cryptonite_backtester` | `8083` | Nautilus Trader / C++ | Executes deterministic, tick/bar-level discrete event backtests with slippage, latency models, and historical data cadence validation. |
| **Execution Engine** | `cryptonite_executor` | `8084` | Nautilus / Python | Live/testnet order execution engine managing order lifecycles, cross/isolated margin risk, position reconciliation, and fill tracking. |
| **AI Agent Worker** | `cryptonite_agent_worker` | `8085` (Internal) | Google Antigravity SDK | Autonomous quantitative operations fleet (Research, Explainer, Strategist, Generator) equipped with read-only database query and DuckDB tools. Authenticated via HMAC. |
| **Web Frontend** | Host Process | `3000` (Bound to `0.0.0.0`) | Next.js 16 / React | High-density trading workstation with TradingView charts, real-time WebSocket feeds, research tear sheets, and operator control panel. Accessible over LAN. |
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

**Role**: AI Operations & Quantitative Copilot Layer.  
**Tech Stack**: Google Antigravity SDK, Google Gemini API, Local DuckDB, PostgreSQL.

This module houses an intelligent multi-agent fleet designed to act as an autonomous trading desk. The user interface exposes 3 unified operational modes that route to specialized backend agents:

1. **Research Agent (`Senior Quantitative Analyst`)**:
   - Uses `query_postgres_readonly` to inspect active instruments, orders, and scanner states.
   - Uses `query_parquet_duckdb` to execute sub-second columnar SQL queries over partitioned Parquet historical tick/bar data (`data/bars/**/*.parquet`).
   - Automatically aggregates analytical metrics (volume profiles, spread distributions, volatility) rather than dumping raw rows into context.
   - Guardrails: Blocks all destructive SQL keywords (`COPY`, `DROP`, `DELETE`, `UPDATE`, `httpfs`) and strictly enforces directory path containment within `CATALOG_PATH`.

2. **Explainer Agent (`Risk & Performance Manager`)**:
   - Uses `read_nautilus_tearsheet` to parse backtest simulation runs from `/app/data/backtest_artifacts/{backtest_id}`.
   - Reads `manifest.json`, `trades.parquet`, and `equity_curve.parquet`.
   - **DuckDB Metric Synthesis Fallback**: When `tearsheet.json` is missing or in-progress, dynamically synthesizes Sharpe ratio, Max Drawdown %, total return, win rate %, and profit factor directly from underlying Parquet equity curves and trade ledgers.
   - Provides plain-English attribution of strategy strengths, regime dependency, and execution slippage risks.

3. **Strategist Agent (`Execution & Strategy Architect`)**:
   - **Automated Nautilus Strategy Coding (`Generator Agent`)**: Generates production-ready, fully typed Python code with `StrategyConfig` and `Strategy` classes conforming to Nautilus Trader standards. Integrated with the UI's one-click "Save Strategy" workflow.
   - **Opportunity Hunting (`Opportunity Hunter Agent`)**: Continuously monitors market structure, RSI levels, and price action indicators to suggest actionable setups.
   - **Deterministic 5-Rule Hierarchical Protocol (`StrategistEngine`)**:
     1. *Macro Context*: Derives 1D & 4H structural floor (support) and ceiling (resistance).
     2. *Volume Trap Filter*: Fades bull traps, bear traps, and liquidity sweeps on 15m/5m order flow.
     3. *Momentum Layer*: Validates trend alignment across 1H and 15m timeframes.
     4. *Micro Trigger*: Verifies 5m candle confirmation before generating directional entry bias.
     5. *Chop Avoidance*: Enforces flat / no-trade bias when indicators conflict, preserving paper and live capital.

**Security & Dynamic Configuration**:
- **Isolated PostgreSQL RBAC**: Connects via `cryptonite_agent_ro` (`default_transaction_read_only = 'on'`).
- **Internal HMAC Authentication**: All gateway-to-worker calls are cryptographically signed with rotating SHA-256 nonces and timestamps (`X-Internal-Timestamp`, `X-Internal-Nonce`, `X-Internal-Signature`).
- **Dynamic Configuration Cache**: Agent system instructions, Gemini model choices (`gemini-2.5-pro`, `gemini-2.5-flash`), and temperature settings are stored in the PostgreSQL `agent_configs` table and cached for 60 seconds, enabling real-time prompt tuning without container restarts.
- **Extended Gateway Timeout**: The Next.js `/agent-api/[...slug]` proxy implements a 300-second execution timeout, ensuring complex multi-step research queries complete without premature 504 timeouts.

---
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

---

## 7. Market Intelligence & Background Alerts Engine
The platform includes an automated discovery layer decoupled from execution:
* **Background Alerts**: The `cryptonite_api` runs a continuous background polling loop (`alerts.py`) that cross-references live PostgreSQL `bars` against user-defined thresholds (`user_alerts` table, created in Alembic migration `008`), pushing WebSocket notifications upon breaches.
* **Onboarding via Command Palette**: Fast HTTP requests via `httpx.AsyncClient` directly onboard new spot pairs from Binance exchange info endpoints, creating immediate catalog entries and triggering historical 1-minute backfills.
* **Alembic Migration 008**: Adds `coingecko_id` on `instruments` for precise third-party market data resolution, creates the `user_alerts` table, and grants full RBAC privileges to `cryptonite_api`.

---

## 8. Local Network (LAN) & Multi-Device Access Architecture
Cryptonite is engineered to allow seamless operational monitoring from secondary devices (laptops, iPads, smartphones) on the local area network (LAN):
* **0.0.0.0 Port Binding**: In `infra/docker-compose.yml`, the API Gateway binds port 8000 to `0.0.0.0:8000:8000`, making REST and WebSocket `/ws/stream` endpoints accessible across the subnet.
* **Next.js `allowedDevOrigins`**: Configured in `apps/web/next.config.mjs` with `["127.0.0.1", "localhost", "192.168.1.203", "192.168.1.*"]` to allow Turbopack cross-origin dev server connections without warning blocks.
* **Dynamic WebSocket Host Resolution**: The client WebSocket library (`apps/web/src/lib/ws.ts`) inspects `window.location.hostname` at runtime, connecting seamlessly to `ws://<host_ip>:8000/ws/stream`.
* **CORS Allow-List**: In `services/api/cryptonite_api/stream_gateway.py`, `ALLOWED_ORIGINS` includes local subnet origins and wildcard compatibility for development environments.
* **Access URL**: `http://192.168.1.203:3000` (Operator credentials: `admin` / `admin123` / TOTP: `000000`).


### 2.5 AI Adversarial Debate Engine (`debate_worker`)
Integrates the `TradingAgents` pattern into the Cryptonite core, converting single-agent queries into a rigorously orchestrated, multi-persona adversarial debate before any trade is proposed.

**Core Workflow**:
1. **Analyst Fleet**: Specialized Gemini Flash agents (Market, Sentiment, On-Chain) gather parallel intelligence dossiers, caching expensive on-chain RPC calls in Redis.
2. **Adversarial Debate**: A `Bull Researcher` and `Bear Researcher` debate the dossier.
3. **Risk & Synthesis**: A `Portfolio Manager` sets initial sizing, followed by a Risk Debate (Aggressive, Conservative, Neutral). The `Chief Risk Officer` outputs final allocation caps (max 25.0%).
4. **Interactive Validation**: In `interactive` mode, decisions pause at `PENDING_APPROVAL` and await human authorization in the workstation UI. In `autonomous` mode, approved allocations flow straight into `paper_portfolio_decisions`.
5. **Reflection Loop**: Completed trades are joined with Nautilus Parquet telemetry to calculate true Alpha. Lessons learned are autonomously fed back into the Portfolio Manager's prompt context for future iterations.

**Security Constraints**:
- Uses the `AntiCorruptionLayer` pattern for strict SQL bounds.
- Connects exclusively as `cryptonite_agent_ro` (Read-Only postgres user for agent views).
- Cannot bypass the `GuardedOrderDispatcher` global risk invariants.

---

## 9. Modern UI/UX Workstation & AI Copilot Integrations

Cryptonite features an institutional, Bloomberg-inspired desktop terminal interface engineered in Next.js 16 with Tailwind CSS 4 and Lucide icons:

### 9.1 Bloomberg-Style 3-Column Resizable Workspace (`TerminalWorkspace.tsx`)
* **3-Column Resizable Grid**: Includes a main TradingView chart view, real-time Orderbook depth panel, and AI Consensus Debate sidebar.
* **Dynamic Sidebar Toggles**: Interactive header controls (`Show Orderbook`, `Show AI Debate Sidebar`) and panel close buttons allow traders to collapse sidebars.
* **Canvas Expansion**: Collapsing sidebars dynamically expands the chart canvas to 100% viewport width without layout distortion.
* **State Persistence**: Workspace layout preferences are persisted across browser sessions via `localStorage`.

### 9.2 Freely Draggable & Resizable AI Copilot (`AgentChatPanel.tsx`)
* **Floating Window**: The AI Copilot panel can be freely dragged across the viewport via pointer capture on its header bar and resized via bottom/side handles.
* **5 Specialized AI Personas**:
  1. **Research Agent** (`Senior Quantitative Analyst`): Market structure, technical indicators, RSI trends.
  2. **Strategist Agent** (`Execution & Strategy Architect`): Writes production-ready Nautilus Python strategies.
  3. **Explainer Agent** (`Risk & Performance Manager`): Parses backtest tearsheets and performance metrics.
  4. **CRO Agent** (`Chief Risk Officer`): Evaluates risk exposure, position bounds, and margin limits.
  5. **On-Chain Agent** (`On-Chain Intelligence`): Queries exchange flow and liquidity metrics.
* **Isolated Threads & Draft Storage**: Each persona maintains an independent conversation history and draft input state. Switching tabs preserves active draft text.
* **Selection & 1-Click Code Actions**: Full `select-text` capability for copying response text, with 1-click **Copy Code** and **Save Strategy** buttons.

### 9.3 On-Demand Consensus Debate Triggers (`/debates/trigger`)
* **Chart Header Trigger**: Click `[Trigger AI Consensus Debate]` on the active chart header to push an on-demand debate payload to `stream:debate_queue`.
* **Command Palette Integration**: Press `⌘D` inside the `CommandPalette` (`Ctrl/Cmd + K`) to queue debates instantly for any symbol.
* **Unauthenticated Analytical Access**: Copilot and debate trigger routes (`/api/v1/agents/*`) permit analytical queries without forcing administrative operator logins, enabling seamless trader exploration directly from the terminal.
