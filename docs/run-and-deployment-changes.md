# Cryptonite: Runtime Operations & Session Changes Log

**Date**: September 8, 2026  
**Subject**: Running Cryptonite, Dependency Bug Fixes, and Server Teardown

---

## 1. Executive Summary

This document records all changes, build outputs, and lifecycle operations performed during the session to execute and subsequently stop the complete **Cryptonite** system (backend services, simulation nodes, and web dashboard).

---

## 2. Changes Made to Codebase & Configurations

During initial container initialization, `cryptonite_api` failed on startup with the following exception:

```text
ModuleNotFoundError: No module named 'cryptonite_backtester'
  File "/app/services/api/cryptonite_api/routes/backtests.py", line 11, in <module>
    from cryptonite_backtester.execution_profiles import VALID_PROFILES, resolve_execution_profile
```

### 2.1. Dependency Specification Update
- **File Modified**: [`services/api/pyproject.toml`](file:///home/samx/Documents/cryptonite/services/api/pyproject.toml)
- **Change**: Added missing internal workspace dependencies required by the API routes:
  - `"cryptonite-strategies"` (needed by `routes/strategies.py` and `routes/backtests.py`)
  - `"cryptonite-backtester"` (needed by `routes/backtests.py`)

```diff
 dependencies = [
     "fastapi>=0.115.0",
     "uvicorn[standard]>=0.31.0",
     "asyncpg>=0.29.0",
     "redis>=5.0.0",
     "duckdb>=1.1.0",
     "polars>=1.12.0",
     "pydantic>=2.9.0",
     "cryptonite-schemas",
     "cryptonite-storage",
     "cryptonite-scanner",
+    "cryptonite-strategies",
+    "cryptonite-backtester",
 ]
```

### 2.2. Docker Build Manifest Update
- **File Modified**: [`infra/docker/api.Dockerfile`](file:///home/samx/Documents/cryptonite/infra/docker/api.Dockerfile)
- **Change**: Added `packages/strategies` and `packages/backtester` to the layer copy operations and `uv pip install` command so the Python packages are available inside the container runtime:

```diff
 COPY pyproject.toml README.md ./
 COPY packages/schemas ./packages/schemas
 COPY packages/storage ./packages/storage
 COPY packages/scanner ./packages/scanner
+COPY packages/strategies ./packages/strategies
+COPY packages/backtester ./packages/backtester
 COPY services/ingest ./services/ingest
 COPY services/outbox ./services/outbox
 COPY services/scanner ./services/scanner
 COPY services/api ./services/api

-RUN uv pip install --system -e ./packages/schemas -e ./packages/storage -e ./packages/scanner -e ./services/api
+RUN uv pip install --system \
+    -e ./packages/schemas \
+    -e ./packages/storage \
+    -e ./packages/scanner \
+    -e ./packages/strategies \
+    -e ./packages/backtester \
+    -e ./services/api
```

---

## 3. Build & Runtime Artifacts Generated

1. **Docker Container Images Built**:
   - `infra-scanner:latest`: Scanner engine and background evaluation worker
   - `infra-outbox:latest`: Transactional outbox relay daemon
   - `infra-executor:latest`: NautilusTrader 2.0.0rc4 LiveNode paper trading engine
   - `infra-api:latest`: FastAPI Gateway and WebSocket stream hub (rebuilt with backtester/strategies packages)
2. **Next.js Production Build**:
   - Compiled and optimized web dashboard located in `apps/web/.next` using Turbopack.

---

## 4. Services Started and Verified During Execution

The entire platform was brought online and verified healthy:

| Service | Technology | Port / Endpoint | Health Check Result |
| :--- | :--- | :--- | :--- |
| **Web Console** | Next.js 16 + React 19 | `http://localhost:3000` | HTTP 200 OK |
| **API Gateway** | FastAPI / Uvicorn | `http://localhost:8000` | `{"status":"ok"}` & `{"status":"ready"}` |
| **Ingest Engine** | Python / Binance WS | `http://localhost:8080` | `{"status":"ok"}` (Stream connected, 2 instruments recovered) |
| **Outbox Relay** | Python Asyncpg / Redis | `http://localhost:8081` | `{"status":"ok", "published_count": 141}` |
| **Scanner Worker** | Python Polars / DuckDB | `http://localhost:8082` | `{"status":"ok", "executed_runs": 6, "detected_candidates": 140}` |
| **Backtester Node** | Python Nautilus Sandbox | `http://localhost:8083` | `{"status":"ok"}` (Isolated bridge network) |
| **Paper Executor** | Nautilus 2.0.0rc4 LiveNode | `http://localhost:8084` | `{"status":"ok"}` (Zero-DB hot loop active) |
| **PostgreSQL 16** | PostgreSQL Alpine | `127.0.0.1:5432` | Healthy (Migration version 005 head) |
| **Redis 7** | Redis Alpine | `127.0.0.1:6379` | Healthy (Active streams & bus channels) |

---

## 5. Teardown Operations (All Servers Stopped)

In response to the stop request, all processes were cleanly terminated:
1. **Frontend Server**:
   - Cancelled background Next.js process (`pnpm start`, PID/task-209).
   - Confirmed port `3000` was released.
2. **Backend Containers**:
   - Executed `docker compose -f infra/docker-compose.yml stop`.
   - Stopped all 8 containers:
     - `cryptonite_api`
     - `cryptonite_outbox`
     - `cryptonite_scanner`
     - `cryptonite_executor`
     - `cryptonite_backtester`
     - `cryptonite_ingest`
     - `cryptonite_redis`
     - `cryptonite_postgres`
3. **Port Verification**:
   - Scanned all relevant platform ports (`3000`, `5432`, `6379`, `8000`, `8080`, `8081`, `8082`, `8083`, `8084`).
   - Confirmed all ports are closed and released.

---

## 6. Commands to Run & Stop Cryptonite in the Future

### To Start All Services:
```bash
# 1. Start backend containers (daemonized)
docker compose -f infra/docker-compose.yml up -d

# 2. Start web frontend (port 3000)
cd apps/web && pnpm start
# (or 'pnpm dev' for hot reloading development mode)
```

### To Stop All Services:
```bash
# 1. Stop web frontend
# Press Ctrl+C in the terminal running Next.js (or kill the process)

# 2. Stop backend containers
docker compose -f infra/docker-compose.yml stop
```

---

## 7. Update Log: September 18–19, 2026 (AI Operations, Network Access & Production Fixes)

### 7.1. Database Migration 008 (`008_market_data_and_alerts.py`)
- Added nullable `coingecko_id VARCHAR(64)` column to `instruments` for third-party market capitalization and pricing resolution.
- Created `user_alerts` table for price threshold tracking (`instrument_id`, `condition`, `threshold`, `is_active`).
- Added explicit `GRANT SELECT, INSERT, UPDATE, DELETE` to `cryptonite_api`.

### 7.2. Cryptonite AI Multi-Agent Operations Fleet
- **Microservice Container**: Built and deployed `cryptonite_agent_worker` (`infra/docker/agent_worker.Dockerfile`) listening on port `8085`.
- **Inter-Service Security**: Implemented HMAC-SHA256 request signing between `cryptonite_api` (FastAPI) and `cryptonite_agent_worker` using rotating timestamps and nonces (`X-Internal-Timestamp`, `X-Internal-Nonce`, `X-Internal-Signature`).
- **Database Least Privilege**: Injected `cryptonite_agent_ro` read-only role with `default_transaction_read_only = 'on'`.
- **DuckDB Analytical Sandboxing**: Added keyword blacklists (`DROP`, `DELETE`, `COPY`, `httpfs`) and strict filesystem containment checks relative to `CATALOG_PATH`.
- **Extended Timeout Proxy**: Added `/agent-api/[...slug]/route.ts` in Next.js with a 300-second timeout to prevent gateway 504 drops on complex multi-step reasoning tasks.
- **Tearsheet Synthesis**: Added DuckDB fallback in `read_nautilus_tearsheet` to dynamically calculate Sharpe ratio, Max Drawdown %, win rate, and total return directly from `equity_curve.parquet` and `trades.parquet` when `tearsheet.json` is missing.
- **Nautilus Strategy Generator**: Built zero-shot strategy synthesizer and added a one-click "Save Strategy" button directly in `AgentChatPanel.tsx`.

### 7.3. Runtime Bug Fixes & Stability Hardening
1. **TradingView Chart `NaN` Assertion Fix**:
   - Resolved `AssertionError: Candlestick series item data value of open must be between -90071992547409.91 and 90071992547409.91, got=number, value=NaN` in `TradingViewChart.tsx`.
   - Cause: `closed_bar_committed` events publish bar metadata without OHLCV fields. The subscriber now safely skips OHLCV updates on committed events lacking price data.
2. **ScannerStudio TypeError Fix**:
   - Resolved `TypeError: Cannot read properties of undefined (reading 'split')` in `ScannerStudio.tsx`.
   - Cause: `signal_candidate` events nest payload inside `msg.data`. Unnested `cand = cand.data || cand` and added defensive string splitting.
3. **Async Instrument Onboarding Migration**:
   - Migrated `services/api/cryptonite_api/routes/instruments.py` from synchronous `urllib.request` + `run_in_executor` to native `httpx.AsyncClient`.
4. **Duplicate Variable Cleanup**:
   - Removed duplicate `ALLOWED_ORIGINS` definition in `stream_gateway.py` and corrected `ClientConnection.subscriptions` type annotation to `set[str]`.

### 7.4. Local Area Network (LAN) & Multi-Device Access
- **API Port Binding**: Changed `ports:` under `api:` in `infra/docker-compose.yml` from `"127.0.0.1:8000:8000"` to `"0.0.0.0:8000:8000"`.
- **Next.js `allowedDevOrigins`**: Added `"192.168.1.203"` and `"192.168.1.*"` to `apps/web/next.config.mjs` to permit cross-device Turbopack dev connections.
- **WebSocket Host Resolution**: Refactored `apps/web/src/lib/ws.ts` to derive host from `window.location.hostname`, enabling remote devices to stream market feeds from `ws://192.168.1.203:8000/ws/stream`.
- **Compiled PDF Manual**: Produced a 17-page illustrated manual saved at `docs/Cryptonite_User_Manual.pdf`.


---

## 8. Update Log: October 2026 (Linux workstation, AI-First Phases 5–7)

The stack moved from the Mac to the Linux workstation (HP Z840, NVIDIA GPU). Full status is in
[HANDOFF.md](../HANDOFF.md); the architecture is in [ai-first-program.md](ai-first-program.md).

### 8.1. Configuration
- **`.env` location:** the root `.env`; `infra/.env` is a git-ignored symlink to `../.env`, because Compose
  interpolates `${...}` from the compose file's directory.
- **Redis password:** `redis-server --requirepass ${REDIS_PASSWORD}` with an authenticated healthcheck;
  all client URLs carry the password. Host tools: `redis-cli -a "$REDIS_PASSWORD"` (read it from `.env`,
  never paste it into chats or logs).
- **Local model tier:** Ollama as a host systemd service on the docker bridge only; see
  [runbooks/local_ai_tier.md](runbooks/local_ai_tier.md).
- **Profiles:** `executor` now needs `--profile binance_demo` (it crash-looped without a PEM key);
  `exit_monitor` is `--profile monitoring`, `notifier` is `--profile notifier`, `research_worker` is
  `--profile research`.

### 8.2. Images (LE-7)
Every Python image runs `uv sync --frozen --no-dev --all-packages` into `/app/.venv` with uv 0.12.17 and a
shared BuildKit cache (`id=cryptonite-uv`, `UV_LOCK_TIMEOUT=3600`), so deployed versions match the lockfile.
Build `api` first when building many images in parallel; the shared cache lock serialises the rest.

### 8.3. Migrations
Run them through the `test` image (the `api` image has no Alembic):

```bash
docker compose -f infra/docker-compose.yml run --rm --no-deps test uv run python -m alembic -c infra/migrations/alembic.ini upgrade head
```

Current code head on `origin/main` is **`040`** (linear `036 → 037` (B2 verdicts) → `038` (C3) →
`039` (D1 outbox) → `040` (B4 recovery); revisions 032–036 also apply). _Refreshed 2026-10-08 (A6
audit)._ Migrate before deploying new images: the orchestrator and API read tables from 027–040. The
**dev DB is still at Alembic `036`** — no live migration has been applied; a code-head revision is not
production revision evidence. Production reaches 037–040 only through the controlled E1 sequence
(`docs/HANDOVER-2026-10-08.md`, `docs/operator-release-checklist.md`).

### 8.4. Tests
Never against the dev DB: use a throwaway `cryptonite_ci` database (command in HANDOFF.md). Run the
Playwright suite with the live API stopped; one flicker test fails only when the real API answers.
Last gates: pytest 804 passed (0 failed), Playwright 26/26.

### 8.5. Data
- History: `scripts/backfill_history.py` (catalog + lake, gaps only); 120 days of BTC/ETH 1m and 1h.
- `scan_runs` had grown to 21 GB (one sha256 per lake file per run); the scanner now stores one digest per
  date partition and the table was compacted to about 250 MB.
- macOS copied `._*` AppleDouble files into `data/`; DuckDB crashes on them. Delete them after any copy
  from a Mac: `find data -name '._*' -delete`.
