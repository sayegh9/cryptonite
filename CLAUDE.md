# CLAUDE.md

## Stack
- Web: Next.js 16 (App Router), React 19, TS 5, Tailwind v4, Zustand v5 — pnpm@10
- Backend: Python 3.12 (>=3.12,<3.15), FastAPI, Postgres 16+pgvector, Redis 7, NautilusTrader 2.0.0rc4, DuckDB, Polars — uv workspace
- Infra: Docker Compose, Alembic

## Commands
- Web (`apps/web`): `pnpm typecheck` · `pnpm lint` · `pnpm build` · `npx playwright test`
- Python: `uv run pytest tests -q` · `uv run ruff check .` (uv resolve fails on macOS x86_64; use Docker/arm64)
- Stack: `docker compose -f infra/docker-compose.yml up -d`
- DB: `docker compose -f infra/docker-compose.yml run --rm --no-deps test uv run python -m alembic -c infra/migrations/alembic.ini upgrade head` (`--no-deps`: never converge/recreate postgres and redis)

## Topology
- `apps/web/src/{app,components,lib/stores}` — pages+API proxy, workspaces, persisted stores
- `apps/web/e2e` — Playwright specs
- `packages/schemas` — Pydantic contracts (bar, signal, order, debate)
- `packages/storage` — DuckDB/Postgres query helpers, Nautilus catalog
- `packages/scanner` — Polars indicator engine
- `packages/strategies` — strategy base + registry
- `packages/backtester` — Nautilus runner, cadence validation
- `packages/execution` — risk engine, OCO, demo/paper pipeline
- `packages/agents` — debate fleet + agent tools
- `packages/database/seeds` — seed data
- `services/{api,ingest,scanner,executor,orchestrator,outbox,backtester,debate_worker,agent_worker,notifier,sentiment}` — deployable workers (`learner` runs from `debate_worker`)
- `packages/agents/cryptonite_agents/gateway` — the only path to models (Gemini + local Ollama); see `docs/ai-first-program.md`
- `infra/{docker,migrations,security}` — images, Alembic, policies
- `tests/` — Python integration/contract suites

## Architecture Rules
- Dependency direction: `services → packages`; `packages/schemas` imports no sibling package; web reaches backend only via `/api/v1` and the `/agent-api` proxy.
- Never bypass `GuardedOrderDispatcher` (`packages/execution/cryptonite_execution/risk.py`); 1–25% equity clamp is invariant.
- AI orders need an ACTIVE mandate (checked before the dispatcher). Tests never call paid models and never run against the dev DB `cryptonite` (use a throwaway DB).
- UI state: persisted Zustand stores only; cross-workspace calls via typed `cryptonite:*` CustomEvents.
- Naming: components `PascalCase.tsx`, stores `useXStore.ts`, Python `snake_case.py`.

## Exploration Protocol
1. Read `graphify-out/GRAPH_REPORT.md` (God Nodes, Communities) first.
2. Run `graphify query "<question>"` before grep/read; run `graphify update .` if HEAD ≠ graph commit.
3. Read `HANDOFF.md` for current state and open items. Open only files the graph points to.
