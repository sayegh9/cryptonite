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

## Baseline (main `3bc1047`, before any change)

`uv run pytest tests -q` in this environment: **2,933 passed, 19 errors, 14 deselected** (destructive). The 19
errors are all `tests/test_backup_drill.py`: it needs a Docker Postgres container by name
(`CRYPTONITE_PG_CONTAINER`), which this environment does not have. Nothing else failed.

## Items

### Stage 1: safety plumbing

| # | Item | Status | Commit | Tests |
|---|---|---|---|---|
| 1.1 | Futures adapter: `reduceOnly`, `STOP_MARKET`/`TAKE_PROFIT_MARKET` with `stopPrice` + `workingType=MARK_PRICE`, `closePosition`; `set_leverage` (refuses > 10x), `set_margin_type`; hedge mode refused; `positionRisk`; routing by `-PERP` | done | `3aba50e` | `tests/test_binance_futures_adapter.py` (16, all HTTP mocked) |
| 1.2 | Live brackets from signed `/fapi/v1/leverageBracket`, Redis cache with fetch time, daily refresh, `REFERENCE_BRACKETS` fallback for BTC/ETH only, `NO_LEVERAGE_BRACKETS` / `STALE_LEVERAGE_BRACKETS` | done | `72d03a5` | `tests/test_leverage_brackets.py` (12) |
| 1.3 | Perp quotes and marks for every fed symbol with a USD-M perpetual (same list and cap as spot ingest); futures bookTicker reconnect recovery hook | done | `639f4c9` | `tests/test_perp_feed_universe.py` (10) |
| 1.4 | Paper resting stops: rest in the dispatcher, trigger on mark, fill at the worse of stop and mark, before the liquidation check; entry `stop_loss` attaches one | done | `31ffff6` | `tests/test_paper_resting_stops.py` (18: incl. the flash-crash test and the round trip with no exit monitor) |
| 1.5 | Partial perp close: `CLOSE_POSITION` with a quantity; TP1 50% scale-out on perps | done | `c0e688d` | `tests/test_partial_perp_close.py` (7) |
| 1.6 | Mandate perp orders ISOLATED (manual default ISOLATED too); paper perp taker fee 0.05% | done | `28ac963` | `tests/test_perp_isolated_default_and_fee.py` (6); Phase 1 golden numbers updated in 5 existing files |
| 1.7 | Exit monitor: perp `Position` with leverage and liquidation price, priced at the mark; `LIQUIDATION_PROXIMITY` CRITICAL alert at 50% of the stop-to-liquidation buffer, reduce-only close at 75% | done | `3e4e52c` | `tests/test_exit_monitor_liquidation.py` (8) |

Notes and choices made (safer option where the plan was open):

- **1.1** The adapter's `_request` swallowed Binance's error code (its own `RuntimeError` was caught by the
  `except` around it), so `-2013` ("no such order") matching never worked. Fixed, because `set_margin_type` needs
  `-4046`. The DEMO pipeline sends bare `BTCUSDT`, which still routes to spot.
- **1.2** Brackets older than **36 h** are stale (daily refresh plus 12 h). A stale or missing table refuses new
  entries only; reduce-only exits, stops and liquidations keep working on the newest table. The live table wins
  over the reference snapshot for BTC/ETH whatever its age (open positions are always valued with the newest
  table). New env vars for the derivatives feed: `BINANCE_FUTURES_READONLY_API_KEY` and
  `BINANCE_FUTURES_READONLY_PRIVATE_KEY_PATH` (Ed25519 PEM path **inside the container**; the compose file passes
  both through, empty by default; the key file must be mounted read-only by the operator). There is no
  `.env.example` in the repo, so they are documented here, in `docker-compose.yml` and in HANDOFF. A missing or
  unreadable key file is logged and the feed runs without brackets (it does not crash). The scanner service does
  not declare `cryptonite-execution` in its `pyproject.toml` (the image installs every workspace package);
  declaring it needs a `uv lock` and is left as a follow-up, like the API → orchestrator one.
- **1.3** Default on (`INGEST_PERP_FROM_ONBOARDED=1`; `PERP_SYMBOLS_FROM_UNIVERSE=1` for the mark feed). The
  USD-M list comes from the public `/fapi/v1/exchangeInfo` at ingest start; if that fails, only the env perps are
  fed. "Recovery" for a quote socket means a fresh REST book ticker for every fed perp at once (there is no gap to
  backfill for quotes).
- **1.4** A resting stop is **reduce-only only** (a stop entry is refused). When the mark has already crossed the
  liquidation price, the stop is left to the liquidation engine (which caps an ISOLATED loss at the margin) rather
  than filling beyond the bankruptcy price. The paper stop uses the full position size and is capped when it
  fires; it does not use up the closable quantity of other reduce-only orders (TP1). If the dispatcher refuses an
  attached stop (the mark went through it before the fill was booked), it is logged CRITICAL and the exit
  monitor's structural stop and the 75% proximity close remain the backup.
- **1.5** A perp TP1 is now a `CLOSE_POSITION` with half the quantity (the old `SUBMIT_ORDER` on the opposite side
  would have been a new, non-reduce-only entry on a perp). Resting stops shrink with the position and are dropped
  when it closes or flips.
- **1.6** Beyond the plan: the margin mode is locked while a position is open (`MARGIN_MODE_LOCKED`, as on Binance),
  and a mandate's CROSS request is refused (`MANDATE_MARGIN_MODE_NOT_ALLOWED`, a refusal, not a breach). The
  manual ticket's stored preference stays whatever the operator last chose; only the default changed.
- **1.7** The proximity buffer is measured from the stop when there is one on the safe side of the liquidation
  price, else from the entry. For a manual (non-AI) perp the monitor only alerts, as for its other stops.

## Not verified in the cloud (the operator must run these locally)

(Filled in at the end.)
