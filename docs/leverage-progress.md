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

### Stage 2: mandate and sizing rules

| # | Item | Status | Commit | Tests |
|---|---|---|---|---|
| 2.1 | Migration 048 (10x CHECK, `max_risk_per_trade_pct` 1/2, perps at L1); schema cap 10, D3 validator, 3x default | done | `4a2b8d4` | `tests/test_migration_048_leverage.py` (upgrade, CHECKs, refused downgrade, refused upgrade); `test_ai_mandates.py`, `test_autonomy.py` updated |
| 2.2 | `leverage.py` `plan_leveraged_order` + `check_leveraged_order` | done | `0b37fa3` | `tests/test_leverage_plan.py` (worked example 10x/7x/4x; hypothesis 2,000 examples: stop within 50% of the liquidation distance, loss cap, room, maximum, plan passes the check) |
| 2.3 | Dispatcher: `STOP_REQUIRED`, `RISK_AT_STOP_EXCEEDED`, `LIQUIDATION_BUFFER_TOO_THIN`, `LEVERAGE_ABOVE_BRACKET`; 25% clamp kept; account max 10x (dispatcher and API) | done | `58bcfaa` | `tests/test_leverage_orchestrator_sizing.py` (dispatcher part) |
| 2.4 | Orchestrator sizes perp entries both ways with the plan; the mandate gate checks with the same valuation | done | `58bcfaa` | `tests/test_leverage_orchestrator_sizing.py` (worked example end to end, short, grade/maximum, no stop, 160-case grid: gate and dispatcher never refuse what the plan sized) |
| 2.5 | Cross-mandate cap counts perp notional (short + spot long, 10x BTC + ETH) | done (tests only; no code change needed) | `7f82b8c` | `tests/test_leverage_cross_mandate.py` |
| 2.6 | Mandate equity includes perp funding and fees | done | `68d2099` | `tests/test_bridge_perpetuals_persistence.py::test_mandate_equity_counts_perp_fees_and_funding_paid` |
| 2.7 | Settings: grade ceilings and slippage allowance editable (T/L), leverage guardrails listed with their source | done | `24165d9` | `tests/test_leverage_settings.py`; `test_settings_registry.py` updated |

Notes and choices (Stage 2):

- **2.1 / D3** "Leaving the allowed level withdraws the opt-in": with L2/L3 autonomous perps out of scope, the allowed
  level is **L1 only**. Any level change away from L1 (promotion or demotion) clears `allow_perps`; the DB CHECK allows
  perps only at level 1. The upgrade **refuses** (rather than silently disables) a mandate with perps away from L1;
  there are none in the live DB per the plan's §1. A HALT or BACKSTOP still clears `allow_perps` (as before): the
  operator re-enables it with TOTP after a resume.
- **2.2** Quantities are rounded down to 1e-8, so plan dollars equal the worked example to the cent, not exactly.
  The liquidation ceiling uses the exact ISOLATED liquidation price with isolated margin = the margin.
- **2.3** The manual ticket's risk budget, when the operator attaches a stop, is **2% of account equity** (the D6
  cap); without a stop a manual order gets no stop checks (unchanged). AI entries always need a stop.
- **2.4** Sizing keeps a margin to the check so a mark tick cannot flip a boundary: 99% of the risk budget and of the
  room, and 0.1% extra slippage. An **ungraded** AI perp entry is held to **grade C's ceiling** (the safer reading of
  §2.3). The plan also refuses below the dispatcher's 1% minimum order, so nothing is placed only to be refused.
  Plan refusals read `CODE: perp sizing (<ceiling>)`; they are refusals, never breaches.
- **2.6** Funding was already in mandate equity (booked on the position row). Found and fixed: a perp **opening fee**
  left only the account's cash, so mandate equity ran high by entry fees. The row's realized PnL now equals the
  account's cash change (one Phase 1 test number updated: 38.04 → 36.04).
- **2.7** The Settings page had a card saying "L1 2x / L2 3x / L3 5x" that never matched the code; it now states
  ADR 0009's rules. The §2.3 automatic check after 30 closed graded cards (lower every ceiling to B if A does not beat
  B) is **not built**: it needs closed graded cards (Stage 5 territory). Lowering a ceiling by hand is possible now.

### Stage 3: setup grade

| # | Item | Status | Commit | Tests |
|---|---|---|---|---|
| 3.1 | `setup_grade.py`, deterministic, inputs kept with the grade | done | `4d8d35c` | `tests/test_setup_grade.py` (A/B/C/NO_TRADE rules, steps, cap, shorts, recompute, hypothesis determinism) |
| 3.2 | Brain tool `grade_setup`; `scan_swing_setups` grades every setup | done | `702b7fc` | `tests/test_grade_setup_tool.py` (all Binance calls stubbed) |
| 3.3 | Grade and confidence stored on each card and order; `agent_safe_trade_timeline` extended in 048 | done | `878fe81` | `tests/test_brain_card_grade.py`, `tests/test_agent_safe_trade_timeline_perps.py`, `test_agent_safe_trade_timeline.py` column list |

Notes and choices (Stage 3):

- A **missing input counts as a weak input**, so A needs all six inputs known. Volatility is judged on its own: the
  regime input is the ADX trend without `regime.py`'s HIGH_VOL precedence (so a volatile trend is not counted twice).
  Thresholds: thin book = spread > 0.10% or 24 h futures volume < $50m; OI falling = 10% down over 24 h; crowded
  funding = ±0.03% per 8 h (scaled from 4 h / 1 h intervals). A volatility or crowding step can take C to NO_TRADE.
- A **scan** grade reads no funding or liquidity (no extra Binance calls per candidate), so it is at most B; a card
  is graded with every input when the Brain stages it.
- The view `agent_safe_trade_timeline` read spot only (entry = BUY, exit = SELL); 048 makes it read perp shorts too.

### Stage 4: Brain and UI

| # | Item | Status | Commit | Tests |
|---|---|---|---|---|
| 4.1 | `OrderProposal`: perp instruments, BUY/SELL = long/short, optional `leverage` (1–10, perps only), a perp entry needs `stop_loss` | done | `80da5c7` | `tests/test_brain.py` (normalisation), `tests/test_brain_perp_cards.py` |
| 4.2 | Brain prompt: perps only with `allow_perps`, a structural stop, state the grade and the binding ceiling, shorts on perps only, never claim a leverage the dry run did not allow | done | `80da5c7` | prompt text only (checked by the paid-model run below, not in the cloud) |
| 4.3 | Order card: `7x ISOLATED` pill, margin, notional, liquidation price (also on a price ladder with stop, entry, target), buffer bar, loss at stop ($ and % of budget), funding per 8 h, grade and reasons, "7x (risk at stop)", pills `1x 2x 3x 5x 7x 10x` + recommended ★, refused pills disabled with the ceiling, re-planned by the dry run (`?leverage=`), locked once decided | done | `80da5c7` (API), `8975e37` (web) | `tests/test_brain_perp_cards.py`; Playwright `e2e/brain-perp-card.spec.ts` |
| 4.4 | Card remedies: "Allow perps" (L1 only, 3x), "Raise max leverage", new mandate with perps; each with TOTP | done | `80da5c7`, `8975e37` | `tests/test_brain_perp_cards.py`, `tests/test_brain_card_preflight.py`; Playwright `brain-perp-card.spec.ts` |
| 4.5 | Manual ticket: dispatcher preview (`GET /api/v1/paper/perp/plan`), 10x pills (20x chip gone), "Attach stop" | done | `8545c62` | `tests/test_manual_perp_ticket.py`, `tests/test_api_manual_order_perp_fields.py`; Playwright `e2e/perpetuals-cockpit.spec.ts` |
| 4.6 | Mission Control: perp exposure and liquidation distance per position; `LIQUIDATION_PROXIMITY` to Telegram and ntfy | done | `6f6a9ef` | `tests/test_mission_perp_positions.py`, `tests/test_exit_monitor_liquidation.py`, `tests/test_notifier.py`; Playwright `e2e/mission-control.spec.ts` |
| 4.7 | "Flatten perps" in the header (TOTP), spot left alone | done | `6345e82` | `tests/test_mission_perp_positions.py`; Playwright `e2e/perpetuals-cockpit.spec.ts` |
| 4.8 | Approval: TOTP on every perp entry, the dialog states loss at stop and loss if liquidated, second confirmation at 8x+ | done | `80da5c7`, `8975e37` | `tests/test_brain_perp_cards.py`; Playwright `brain-perp-card.spec.ts` |
| 4.9 | Leverage colour tokens (low/mid/high/liquidation) for light and dark in `globals.css` | done | `8975e37` | used by the card, ticket, Mission Control and header; checked in Playwright (class names) |

Notes and choices (Stage 4):

- **A perp fix spends its own TOTP code.** A TOTP code works once (`TOTP_REUSED`), and approving a perp entry needs a
  code too (ADR 0009 D3). So "Allow perps", "Raise max leverage", or creating/editing a mandate for a perp card is a
  step of its own: the mandate change takes this code, the card re-reads its dry run, and approve takes the next code.
  Spot fixes keep the old one-step flow (spot approve needs no code).
- **The manual ticket fixes the quantity, not the margin.** Each pill is judged by the dispatcher's own
  `check_leveraged_order` for the typed quantity (bracket; with a stop, the liquidation buffer and a loss within 2%
  of equity), plus the 1% minimum order and the 25% concentration limit. Submit is held while the preview refuses.
  Without a stop, only the bracket judges the leverage (manual orders are not required to carry a stop; AI ones are).
  A CROSS ticket is still checked the same way; the preview shows the ISOLATED liquidation price and says so.
- **The pills lock once the card is decided** (approved, rejected), not only once filled: after approval the order is
  queued at that leverage, so changing it would not change the order. The approved leverage is returned by approve.
- **"Chart overlay" is a price ladder on the card** (liquidation, stop, entry, target on one axis). The separate price
  chart widget (`show_price_chart`) is drawn from the model's arguments and has no liquidation price; adding one there
  would need the model to supply it, which it must not.
- **Flatten perps needs TOTP**, as the plan says; the kill switch trip itself does not (unchanged). It closes through
  the existing `CLOSE_POSITION` path (one per perp, to the instance that holds it), so a mandate's close checks stay.
- **Mission Control colours** for the liquidation distance (<5% liquidation colour, <10% high, else low) are display
  tiers only; the exit monitor's thresholds, which act, are relative to the stop (50% alert, 75% close for AI).
- The Mandate editor now allows perps at L1 (it allowed them only at L3), up to 10x, with a 3x default when turned on,
  and shows the risk-per-trade field (≤ 2%).
- A leverage remembered by the ticket from before the 10x cap is clamped on load.

## Not verified in the cloud (the operator must run these locally)

(Filled in at the end.)
