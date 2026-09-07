# Cryptonite

Cryptonite is a self-hosted cryptocurrency intelligence and algorithmic trading platform designed for auditable research, deterministic risk controls, and automated execution with NautilusTrader.

## Architecture Core Principles
- **AI and UI can propose.**
- **Deterministic rules and risk controls decide.**
- **NautilusTrader executes.**

## Project Structure
```
cryptonite/
├── docs/                # Architecture specifications & data contracts
├── infra/               # Docker Compose, Dockerfiles, and Alembic migrations
├── packages/
│   ├── schemas/         # Shared domain models and abstract source interfaces
│   └── storage/         # Nautilus ParquetDataCatalog and Enriched Analytics Lake
├── services/
│   └── ingest/          # Binance WebSocket stream and REST recovery recorder
└── tests/               # Offline fixtures and integration test suite
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
