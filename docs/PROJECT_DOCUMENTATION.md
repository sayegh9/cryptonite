# Cryptonite: Enterprise Algorithmic Trading Infrastructure
## Comprehensive Technical Documentation & Architecture Reference

---

### Executive Overview

**Cryptonite** is an institutional-grade, low-latency quantitative algorithmic trading platform engineered for cryptocurrency markets. Designed to bridge the gap between high-frequency research and real-capital execution, Cryptonite couples a **Nautilus Trader** event-driven backtesting engine and a **Polars-powered vectorized scanner** with a resilient microservices architecture, strict margin risk controls, and a multi-agent AI operations fleet driven by **Google Gemini and local open models (Ollama)** through one AI gateway.

> **Current state (2026-10-04):** the AI-First program (Phases 0–7) changed the AI layer substantially: the Antigravity SDK was replaced by a Pydantic AI gateway, AI orders now need a **mandate**, approvals can come from Telegram, and a learner labels outcomes. The authoritative description is [ai-first-program.md](ai-first-program.md); where this document disagrees with it, that page is current.

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
| **AI Agent Worker** | `cryptonite_agent_worker` | `8085` (Internal) | AI gateway (Pydantic AI) | 1-on-1 quantitative operations fleet (Research, Explainer, Strategist, CRO, On-Chain) equipped with read-only database query and DuckDB tools. Authenticated via HMAC. |
| **Debate Worker** | `cryptonite_debate_worker` | Internal Worker Daemon | AI gateway (Pydantic AI) | Multi-agent adversarial debate engine (Bull vs Bear, Risk Committee, Reflection loop). Consumes Redis Streams (`stream:debate_queue`, `stream:exit_debate_queue`) and stages trade allocations. |
| **Web Frontend** | Host Process | `3000` (Bound to `0.0.0.0`) | Next.js 16 / React 19 | High-density trading workstation with TradingView charts, real-time WebSocket feeds, research tear sheets, and operator control panel. Accessible over LAN. |
| **Orchestrator** | `infra-orchestrator-1` | Internal | Python / AsyncIO | Paper engine, mandate risk monitor (5 s), approval sweeper, autonomy gate evaluator. |
| **Learner** | `infra-learner-1` | Internal | Python / AsyncIO | Single instance: labels debate outcomes (1h/4h/24h/72h), writes and embeds reflections. |
| **Sentiment** | `infra-sentiment-1` | Internal network only | FastAPI / PyTorch CPU | FinBERT news-headline sentiment for the debate's news analyst. |
| **Notifier** | `infra-notifier-1` (profile `notifier`) | Outbound only | Python / httpx | Telegram approvals (TOTP for ADD_RISK), ntfy Critical alerts. |
| **Local model tier** | host systemd `ollama` | `172.17.0.1:11434` (docker bridge only) | Ollama | Gemma 4 12B for triage and summaries, EmbeddingGemma for embeddings. See [runbooks/local_ai_tier.md](runbooks/local_ai_tier.md). |
| **Primary Database** | `cryptonite_postgres` | `5432` | PostgreSQL 16 / pgvector | Durable state storage for users, strategies, backtest runs, order ledgers, and transactional outbox. |
| **Execution Redis** | `cryptonite_redis` | `6379` | Redis 7 Alpine | Ultra-low latency pub/sub bus for market data streams, order signals, and execution events (DB 0: Telemetry/Streams; DB 1: Agent Cache). |
| **Agent Redis** | `cryptonite_redis` (DB 1) | `6379` (Internal) | Redis 7 Alpine | Consolidated session cache and conversational memory for AI agents. |

### 1.2 Trading Venue Support (Spot vs. Futures)
Currently, Cryptonite is deployed end-to-end as a **Spot-only** trading platform. While the underlying execution engine (`BinanceCompositeAdapter`) supports both Spot and USD-M Futures order routing, the data pipelines are exclusively configured for Spot:
* **Ingestion:** `cryptonite_ingest` connects exclusively to the Binance Spot WebSocket (`wss://stream.binance.com:9443/stream`).
* **Onboarding:** Instrument onboarding queries `https://api.binance.com/api/v3/exchangeInfo` (Spot) and hardcodes the `market_type` to `SPOT` in the PostgreSQL database.
* **Analysis:** The backtester, AI agents, and scanners rely entirely on the Spot market data ingested by the aforementioned services. 

To enable Futures trading, the ingestion and onboarding pipelines must be updated to route `.PERP` pairs to the `fstream` WebSockets and `fapi` REST endpoints.

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

# AI Operations (AI gateway)
GEMINI_API_KEY=your_google_gemini_api_key
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1   # compose default

# Redis (requirepass; every client URL carries it)
REDIS_PASSWORD=a_long_random_password

# Approvals & notifier (see HANDOFF.md, Phase 5; the notifier's own secrets go in .env.notifier)
TELEGRAM_ALLOWED_CHAT_IDS=your_chat_id
NOTIFIER_HMAC_SECRET=random_32_bytes_hex
NOTIFIER_OPERATOR_USERNAME=admin

# Cryptography & Sessions
CRYPTONITE_TOTP_AES_KEY=your_base64_aes256_key
OPERATOR_SESSION_SECRET=your_random_session_secret

# Database & Cache URLs
DATABASE_URL=postgresql://cryptonite:cryptonite@localhost:5432/cryptonite
REDIS_URL=redis://:${REDIS_PASSWORD}@localhost:6379/0
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


---

## 9. AI Operations Architecture: Copilot, Debate Fleet & AI Brain Workspace

Cryptonite organizes quantitative AI operations into three deeply integrated, specialized operational layers that form the **Alpha Lifecycle**:

```mermaid
flowchart TD
    subgraph CopilotLayer ["1. Interactive AI Copilot (cryptonite_agent_worker:8085)"]
        Trader["Quant Operator"] -->|HTTP /agent-api| CopilotUI["Floating AI Copilot (AgentChatPanel)"]
        CopilotUI -->|FastAPI HMAC Proxy| Worker["Agent Worker Service"]
        Worker --> Research["Research Intelligence (Postgres + DuckDB)"]
        Worker --> Strategist["Execution Strategist (Nautilus Code Gen)"]
        Worker --> Explainer["Backtest Explainer (Tearsheet Audit)"]
        Worker --> CRO["Chief Risk Officer (Risk Bounds)"]
        Worker --> OnChain["On-Chain Analyst (Node RPC / Flows)"]
    end

    subgraph BrainWorkspace ["2. AI Brain Orchestrator & Action Inbox (AIBrainWorkspace)"]
        OrchestratorCanvas["Orchestrator Canvas (OrchestratorCanvas.tsx)"]
        ActionInbox["Action & Decision Inbox (ActionInbox.tsx)"]
        InlineCards["Inline Action Cards (ReactMarkdown Manifest Interceptor)"]
        TelemetryEngine["Live WebSocket Telemetry Engine (Redis HEARTBEAT)"]
        OrchestratorCanvas --> InlineCards
        OrchestratorCanvas --> TelemetryEngine
        InlineCards -->|Event: cryptonite:load-strategy| ResearchLab["Research Lab (Backtest UI)"]
        InlineCards -->|Trigger On-Demand| DebateWorker["Debate Worker Daemon"]
        InlineCards -->|Stage Order| ActionInbox
    end

    subgraph DebateLayer ["3. Autonomous Background Debate Fleet (cryptonite_debate_worker)"]
        ScannerTrigger["Polars Scanner Alert"] -->|stream:debate_queue| DebateWorker
        UIHeaderTrigger["Chart Header 'Run Debate'"] -->|stream:debate_queue| DebateWorker
        DebateWorker --> AnalystFleet["Market, Sentiment & On-Chain Analysts"]
        AnalystFleet --> DebateArena["Adversarial Bull vs. Bear Debate"]
        DebateArena --> RiskCommittee["Aggressive, Conservative & Neutral Risk"]
        RiskCommittee --> CROApproval["CRO Synthesis (Max 25% Allocation)"]
        CROApproval --> Staging["Execution Staging Canvas (PENDING_APPROVAL)"]
        Staging -->|Operator Approve (Hold 2s)| OutboxQueue["Transactional Outbox -> Order Execution"]
        OutboxQueue --> ReflectionEngine["Reflection Engine (Alpha vs BTC/ETH Feedback)"]
        ReflectionEngine -.->|Historical Lessons via pgvector| DebateArena
    end

    CopilotLayer -.->|1-Click Workflow Bridges| BrainWorkspace
    BrainWorkspace <--> DebateLayer
```

### 9.1 Interactive 1-on-1 AI Copilot (`cryptonite_agent_worker`)
* **Framework**: Google Antigravity SDK with Google Gemini (`gemini-2.5-pro` & `gemini-2.5-flash`).
* **Persona Isolation**: Fully independent state threads (`threads[mode]`) and input draft memory (`drafts[mode]`) across all 5 personas:
  1. **Research Intelligence**: Performs deep columnar SQL queries over partitioned Parquet historical ticks and live Postgres ingest batches.
  2. **Execution Strategist**: Generates AST-compliant Python strategy definitions inheriting from `CryptoniteStrategy` and `CryptoniteStrategyConfig`.
  3. **Backtest Explainer**: Audits simulation artifacts, parses tearsheets, and dynamically synthesizes Sharpe ratio and drawdown curves.
  4. **Chief Risk Officer (CRO)**: Evaluates capital limits, drawdown budgets, and margin utilization.
  5. **On-Chain & Whale Intelligence**: Analyzes exchange reserve changes, whale wallet accumulations, and funding rate divergences.
* **Resilience & Dependencies**: The worker container includes `redis` (async token bucket rate limiting) and `numpy` (DuckDB parquet math kernels), authenticated via rotating HMAC-SHA256 nonces.
* **Extended Gateway Timeout**: The Next.js `/agent-api/[...slug]` proxy implements a 300-second execution timeout and full `GET` / `POST` support, ensuring multi-step reasoning queries complete without premature 504 timeouts.

### 9.2 The Alpha Lifecycle Integration & AI Brain Workspace (`AIBrainWorkspace`)
The AI Brain workspace (`OrchestratorCanvas.tsx`, `ActionInbox.tsx`, `AgentFleetRoster.tsx`) unites the Copilot, Research Lab, and Execution Outbox into a continuous feedback loop:
1. **Interactive Strategy Manifest Cards (`InlineActionCard`)**:
   - `ReactMarkdown` automatically intercepts JSON code blocks containing strategy parameters (`instrument_id`, `max_position_notional_usd`, `allocation_usd`).
   - Renders rich interactive UI cards with:
     - **[🧪 Backtest in Lab]**: Dispatches a custom window event (`cryptonite:load-strategy`) containing the generated Python code, pre-filling strategy parameters into the Research Lab.
     - **[⚔️ Trigger Debate]**: Instantly triggers an adversarial debate on the newly formulated strategy.
     - **[📥 Stage to Inbox]**: Directly registers the strategy proposal into the Action Inbox.
2. **Inline Risk Allocation Sliders**:
   - Each manifest card includes an embedded `<AllocationSlider>` bounded between 1.0% and the institutional **25.0% maximum portfolio equity concentration ceiling** ($25,000 max on $100k equity).
3. **Live Redis WebSocket Telemetry Engine**:
   - Subscribes directly to Redis `HEARTBEAT` streams via `getStreamClient()`, capturing sub-agent latency and execution states in real-time.
   - Dynamically renders thought traces during query execution and persists the captured telemetry alongside message history, eliminating hardcoded mock delays.
4. **Dynamic Context Tags & Diagnostics**:
   - **`@Backtest(latest)`**: Injects system instructions for the LLM to call `read_nautilus_tearsheet` and ground its arguments in empirical backtest data (Sharpe Ratio, Max Drawdown).
   - **`/delta @PaperPosition(latest)`**: Injects directives to execute `compare_live_vs_backtest`, calculating real-time execution slippage and Sharpe degradation between live fills and backtest models.

### 9.3 Agent Tool Fleet & Security Guardrails (`cryptonite_agents.tools`)
All agents operate under strict Role-Based Access Control (`cryptonite_agent_ro` read-only database user):
* **`stage_debate_proposal`**: Safely stages debate and execution proposals by dispatching signed HTTP requests to the API gateway (`POST /debates/trigger`), maintaining database read-only constraints without privilege errors.
* **`compare_live_vs_backtest`**: Queries live PostgreSQL `agent_safe_orders` fills and joins them against DuckDB `trades.parquet` to calculate price slippage and drift.
* **`query_parquet_duckdb`**: Native DuckDB SQL execution optimized with `.fetchall()` to query raw Parquet lakehouse files without requiring Pandas.
* **`read_nautilus_tearsheet`**: Reads `equity_curve.parquet` and `trades.parquet` from the Nautilus run directory, returning structured KPI summaries (return %, max drawdown %, win rate, total trades).
* **`get_live_market_data`**: Fetches real-time price, orderbook depth, and 24h stats directly from Redis and Binance.
* **`query_postgres_readonly`**: Enforces strict read-only analytical queries against PostgreSQL with query length and timeout limits.

### 9.4 Action Inbox Exact Order Sizing & Database State Reconciliation
* **PostgreSQL Hydration (`GET /api/v1/agents/debates`)**: The Action & Decision Inbox actively hydrates historical and active debate proposals directly from the PostgreSQL database (`debate_runs` and `portfolio_decisions`), rather than relying solely on transient WebSocket events or volatile browser memory.
* **Bi-directional Cache Reconciliation**: The client-side state store (`useDebateStore.ts`) reconciles with the database on load, automatically purging locally cached ghost/orphaned items if they have been removed or truncated in PostgreSQL.
* **Card Hierarchy & Temporal Ordering**: Each proposal card features a dedicated stacked metadata header with formatted timestamps (`new Date(timestamp).toLocaleString()`) and clock icon indicators, ordered strictly by creation date with newest proposals at the top.
* **Exact Order Sizing Math**:
When approving staged debate proposals via `POST /debates/{run_id}/approve`:
- The API gateway queries `trading_balances` for live USDT account equity.
- Queries live mid-market prices via Redis or Binance.
- Derives exact base asset order quantity:
  $$\text{Quantity} = \frac{\text{Equity}_{\text{USDT}} \times (\text{Allocation}_{\%} / 100)}{\text{Price}_{\text{Market}}}$$
- Translates percentage allocations into exact orders before dispatching to `execution_commands`, ensuring the Nautilus executor receives fully validated, non-zero order quantities.

### 9.5 Autonomous TradingAgents Debate Engine (`cryptonite_debate_worker`)
* **Architecture**: Multi-agent adversarial consensus pipeline implementing the `TradingAgents` pattern.
* **Queue Topology**: Consumes from Redis Streams (`stream:debate_queue` for entry debates, `stream:exit_debate_queue` for position exit monitoring).
* **Consensus Workflow**:
  0. **Before any model call (Phase 2):** a deterministic pre-debate gate (kill switch, daily loss, headroom, freshness, cooldown) and a cheap **triage** call (local Gemma 4) decide whether a candidate is worth a debate at all.
  1. **Analyst Fleet**: Generates structured dossiers on technical indicators, news sentiment, and on-chain RPC flows.
  2. **Bull vs. Bear Debate**: Adversarial debaters challenge bullish and bearish assumptions using real telemetry.
  3. **Risk Committee & CRO Verdict**: Aggressive, Conservative, and Neutral managers propose position limits, capped by the Chief Risk Officer at a maximum 25% portfolio allocation.
  4. **Execution Staging (`PENDING_APPROVAL`)**: Staged trades appear directly in the terminal's docked debate sidebar or the Execution Staging Canvas, permitting 1-click execution or operator parameter overrides. Since Phase 5, verdicts are routed by the owning **mandate's autonomy level** (L1: approval required; L2: veto window; L3: acts), and nothing executes without an active mandate.
  5. **Reflection Feedback Loop**: Evaluates completed trade outcomes against BTC/ETH benchmarks and autonomously stores lessons learned in `reflections` to guide future debate rounds.

### 9.6 Vectorized Reflection Memory & Semantic Recall (`pgvector`)
* **Self-Improving Feedback Loop**: The `reflections` table records quantitative and qualitative trade post-mortems comparing actual trade PnL against benchmark BTC/ETH beta over identical time horizons.
* **Embeddings & Regime Indexing**: Uses 768-dimensional dense vector embeddings (`vector(768)`) generated locally by `ollama:embeddinggemma` (Google retired `text-embedding-004`); each row records its `embedding_model` and recall only compares same-model vectors. Since Phase 7 the learner writes reflections from labelled 24h outcomes and ranks recalled lessons by outcome:
  - Indexed by `instrument_type`, `volatility_regime`, `timeframe`, and `outcome` (`PROFITABLE`, `LOSS`, `BREAKEVEN`).
  - When new debate rounds initialize, the debate fleet performs cosine similarity nearest-neighbor queries to retrieve contextually relevant historical mistakes or winning heuristics, eliminating repetitive strategy pitfalls without manual code changes.

### 9.7 Dynamic Position Exit Monitoring (`stream:exit_debate_queue`)
* **Continuous Position Surveillance**: The `cryptonite_scanner` exit monitor continuously tracks open positions against trailing volatility and RSI/Bollinger exhaustion signals.
* **Exit Consensus Engine**: When exit conditions trigger, an exit debate event is pushed to `stream:exit_debate_queue`.
* **Exit Decisions Table (`exit_decisions`)**: Records exit debate recommendations, conviction scores, target stop prices, and detailed reasoning, enabling structured de-risking and automated trailing stops.

---

## 10. Modern Bloomberg-Style Workstation UI

Cryptonite features an institutional workstation engineered in Next.js 16 (React 19) with Tailwind CSS 4, Lightweight Charts 5, and Base UI components:

### 10.1 Modular Terminal Workspace (`TerminalWorkspace.tsx`)
* **Quick-Toggles Toolbar**: Dedicated toggles for **Watchlist**, **Orderbook**, **AI Debate**, and **Operator Control** with real-time mode indicators.
* **Integrated Operator Sidebar**: Operators can open the **Operator Control** sidebar directly inside the main Trading Terminal via the right toolbar toggle (red Shield icon). This embeds the full Kill-Switch panel alongside active charts, eliminating the need to navigate away from live market telemetry during emergency situations.
* **Layout Persistence**: All panel visibility settings persist across browser sessions in `localStorage` (`cryptonite:panel-watchlist`, `cryptonite:panel-orderbook`, `cryptonite:panel-debate`, `cryptonite:panel-operator`).
* **Fluid Multi-Chart Canvas**: Canvas automatically resizes to 100% of viewport when sidebars are collapsed, without canvas stutter or aspect ratio distortion.

### 10.2 Real-Time Orderbook L2 Depth (`OrderbookPanel.tsx`)
* **Live Price Anchoring**: Listens to WebSocket `open_kline` and `closed_bar_committed` feeds to continuously anchor mid-market price and dynamic spreads.
* **Depth Visualization**: Renders visual depth percentage bars for bid and ask tiers with smooth transition animations.
* **Tick Grouping Controls**: Traders can switch tick granularity between `0.1`, `1`, and `10` USDT steps with a single click.
* **SSR Hydration-Safe**: Employs an `isMounted` gate, deterministic initial seed (`0`), and explicit `en-US` number formatting to guarantee 0 hydration mismatches.

### 10.3 Docked AI Consensus Debate Sidebar (`DebateTranscriptViewer.tsx`)
* **Live Telemetry Stream**: Displays active debate status (`RUNNING`, `PENDING_APPROVAL`, `COMPLETED`) with pulsing indicators.
* **Role-Coded Hierarchy**: Color-coded cards for Sentiment Analyst, Bull/Bear Researchers, Portfolio Manager, and CRO.
* **Contained Markdown Tables**: All markdown tables and code blocks are wrapped in responsive horizontal scroll containers (`overflow-x-auto`, `break-words max-w-[200px]`), preventing layout breaking or overlapping columns on compact sidebars.

### 10.4 Freely Draggable & Resizable AI Copilot (`AgentChatPanel.tsx`)
* **8-Direction Resizing**: Resize from any edge (`n`, `s`, `e`, `w`) or corner (`ne`, `nw`, `se`, `sw`) with visual grip indicators.
* **Viewport Clamping & Re-centering**: Drag freely across the chart canvas or click the **Center Window** (`Crosshair`) button to snap back to center.
* **Text Selection & 1-Click Clipboard**: Full native text cursor selection (`select-text cursor-text`), per-message **Copy** buttons, and header **Copy All** transcript export.

### 10.5 High-Assurance Execution Staging Canvas (`ExecutionStagingCanvas.tsx`)
* **Stage Gate Workflow**: Triggered whenever an adversarial debate run transitions into `PENDING_APPROVAL` status.
* **Real-Time Price Drift**: Calculates delta between `decision_price` (at debate consensus) and streaming mid-market prices, warning the operator of slippage risk before execution.
* **Institutional Parameter Overrides**:
  - **Allocation Slider**: Adjustable position sizing from 0.5% up to the 25.0% institutional hard ceiling.
  - **Order Type**: Toggle between `Market` and `Limit` with configurable offset basis points.
  - **Stop-Loss Protection**: Dynamic downside stop-loss percentage override.
* **2-Second Hold-to-Approve**: Enforces a deliberate 2000ms press-and-hold interaction with animated progress feedback to prevent accidental execution, alongside an immediate 1-click **Reject** option.
* **Transactional Dispatch**: Dispatches approved commands via authenticated `POST /debates/{run_id}/approve` into PostgreSQL's `execution_commands` outbox.

### 10.6 Portfolio & Paper Execution Console (`PortfolioConsole.tsx`)
* **Real-Time Position Ledger & Allocation Tracking**: 
  - Subscribes to the `CHANNEL_POSITIONS` Redis WebSocket stream to receive zero-latency push updates of all active positions, account balance, total equity, and realized PnL.
  - Implements a dynamic **Capital Allocation & Concentration Risk Gauge**, enforcing the 25% single-asset institutional concentration limit with visual red/yellow/green threshold indicators.
* **Granular AI Execution Attribution**:
  - Replaces ambiguous conviction stats with precise "Origin" intent tagging. 
  - Features an animated **Position Attribution Drawer** (slide-over panel) detailing exact AI debate origins (`AI_DEBATE_WORKER`), entry vs. mark prices, and verifiable risk invariant checks.
* **Multi-Tab Execution Journals**:
  - **Execution Orders Journal**: Logs all generated intents and order states (Pending, Filled, Canceled). Operators can instantly abort `PENDING` orders with 1-click execution.
  - **Executed Trades Ledger**: Reconciles the historical trade log including side, fill prices, and executed commissions mapped directly to the PostgreSQL persistence layer.
* **Strategy Operations & Emergency Overrides**:
  - **Dynamic Deploy Modal**: Populates active registered strategies securely from `/api/v1/strategies`.
  - **Optimistic Strategy Controls**: Operators can `Pause`, `Resume`, or `Stop` any running algorithmic strategy instance. The interface employs optimistic UI updates to instantly reflect transitions while securely dispatching commands to the orchestrator.
  - **Emergency Flatten All**: An overarching institutional stop-switch that concurrently dispatches `CLOSE_POSITION` commands for all `OPEN` inventory across every running strategy.
  - **Strategy Details Drawer**: A dedicated slide-over panel for each running strategy instance, exposing runtime variables, resolved configurations, status, and direct shortcuts to the strategy-specific operations dashboard.

### 10.7 Industrial Execution Kill-Switch Panel (`KillSwitchPanel.tsx`)
* **Physical Safety Aesthetic**: Completely redesigned with industrial control-room visual cues, deep shadows, and state-dependent glow dynamics.
  - **Armed Normal Mode**: Emerald ambient border glow with active system telemetry.
  - **Latched Emergency Mode**: High-contrast flashing hazard striping (`repeating-linear-gradient`) and intense rose/red danger illumination across the workspace.
* **Tactile Mechanical Switch Controls**: Prominently styled, oversized action trigger featuring one-touch trip mechanics with mandatory reason tracking and generation-counter increments.
* **Dual Execution Surfaces**: Rendered both within the full `/operator` management dashboard and the docked slide-out panel inside `TerminalWorkspace.tsx`.

---

## 11. Security, Authentication & Risk Governance

### 11.1 Machine & Operator Authentication
* **Unified Auth Dependency (`routes/auth.py`)**: Supports HttpOnly session cookies for operator browser interfaces, as well as bearer/header machine tokens (`X-API-Token`).
* **Deterministic SHA-256 Token Hashing**: Machine tokens are hashed natively in Python via `hashlib.sha256(token.encode('utf-8')).hexdigest()` before matching against `machine_api_tokens.token_hash` in PostgreSQL. This eliminates asyncpg type inference ambiguity (avoiding `DataError` on raw bytea/text conversion) and guarantees reliable high-frequency polling by automated monitoring services.
* **Role-Based Privilege Escalation**: Read-only access for agent fleet workers, mandatory CSRF checks for cookie-based state modifications, and operator/admin role gates on all emergency execution endpoints.
