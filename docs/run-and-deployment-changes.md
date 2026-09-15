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
