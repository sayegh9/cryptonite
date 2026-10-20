# Leverage trading: build progress (`feat/leverage-perps`)

Plan: [leverage-trading-plan.md](leverage-trading-plan.md). Decisions: [ADR 0009](adr/0009-leveraged-perpetuals.md).
Branch cut from `main` at `3bc1047`. Migration: `048`. Built in a cloud session; the operator verifies locally
before merging (CI does not run).

Legend: **done** = implemented and its tests pass in the cloud session; **partial** = see notes;
**not started**.

## Cloud verification environment

- Python 3.12 via `uv sync --frozen --extra dev --all-packages`.
- No Docker daemon. Instead: a local Postgres 16.15 with pgvector (`:15432`, DB `cryptonite_ci_local`, migrated to
  `head`) and Redis 7.0.15 (`:16379`), with the HANDOFF "Test" environment variables. Tests needing a Docker
  container by name (`CRYPTONITE_PG_CONTAINER`, backup drills) cannot run here; see "Not run" below.
- No Binance access, no paid models, no real keys: every HTTP call in the new tests is mocked.

## Items

(Filled in per item as the work lands; see the sections below.)

## Not verified in the cloud (the operator must run these locally)

(Filled in at the end.)
