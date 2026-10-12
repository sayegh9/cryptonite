# ADR 0007: Settings module: scope, change tiers, versioning

- Status: Accepted (2026-10-12, operator decisions below). Built: the read-only registry API (PR #72) and the LLM daily budget write with tier enforcement (`PUT /api/v1/settings/registry/LLM_DAILY_BUDGET_USD`, TOTP to raise). Not built: the Settings UI, per-cycle reads for exit, alert and scanner values, and writes for any other key.
- Context: [ADR 0004](0004-mandates-and-autonomy.md) (mandates, autonomy, approvals), [ADR 0006](0006-cross-mandate-exposure-and-configurable-limits.md) (tighten-only risk overrides), [ADR 0002](0002-debate-cost-controls.md) (LLM budget and exit rules), [ADR 0005](0005-local-tier-and-learning.md) (embedding model), [UI critique](../ui-ux-critique-2026-10-12.md) (Settings layout). The universe work is the onboarding plan (`symbol-onboarding-plan-v2.md`, kept outside the repo). Today Settings edits five agents and shows read-only health; the rest sits in env vars, code constants and other screens.

## Decisions

1. **Scope.** Platform-wide settings live in Settings: risk limits, LLM budget, exit rules, alert routing, and the universe as a view (decision 5). Per-mandate editing stays in Mission Control. Settings may hold defaults for new mandates; it never edits an existing mandate.
2. **Change tiers.** Each setting carries one tier, and the server enforces it, not the UI:
   - **T/L**: tightening is free; loosening needs operator TOTP (the ADR 0006 rule).
   - **TOTP**: every change needs TOTP.
   - **Session**: operator session only.
   - **G**: governance. Change by ADR, not by a toggle.
   - **R**: read-only in the module.
   - **Fixed**: not configurable.
3. **Registry.** Each setting is a registry entry: key, type, default, bounds, apply mode (`hot`, `restart`, `deploy`), tier and owning ADR. Values live in `system_settings` (JSONB key/value, [migration 010](../../infra/migrations/versions/010_system_settings.py)) and keep their current keys (`LLM_DAILY_BUDGET_USD`). Where ADR 0006 already has a store (`risk_limit_overrides`), the registry points at it rather than copying it. Every write is audited with old and new values, as the agent-config route already does. A `restart` setting says so in the UI.
4. **Paper parameters are versioned.** Paper taker fee, slippage buffer, price collar and starting capital can be changed only as a new version: each change appends a row (value, effective time, who, reason, TOTP-verified) and every paper run records the version it used. Old runs are never recomputed. Any change needs TOTP, because a lower fee makes paper results look better.
5. **The universe uses the onboarding plan's mechanism. Settings adds no second path.** Settings shows the active list read-only and links to onboarding. Add and remove go through the onboarding flow once it merges. The top-10 list (operator decision #8) is data and waits on the operator. The mechanism does not. Today ingest reads its symbols from env at start-up and loses runtime additions on restart (F1, F2 in the plan), so a durable list with reload is a precondition for any universe write.
6. **Embedding model.** The live value is the `embed` row in `ai_model_routes`, set to `ollama:embeddinggemma`. The operator checked this in the database, and every reflection since 2026-10-04 is tagged with it. The code default in [routes.py](../../packages/agents/cryptonite_agents/gateway/routes.py) applies only when no row loads. It also applies on a failed route read before the first good read, because the gateway starts with the defaults and a failed read only logs a warning. The default is therefore changed from the retired `google:text-embedding-004` to `ollama:embeddinggemma`, so those paths match the live value. The live row is not touched. Changing the embed model stays **G**: a change re-embeds every reflection, and each vector is tagged with the model that produced it ([migration 027](../../infra/migrations/versions/027_reflection_embedding_model.py)).
7. **Fixed guardrails** are shown read-only, each with its source (and never editable): the 25% per-order equity clamp, the 20% account backstop, the 1% minimum order, the mandate ceilings (25% position, 20% hard stop, 5× leverage, perps only at L3), the mandate check before the dispatcher, FAIL_CLOSED for protected trade roles, add-risk approvals timing out to no, reduce-risk approvals proceeding, operator-only kill-switch reset, and gateway-only model access.
8. **Never editable in the module:** API keys and bot tokens, `ENVIRONMENT`, `ACCOUNT_ID`, cookie flags, and the dev-TOTP bypass flags. The module shows the bypass flags read-only and warns if one is on in PROD.
9. **No global autonomy switch.** `DEBATE_EXECUTION_MODE` was deleted (ADR 0004 §10). Autonomy is per mandate.

## Phase 1 registry (proposed bounds; current values from code)

| Key | Current value | Bounds (proposed) | Apply | Tier | Stored today |
|---|---|---|---|---|---|
| `LLM_DAILY_BUDGET_USD` | 25.00 | ≥ 0 | hot (the guard re-reads every 10 s) | T/L | `system_settings` row; written by `PUT /api/v1/settings/registry/LLM_DAILY_BUDGET_USD` (TOTP to raise), no UI yet |
| `risk.max_daily_loss_pct` | 5 | 0 < x ≤ 5 (code ceiling) | hot (ops monitor, ≤ 60 s) | T/L | `risk_limit_overrides`; `RiskLimitsForm` in Operator |
| `risk.max_order_equity_pct` | 25 | 1 ≤ x ≤ 25 | hot (≤ 60 s) | T/L | same |
| `risk.cross_mandate_exposure_pct` | 25 | 1 ≤ x ≤ 25 | hot (≤ 60 s) | T/L | same |
| `exit.stop_loss_pct` | 5 | 0 < x ≤ 20 | restart (hot once the per-cycle read ships) | T/L | env `EXIT_STOP_LOSS_PCT`, read at import |
| `exit.trail_activate_pct` | 3 | > 0 | restart (hot once the per-cycle read ships) | T/L | env `EXIT_TRAIL_ACTIVATE_PCT`, read at import |
| `exit.trail_pct` | 2 | 0 < x ≤ trail_activate_pct | restart (hot once the per-cycle read ships) | T/L | env `EXIT_TRAIL_PCT`, read at import |
| `exit.debate_drawdown_pct` | 2 | 0 < x < stop_loss_pct | restart (hot once the per-cycle read ships) | T/L | env `EXIT_DEBATE_DRAWDOWN_PCT`, read at import |
| `exit.funding_spike` | 0.0005 | 0 < x ≤ 0.01 | restart (hot once the per-cycle read ships) | T/L | env `EXIT_FUNDING_SPIKE`, read at import |
| `exit.monitor_interval_s` | 30 | 5–300 | restart | Session | env `EXIT_MONITOR_INTERVAL_S` |
| `alerts.heartbeat_grace_s` | 300 | 60–3600 | restart (hot once read per pass) | T/L | env `HEARTBEAT_ALERT_GRACE_S`, read at import |
| `alerts.sentiment_stale_s` | 86400 | ≥ 3600 | restart (hot once read per pass) | Session | env `SENTIMENT_STALE_ALERT_S`, read at import |
| `alerts.routing.<severity>` | to confirm in the notifier | Critical fixed to Telegram and ntfy | hot | Session (non-critical) | code |
| `scanner.preset.<slug>.enabled` | five built-in presets | boolean | to confirm per scan cycle | Session | code, `presets.py` |
| `scanner.max_active_specs` | 10 | 1–50 | restart | Session | env `MAX_ACTIVE_SPECS`, read at import |
| `universe.*` | `INGEST_*` env | view only | n/a | R | env (see decision 5) |

Notes:
- Exit and alert values are read at import today. Making them hot means reading them per cycle, which is a small change per service.
- Where a value is marked "to confirm", the build needs to check current behaviour first.
- The apply modes above match the registry (`settings_registry.py`). The alert-routing and scanner-preset rows are not in the registry yet.

## Phase 2 (after Phase 1 lands)

- Paper parameters, versioned (decision 4).
- Per-role model routing editor for `ai_model_routes`. Protected roles stay locked, as the gateway already enforces for them.
- Session and lockout bounds for `auth.py` (12 h absolute, 30 min idle, 5 min lockout), with floors and ceilings.
- Per-symbol price and size precision from instrument metadata (F3, F20; D6).
- Data retention and free-disk threshold (open in the onboarding plan).
- Universe write path, only through onboarding (decision 5).

## Phase 3 (ADR-gated, not toggles)

- Promotion gates and the calibration minimum (20 decisions now; 100 proposed in ADR 0004).
- Informational → required gate checks (ADR 0004 D6).
- Per-symbol perp leverage and margin mode. The `paper_perp_settings` table is in the perps blueprint and not yet in migrations.
- Embedding model changes (decision 6).

## Consequences

- Phase 1 adds registry endpoints, UI in Settings, and per-cycle reads for the exit and alert values. It adds no migration unless the registry needs one.
- Phase 2's paper-parameter versions need a migration. The next free number is after the migrations in open PRs (the K1 PR takes 043).
- Every `hot` setting needs a reader that re-reads its value. Every `restart` setting says so in the UI.
- Tests must cover each tier: the server refuses a loosening without TOTP, and an unversioned paper change.

## Verification notes

- Embedding default: `test_embed_default_is_the_local_model_not_the_retired_one` is new in `tests/test_ai_embeddings.py`. `test_ai_embeddings.py`, `test_ai_gateway.py` and `test_debate_worker_gateway.py` pass (44 tests).
- The live `embed` row and the reflection tags come from the operator's database check. This change did not query the database.
- Not checked yet: current notifier routing for warning and info alerts, and the default enabled state of each scanner preset. Both are marked "to confirm" above.
