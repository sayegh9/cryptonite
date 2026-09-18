# Cryptonite

Cryptonite is a self-hosted cryptocurrency intelligence and algorithmic trading platform designed for auditable research, deterministic risk controls, and automated execution with NautilusTrader.

## Architecture Core Principles
- **AI and UI can propose.**
- **Deterministic rules and risk controls decide.**
- **NautilusTrader executes.**

## Project Structure
```
cryptonite/
├── apps/
│   └── web/             # Next.js 14 Institutional Web Terminal UI
├── docs/                # Architecture specifications & data contracts
├── infra/               # Docker Compose, Dockerfiles, and Alembic migrations
├── packages/
│   ├── schemas/         # Shared domain models and abstract source interfaces
│   ├── storage/         # Nautilus ParquetDataCatalog and Enriched Analytics Lake
│   ├── execution/       # Order State Machine and Risk Constraints
│   ├── backtester/      # Nautilus Backtesting Engine wrapper
│   ├── strategies/      # Base implementations of Quantitative Strategies
│   └── agents/          # Antigravity SDK & Gemini Agent Multi-Agent Fleet
└── services/
    ├── api/             # FastAPI Gateway, REST, WebSocket & Background Alerts
    ├── ingest/          # Binance WebSocket stream and REST recovery recorder
    ├── scanner/         # Polars Vectorized Screening Engine
    └── outbox/          # Transactional Outbox Poller & Binance Execution Bridge
```

## Quick Start
```bash
# 1. Start local PostgreSQL
docker compose -f infra/docker-compose.yml up -d postgres

# 2. Run migrations
uv run alembic -c infra/migrations/alembic.ini upgrade head

# 3. Run test suite
uv run pytest
```
