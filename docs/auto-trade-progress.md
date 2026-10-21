# Auto-trade Phase A: build progress (`feat/auto-trade-phase-a`)

Decisions: [ADR 0010](adr/0010-mandate-sessions-and-entry-filters.md). Branch cut from `main` at `1d68466`.
Migration: `049`. Built in a cloud session; the operator verifies locally before merging (CI does not run).
Phase A adds per-mandate daily session guardrails and refuse-only entry filters, all at the mandate's current level:
it changes no autonomy rule and adds no approval bypass.

Legend: **done** = implemented and its tests pass in the cloud session; **partial** = see notes; **not started**.

## Cloud verification environment

- Python 3.12 via `uv sync --frozen --extra dev --all-packages`; Node 22, `pnpm install --frozen-lockfile`.
- No Docker daemon. Instead, as in [leverage-progress.md](leverage-progress.md): a local Postgres 16 with pgvector
  (`apt-get install postgresql-16-pgvector`, `initdb`, `:15432`, DB `cryptonite_ci_local`, the app roles created by
  hand, migrated to `head`) and Redis 7 (`:16379`), with the HANDOFF "Test" environment variables. `zstd` installed for
  `test_backup_data_dir.py`.
- No Binance access, no paid models, no real keys.

## Baseline (main `1d68466`, before any change)

`uv run pytest tests -q --ignore=tests/test_backup_drill.py`: **3,325 passed, 0 failed, 14 deselected**
(destructive). `tests/test_backup_drill.py` (19 tests) needs a Docker Postgres container by name and errors here.

## Items

| # | Item | Status | Commit | Tests |
|---|---|---|---|---|
| A1 | Contract: five `Mandate` fields and validators, perps default daily loss 3 × risk, `risk_adding_change` for each field, `MandateReject.DAILY_LOSS_LATCHED` / `DAILY_TARGET_MET` (not breaches), pure `cryptonite_schemas.session` (`session_verdict`, `utc_session_date`, `entry_filter_refusal`). Migration 049 (named CHECKs, `ai_mandate_daily_sessions`, grants, Brain view). `MANDATE_COLUMNS` (both copies) and the mandate API (create, edit with `clear_limits`, views with today's `session`) | done | `83cb47c` | `tests/test_mandate_session_contract.py` (52, incl. a 2,000-example hypothesis property test); view contract updated in `test_agent_safe_ai_state_views.py` |
| A2 | Session engine in the mandate monitor (`track_session`): start equity, P&L, peak, idempotent latches; loss → cancel ADD_RISK + flatten + Critical; target → cancel ADD_RISK + Info + `mandate:session:target_met` (EXPIREAT next 00:00 UTC); lock → flatten + Critical. Gate: `check_mandate(..., session=)` refuses ADD_RISK, passes REDUCE_RISK | done | `62ba6ec` | `tests/test_mandate_sessions_replay.py` (16: both replays with the real poller, budget edit, DB CHECKs, cascade, notifier kinds) |
| A3 | Entry filters: `route_entry` (conviction, R:R from the decision price, session) → `NO_TRADE`, logged; the Brain card dry run and approve, the Inbox debate approval (`mandate_choice.entry_filter_detail`); the Brain's System state shows each mandate's session verdict | done | `8e19b5a` | `tests/test_mandate_entry_filters.py` (16); `test_brain_ai_state.py` (+1); Brain prompt test updated in `test_brain.py` |
| A4 | Perp resting-stop ratchet | not in this branch | – | Built by the parallel session (`feat/perp-stop-ratchet`); it reads the Redis set above |
| A5 | API + web: mandate views carry `session`; Mandate editor fields with helper text and the TOTP mirror; mandate card session meter (P&L vs loss limit and target, verdict chip, "resets 00:00 UTC"); one Mission banner per mandate whose session is not ACTIVE | done | `ba62bb3` | `apps/web/e2e/mission-control.spec.ts` (+3) |
| A6 | Tests | done | with each item | see above and the final run below |
| A7 | Docs: ADR 0010, this file, HANDOFF row, Stage 5 runbook | done | docs commit on the branch | – |

Notes and choices made (the safer or simpler option where the plan was open):

- **`start_budget` column** (not in the plan's table list): the row keeps the budget it opened with and P&L is
  `(equity − budget) − (start_equity − start_budget)`. Without it, lowering a mandate's budget during the day reads as
  a loss and could latch and flatten the mandate; raising it would read as profit and could meet the target.
- **Latches are sticky whatever the limits**: removing a limit (TOTP) after it latched does not clear today's latch;
  the next UTC day does. A mandate with no limit gets no session row and is always ACTIVE.
- **Migration 049 backfills** the perps default (3 × risk, capped at the hard stop) on mandates that already have perps
  on, as S-2 applies to new ones. Spot mandates stay off. A perps mandate whose hard stop is below one trade's risk
  gets no default (its hard stop is tighter than any daily limit).
- **`daily_loss_limit_pct ≥ max_risk_per_trade_pct`** is a model validator and, after review, also the DB CHECK
  `chk_ai_mandates_daily_loss_ge_risk` (049), so a row that breaks it cannot reach `load_mandate`; the monitor and the
  account backstop also log (ERROR) and skip a mandate that fails to load instead of aborting the pass.
- **Gate order**: the session check runs right after the status check, before the instrument, perps, budget and
  approval checks, so a latched day refuses with the session code and never records a breach.
- **R:R reference price**: the debate worker passes the decision price (`reference_price`) to `route_entry`; the card
  uses its limit price, else the perp mark, else the spot `market:24h` last price. With none, the filter is skipped
  and logged. Brain cards must carry a stop and a target (and a confidence) when the mandate sets the filter, else
  they are refused; the worker's debates carry no stop or target today, so there `min_rr` is skipped, logged and
  recorded as "min_rr skipped: no stop/target" (S-9).
- **Refusal codes outside the gate**: `MANDATE_MIN_CONVICTION` and `MANDATE_MIN_RR` (API details and logs only; the
  gate does not check them, because it has no conviction or target for an order).
- **Notifier**: `MANDATE_DAILY_LOSS`, `MANDATE_PROFIT_LOCK` and `MANDATE_DAILY_TARGET` added to `OUTBOX_KINDS`, so a
  Critical session alert is not sent twice (once from the channel, once from the outbox).
- **Brain view**: `agent_safe_mandate_health` was trivially extendable (CREATE OR REPLACE appends the limits and
  today's session row); the downgrade restores 046's view.
- **Editor**: an emptied field that was set is sent as `clear_limits` (needs TOTP); an emptied profit lock is not sent (the server keeps the value or applies the default) and is not a loosening. The
  stale-build guard from #111 is kept.

## Final cloud run

Branch head, test DB `cryptonite_ci_local` at 049, Redis flushed at start.

- `uv run pytest tests -q`: **3,410 passed, 0 failed, 19 errors, 14 deselected** (baseline 3,325 passed; +85 = the
  new tests). The 19 errors are all `tests/test_backup_drill.py` (needs a Docker Postgres container by name).
- `uv run pytest tests -m destructive -q`: **2 passed** (Alembic roundtrip base ↔ head, including 049). Also by hand on
  a throwaway DB: 048 with a perps and a spot mandate → 049 (perps backfilled to 3.00, spot left NULL, lock 50) → 048
  (columns and table gone, 046's 15-column view back) → 049 again.
- `uv run ruff check` on every Python file changed on the branch: clean.
- `pnpm typecheck`: clean. `pnpm lint`: 0 errors, 2 warnings, both already on `main` (unused `PortfolioConsole` in
  `page.tsx`, unused `SwingCandidateItem` in `BrainWidgets.tsx`).
- Playwright (production build with the API at a closed port, served on `:3100`, the preinstalled Chromium through a
  local config that only sets `baseURL`, `webServer` and `executablePath`): **237 passed, 9 failed**.
  `mission-control.spec.ts` passes in full (14, the 3 new specs included). The 9 failures do not touch the files this
  branch changed:
  - 8 are the ones [leverage-progress.md](leverage-progress.md) recorded as failing on a build of `main`:
    `mandate-instance-controls:107`, `mode-banner:216`, `portfolio_console` (5), `units-formatting:77`. Not
    re-checked against a `main` build in this session.
  - `alpha_lifecycle.spec.ts:22` opens `http://localhost:3000/` directly (a hard-coded URL), and nothing listens there
    with the `:3100` local config: an environment artifact. It should pass with the default config on `:3000`.

## Not verified in the cloud (the operator must run these locally)

1. `tests/test_backup_drill.py` (19 tests): needs the Docker Postgres container (`CRYPTONITE_PG_CONTAINER`).
2. The whole suite in the project's Docker test environment (HANDOFF "Test"), on arm64, and Playwright with the
   Playwright-pinned browser (`npx playwright test` with the default config).
3. Migration 049 on a **copy of the live DB**: upgrade (the perps backfill on `Brain BTCUSDT-PERP.BINANCE`, the CHECKs
   against existing rows), `agent_safe_mandate_health` read by `cryptonite_agent_ro`, `ai_mandate_daily_sessions` read
   by `cryptonite_api`, downgrade to 048 and upgrade again. Checked here only on a throwaway DB.
4. The session engine on the running orchestrator: the first tick after midnight opens rows from the last snapshot
   before 00:00 UTC; Mission Control shows the meter; a latch flattens a real paper position.
5. Live Brain checks (paid model, never run in tests): the System state line shows the session verdict, and the Brain
   declines to propose an entry for a mandate whose session is not ACTIVE.
6. Telegram and ntfy delivery of `MANDATE_DAILY_LOSS`, `MANDATE_PROFIT_LOCK` (Critical) and `MANDATE_DAILY_TARGET`
   (Info) through the outbox; only the `OUTBOX_KINDS` membership is unit-tested.
7. The A4 ratchet reading `mandate:session:target_met` (built in the other session).

## Local verification (2026-10-21, operator's machine, #114 at `ca6a44f` merged with #113 at `bd7d5d5`)

Both PRs were reviewed adversarially (Opus) and every HIGH/MEDIUM finding was fixed (#114: `ca6a44f`; #113:
`d545890`, `bd7d5d5`). Then, on a local merge of both branches over `main` `1d68466`:

- `uv run pytest tests -q` (Docker test containers, so `test_backup_drill.py` runs): **3,473 passed, 1 failed**. The
  failure, `test_debate_decision_price.py::test_boundary_age_is_accepted`, is a timing flake (a quote 1 s inside the
  age bound); the file passes 19/19 three times in isolation, and neither PR touches that logic.
- `uv run pytest tests -q -m destructive`: **2 passed**. Ruff on every changed Python file: clean.
- `pnpm typecheck`: clean. `pnpm lint`: 0 errors, the 2 warnings already on `main`.
- Playwright (production build on :3100, API proxy at a closed port): **238 passed, 8 failed**: the same 8 that fail
  on `main` (`mandate-instance-controls`, `mode-banner` 1024px, `portfolio_console` ×5, `units-formatting`).
- Migration 049 on a **fresh copy of the live DB** (streamed `pg_dump` into a throwaway Postgres): upgrade, downgrade
  to 048, upgrade again, all clean in ~2 s. Only `Brain BTCUSDT-PERP.BINANCE` gets `daily_loss_limit_pct` 3.00 (the
  other six mandates keep every filter NULL, profit lock 50). All six new CHECKs present, including
  `chk_ai_mandates_daily_loss_ge_risk`. `cryptonite_agent_ro` reads `agent_safe_mandate_health` and is refused on
  `ai_mandate_daily_sessions`.

Items 4–7 above (the running stack, live Brain, Telegram/ntfy, the ratchet reading the target set) are post-deploy
checks.
