# Cryptonite: Institutional User Manual & Operations Guide
## A Complete Step-by-Step Guide with Live System Walkthroughs

---

## Table of Contents
1. [Welcome & Quick Start Guide](#1-welcome--quick-start-guide)
2. [Trading Terminal & Live Market Data](#2-trading-terminal--live-market-data)
3. [Vectorized Polars Scanner Studio](#3-vectorized-polars-scanner-studio)
4. [Quantitative Research Lab & Backtesting](#4-quantitative-research-lab--backtesting)
5. [Portfolio Console & Risk Analytics](#5-portfolio-console--risk-analytics)
6. [AI Copilot & Multi-Agent Operations](#6-ai-copilot--multi-agent-operations)
7. [Operator Control Plane & Emergency Governance](#7-operator-control-plane--emergency-governance)
8. [Troubleshooting & Frequently Asked Questions](#8-troubleshooting--frequently-asked-questions)

---

## 1. Welcome & Quick Start Guide

**Cryptonite** provides quantitative traders, risk officers, and portfolio managers with an integrated workstation for systematic trading.

### 1.1 Service Access Points
* **Web Trading Workstation (Local)**: [http://localhost:3000](http://localhost:3000)
* **Web Trading Workstation (LAN Network)**: [http://192.168.1.203:3000](http://192.168.1.203:3000)
* **Operator Governance Portal**: [http://localhost:3000/operator](http://localhost:3000/operator) or via the **Operator** tab
* **WebSocket Market Stream**: `ws://localhost:8000/ws/stream` (or `ws://192.168.1.203:8000/ws/stream` from LAN)
* **FastAPI Backend Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
* **AI Operations Gateway**: [http://localhost:8085/health](http://localhost:8085/health)

### 1.2 Default Operator Credentials
* **Operator ID**: `admin`
* **Master Password**: `admin123`
* **Two-Factor TOTP**: `000000` *(development bypass)*
* **Execution Environment**: Binance Demo / Testnet (Isolated paper risk)

---

## 2. Trading Terminal & Live Market Data

The **Trading Terminal** is your primary workspace for active market observation, multi-asset price action, and automated intelligence.

![Trading Terminal](./screenshots/01_trading_terminal.png)

### Key Features & Workflow:
1. **Bloomberg-Style 3-Column Resizable Workspace**: Features an interactive TradingView chart, live orderbook depth panel, and AI Consensus Debate sidebar.
2. **Dynamic Sidebar & Canvas Controls**: Click `Show Orderbook` or `Show AI Debate Sidebar` in the toolbar to toggle sidebars. Closing sidebars automatically expands the chart canvas to 100% full width. Layout preferences are persisted in `localStorage`.
3. **On-Demand Consensus Debate**: Click `[Trigger AI Consensus Debate]` on the chart header or press `⌘D` in the Command Palette to launch a multi-agent Bull vs. Bear debate for the active ticker.
4. **Timeframe Selector**: Toggle effortlessly between `1m`, `5m`, `15m`, `1h`, `4h`, and `1d` intervals.
5. **Multi-Asset Watchlist**: View live 24h ticker updates, spread dynamics, and quick-switch between major trading pairs (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`).
6. **Sub-millisecond Latency Bar**: Bottom telemetry displays DuckDB p95 query latency (~12ms) and Polars vectorized execution speeds (~8ms).

---

## 3. Vectorized Polars Scanner Studio

The **Scanner Studio** allows you to scan thousands of historical and real-time candles in single-digit milliseconds using Polars expression kernels.

![Scanner Studio & Builder](./screenshots/02_scanner_builder.png)

### Step-by-Step: Creating a Custom Scanner
1. In the Terminal view, click the cyan **Create Scanner** button on the Scanner Studio toolbar.
2. Configure your scanning criteria:
   * **Scanner Name & Description**: e.g., `Oversold RSI Reversal`.
   * **Target Instrument & Timeframe**: Select `BTCUSDT` and `1m` or `1h`.
   * **Indicator Rule Expression**: Define vectorized filters such as:
     * `RSI(14) < 30`
     * `Close < Lower_Bollinger_Band(20, 2.0)`
     * `Volume > SMA(Volume, 20) * 2.5`
3. Click **Save & Activate Scanner**.
4. Click **Run Scanner** to immediately execute the Polars kernel across the Parquet data lake. Click any detected opportunity to auto-center the TradingView chart on that exact timestamp.

---

## 4. Quantitative Research Lab & Backtesting

The **Research Lab** executes institutional-grade quantitative backtests powered by the **Nautilus Trader** discrete-event simulation engine.

![Research Lab](./screenshots/03_research_lab.png)

### Step-by-Step: Executing a Quantitative Backtest
1. Navigate to the **Research Lab** tab on the top navigation bar.
2. Select your quantitative strategy (e.g., `EMA Cross`, `Breakout Volatility`).
3. Tune parameter thresholds (e.g., Fast EMA `9`, Slow EMA `21`).
4. Select an **Execution Profile**:
   * **Realistic**: 50ms simulated network latency, 1 bps exchange fee, and dynamic slippage.
   * **Perfect**: Zero-friction theoretical benchmark.
   * **High Volatility**: Stress-test against adverse fill distributions and widening spreads.
5. Click **Run Quantitative Backtest**.
6. The engine validates data cadence across the Parquet catalog, runs the simulation, and outputs:
   * **Tearsheet KPI Cards**: Sharpe Ratio, Sortino Ratio, Calmar Ratio, Max Drawdown %, Win Rate, and Total PnL.
   * **Equity Curve Chart**: Visualizes portfolio cumulative return vs. benchmark.
   * **Monthly Returns Heatmap**: Breaks down alpha by month and year.
   * **Trades Ledger**: Chronological audit of every simulated execution with entry price, exit price, and fees.

---

## 5. Portfolio Console & Risk Analytics

The **Portfolio Console** gives risk managers real-time visibility into overall capitalization, leverage utilization, and perpetual futures positions.

![Portfolio Console](./screenshots/04_portfolio_console.png)

### Key Metrics Tracked:
* **Total Account Equity**: Real-time aggregated collateral valued in USDT.
* **Margin Utilization Ratio**: Gauge representing used margin against maintenance margin liquidation thresholds.
* **Open Positions Ledger**: Details symbol, entry price, mark price, liquidation price, notional value, leverage bracket, and unrealized PnL.
* **Asset Allocation Breakdown**: Interactive pie chart displaying capital distribution across spot assets and margin collateral.

---

## 6. AI Copilot & Multi-Agent Operations

The **AI Copilot** provides an autonomous quantitative operations analyst directly inside your trading workspace, powered by the **Google Antigravity SDK and Google Gemini**.

![AI Copilot Active Chat](./manual_images/06b_ai_chat_detail.png)

### Floating Window & Persona Controls:
- **Freely Draggable & Resizable**: Drag the floating panel via its header bar and resize from the bottom or right handles. Toggle docking to lock it to the right sidebar.
- **5 Specialized AI Personas**:
  1. **Research Agent (`Senior Quantitative Analyst`)**: Technical market structure, volume profile, RSI trends, and level discovery.
  2. **Strategist Agent (`Execution & Strategy Architect`)**: Writes production-ready Nautilus Python code (`StrategyConfig` & `Strategy`). Includes a 1-click green **"Save Strategy"** button.
  3. **Explainer Agent (`Risk & Performance Manager`)**: Ingests backtest run tearsheets (`trades.parquet`, `equity_curve.parquet`) to analyze Sharpe ratio, drawdowns, and regime bias.
  4. **CRO Agent (`Chief Risk Officer`)**: Evaluates portfolio risk, leverage limits, and position concentration bounds.
  5. **On-Chain Agent (`On-Chain Intelligence`)**: Analyzes exchange inflows, netflow anomalies, and whale liquidity movements.
- **Isolated Threads & Draft Storage**: Each persona maintains a separate conversation history and draft input box. Switching persona tabs preserves your un-sent text.
- **1-Click Text & Code Copying**: Select text directly or click **Copy Code** to copy Python snippets into your clipboard.

### 6.1 Market Intelligence & Discovery
The platform includes a full-featured Market Intelligence suite:
- **Command Palette**: Press `Ctrl/Cmd + K` anywhere in the app to quickly search for assets, run commands, or trigger debates (`⌘D`).
- **Watchlists**: Pin your favorite instruments to the sidebar for real-time tracking.
- **Alerts Engine**: Set custom price alerts evaluating in the background with real-time WebSocket breach notifications.
- **Extended Timeout**: `/agent-api` Next.js proxy route implements a 300s timeout to support deep multi-step quantitative agent queries without gateway timeouts.

### Example Prompts to Try:
* *"Analyze the hourly volume distribution of BTCUSDT over the past 30 days."*
* *"Evaluate backtest run `latest` and explain why max drawdown reached 14%."*
* *"Generate a complete Nautilus Trader strategy in Python that trades 15m Bollinger Band breakouts with an ATR trailing stop."*
* *"Check risk bounds for a 10x leveraged long position on ETHUSDT given current volatility."*

---

## 7. Operator Control Plane & Emergency Governance

The **Operator Control Plane** provides authorized personnel with full operational governance over live capital and strategy execution.

### 7.1 Operator Authentication
Navigate to `/operator` or click the **Operator** tab in the main header. Enter your operator credentials:
* **Operator ID**: `admin`
* **Password**: `admin123`
* **TOTP Code**: `000000` *(development bypass)*

![Operator Authentication](./manual_images/00_operator_login.png)

### 7.2 Operator Dashboard Overview
Once authenticated, the console displays live execution status, the global kill switch, active strategy instances, and the multi-asset audit ledger.

![Operator Dashboard](./screenshots/07_operator_dashboard.png)

### 7.3 The Global Execution Kill-Switch
* **Armed State**: Green indicator confirms normal execution mode.
* **Tripping the Kill-Switch**: In an emergency or severe market anomaly, click the red **TRIP KILL-SWITCH** button:
  1. Instantly terminates all live strategy loops.
  2. Cancels all outstanding limit and stop orders on connected exchanges.
  3. Rejects any new orders with HTTP 423 Locked.
* **Resume Execution**: Requires manual operator re-authentication to disarm and reset generation counters.

### 7.4 Deploying a New Strategy Instance
Click **Deploy New** in the Active Strategy Instances card:

![Deploy Strategy Modal](./screenshots/08_deploy_strategy_modal.png)

1. Choose the strategy definition and target instrument (`BTCUSDT.BINANCE`).
2. Allocate starting trading capital and define maximum position size.
3. Configure leverage bracket and margin mode (`CROSS` or `ISOLATED`).
4. Click **Deploy Strategy** to spin up the execution worker.

### 7.5 Executing a Manual OTC / Emergency Trade
Click **Manual Trade** to dispatch direct orders to the exchange:

![Manual Trade Modal](./screenshots/09_manual_trade_modal.png)

1. Select order side (`BUY` or `SELL`).
2. Enter target quantity and select order type (`LIMIT` or `MARKET`).
3. Set execution style (`POST_ONLY`, `IOC`, or `FOK`).
4. Review calculated notional value and click **Submit Order**.

### 7.6 Multi-Asset Reconciliation Ledger
The bottom panel runs continuous zero-tolerance audits comparing Binance exchange balances and open orders against internal PostgreSQL records. Any discrepancy is flagged immediately with an audit run ID for post-mortem analysis.

### 7.7 System Configuration & Settings Dashboard
Cryptonite features a dynamic System Configuration panel accessed via the **Settings** (Gear icon) tab in the main sidebar.
* **Agent Prompt Customization**: Dynamically overwrite the system instructions, LLM model choice (e.g., `gemini-2.5-pro` vs `gemini-2.5-flash`), and temperature for all AI Agents. Changes take effect instantly without restarting containers.
* **Environment & Security Health**: Provides a read-only audit view of critical `.env` configurations, including the presence of Gemini and Binance API keys, the active Environment Mode (DEMO vs PROD), and the Kill-Switch status.

---

## 8. Remote LAN & Multi-Device Access Guide

Cryptonite can be accessed and operated from secondary devices (laptops, iPads, smartphones) on your local network:

1. **Verify Host Network Binding**: The API service in `docker-compose.yml` binds to `0.0.0.0:8000:8000`, making REST and WebSocket `/ws/stream` reachable.
2. **Connect from Secondary Device**: Open any web browser on your tablet or laptop (connected to the same Wi-Fi) and navigate to:
   ```
   http://192.168.1.203:3000
   ```
3. **Log In**: Enter operator credentials (`admin` / `admin123` / TOTP: `000000`).
4. **Live Streaming**: The Next.js frontend automatically resolves the host and opens a WebSocket stream to `ws://192.168.1.203:8000/ws/stream`.
5. **PDF User Manual**: You can download or view the compiled 17-page illustrated manual at `docs/Cryptonite_User_Manual.pdf`.

---

## 9. Troubleshooting & Frequently Asked Questions

### Q1: Why does the AI Copilot show "API Key Required"?
Ensure your Google Gemini API key is configured in the root `.env` file under `GEMINI_API_KEY`. The `docker-compose.yml` automatically passes this to the `cryptonite_agent_worker` container.

### Q2: What causes "Out of order or duplicate bars detected" in the backtester?
Nautilus Trader requires strict chronological monotonicity. If this occurs, the data catalog contains overlapping parquet files. The updated `cryptonite_backtester` cadence engine automatically sorts and deduplicates timestamps before starting.

### Q3: How do I verify Docker service health?
Run `docker compose ps` from the `infra/` folder. All services (`postgres`, `redis`, `api`, `ingest`, `scanner`, `backtester`, `executor`, `agent_worker`) should show `Up` or `Healthy`.

### Q4: Cannot connect from another device on the Wi-Fi?
Verify that the host firewall permits ingress on ports `3000` (Next.js) and `8000` (API Gateway / WebSocket). Ensure both devices are on the `192.168.1.0/24` subnet.

