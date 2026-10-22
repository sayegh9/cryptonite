# Migration 050 spec: auto-trade profiles (P0.3)

File: `infra/migrations/versions/050_auto_trade_profiles.py`; `revision = "050"`, `down_revision = "049"`. Follow
049's structure (docstring, `op.execute` SQL, named constraints `chk_ai_mandates_*`, grants, view handling). No
`cryptonite_*` imports (no migration imports app packages): the base-asset rule is implemented locally (§5) and a
test proves it equals `cryptonite_schemas.profile.base_asset` for every case in that test file.

## 1. `ai_mandates` new columns

| Column | Type | Null | Default | CHECK name and rule |
|---|---|---|---|---|
| environment | varchar(8) | NOT NULL | 'PAPER' | `chk_ai_mandates_environment`: `environment IN ('PAPER')` (052 widens to LIVE) |
| markets | text[] | NOT NULL | — (backfilled, then SET NOT NULL) | `chk_ai_mandates_markets`: `cardinality(markets) >= 1 AND markets <@ ARRAY['SPOT','SPOT_MARGIN','PERP']::text[]` |
| direction | varchar(8) | NOT NULL | 'LONG' | `chk_ai_mandates_direction`: IN ('LONG','SHORT','BOTH') |
| style | varchar(10) | NOT NULL | 'SWING' | `chk_ai_mandates_style`: IN ('SWING','INTRADAY') |
| max_open_positions | integer | NOT NULL | 3 | `chk_ai_mandates_max_open_positions`: BETWEEN 1 AND 20 |
| max_debates_per_day | integer | NOT NULL | 6 | `chk_ai_mandates_max_debates`: BETWEEN 0 AND 48 |
| exit_tp_r | numeric(5,2) | NOT NULL | 2.00 | `chk_ai_mandates_exit_tp_r`: BETWEEN 0.5 AND 20 |
| exit_tp1_scale_pct | numeric(5,2) | NOT NULL | 50 | `chk_ai_mandates_exit_tp1`: BETWEEN 0 AND 100 |
| exit_trail_activate_pct | numeric(5,2) | NULL | — | `chk_ai_mandates_exit_trail_activate`: IS NULL OR (> 0 AND <= 50) |
| exit_trail_pct | numeric(5,2) | NULL | — | `chk_ai_mandates_exit_trail`: IS NULL OR (> 0 AND <= 50 AND (exit_trail_activate_pct IS NULL OR exit_trail_pct <= exit_trail_activate_pct)) |
| exit_max_hold_hours | integer | NULL | — | `chk_ai_mandates_exit_max_hold`: IS NULL OR BETWEEN 1 AND 720 |
| dry_run | boolean | NOT NULL | false | — |
| notify_level | varchar(10) | NOT NULL | 'IMPORTANT' | `chk_ai_mandates_notify_level`: IN ('ALL','IMPORTANT','NONE') |
| breaker_max_loss_streak | integer | NOT NULL | 4 | `chk_ai_mandates_breaker_loss`: BETWEEN 1 AND 50 |
| breaker_max_reject_streak | integer | NOT NULL | 5 | `chk_ai_mandates_breaker_reject`: BETWEEN 1 AND 50 |
| paused_reason | text | NULL | — | — |

Cross-field CHECKs (added after the backfill):
- `chk_ai_mandates_short_market`: `direction = 'LONG' OR markets && ARRAY['SPOT_MARGIN','PERP']::text[]`
- `chk_ai_mandates_leverage_market`: `max_leverage = 1 OR markets && ARRAY['SPOT_MARGIN','PERP']::text[]`
- `chk_ai_mandates_perps_markets`: `allow_perps = ('PERP' = ANY(markets))` — `markets` is the truth; `allow_perps`
  stays as a derived column the existing code reads (P0.4 writes both together)
- `chk_ai_mandates_running_daily_loss`: `status <> 'ACTIVE' OR daily_loss_limit_pct IS NOT NULL` (ADR 0011 D9)

Status: replace `chk_ai_mandates_status` with IN ('ACTIVE','HALTED','PAUSED','STOPPING','STOPPED','ARCHIVED').
Drop `chk_ai_mandates_perps_level` (ADR 0011 D4).

## 2. Backfill (in this order, inside the migration transaction)

1. `markets` = the distinct markets of `instruments` (suffix `-PERP.` → PERP, `-MARGIN.` → SPOT_MARGIN, else SPOT),
   plus PERP when `allow_perps`. Then `allow_perps := 'PERP' = ANY(markets)` (equal for live data: the only perp
   mandate has allow_perps true and only perp instruments).
2. `daily_loss_limit_pct` where NULL and status <> 'ARCHIVED': `LEAST(3 * max_risk_per_trade_pct, max_drawdown_pct)`.
   Respect `chk_ai_mandates_daily_loss_ge_risk` (it holds: 3×risk ≥ risk; and drawdown ≥ risk by existing CHECKs).
3. Coin locks (§3) for status IN ('ACTIVE','PAUSED','HALTED').

## 3. `ai_profile_coin_locks`

```sql
CREATE TABLE ai_profile_coin_locks (
  base_asset varchar(20) PRIMARY KEY,
  mandate_id uuid NOT NULL REFERENCES ai_mandates(id) ON DELETE CASCADE,
  locked_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ai_profile_coin_locks_mandate ON ai_profile_coin_locks (mandate_id);
```
Backfill in Python inside `upgrade()`: read (id, name, status, instruments) of locking mandates, compute base assets
with the local rule, build {base: [mandates]}. **If any base has more than one mandate, raise
`RuntimeError` listing every conflicting coin with the mandate names and statuses, and the fix
("edit instruments or archive with scripts/profile_overlap_resolve.py (runbook O1); PAUSED and HALTED mandates still hold their coins" — corrected after Gate R H3)** — the transaction rolls
back, nothing changes. Otherwise insert one row per (base, mandate).

## 4. `ai_profile_decisions`

```sql
CREATE TABLE ai_profile_decisions (
  id bigserial PRIMARY KEY,
  mandate_id uuid NOT NULL REFERENCES ai_mandates(id) ON DELETE CASCADE,
  ts timestamptz NOT NULL DEFAULT now(),
  instrument_id varchar(64),
  stage varchar(12) NOT NULL CONSTRAINT chk_ai_profile_decisions_stage CHECK (stage IN
    ('SCAN','GATE','DEBATE','ROUTE','APPROVAL','ORDER','FILL','EXIT','LIFECYCLE','BREAKER','SESSION')),
  outcome varchar(40) NOT NULL,
  reason text,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  run_id uuid,
  command_id uuid
);
CREATE INDEX ix_ai_profile_decisions_mandate_ts ON ai_profile_decisions (mandate_id, ts DESC);
CREATE INDEX ix_ai_profile_decisions_ts ON ai_profile_decisions (ts);   -- retention job
```

## 5. Local base-asset rule (must equal the contract)

Quotes longest-first: FDUSD, USDT, USDC, BUSD, TUSD, BTC, ETH, BNB, EUR, TRY (stable order within a length as in
`QUOTE_ASSETS`). Strip venue and `-PERP`/`-MARGIN` suffix, upper-case, take the first quote the symbol ends with; an
empty base or no match raises `RuntimeError` naming the instrument.

## 6. Grants (services connect as: orchestrator/scanner/exit_monitor = `cryptonite`; api and debate_worker =
`cryptonite_api`; Brain = `cryptonite_agent_ro` via views only)

- `ai_profile_coin_locks`: `cryptonite_api` SELECT, INSERT, DELETE.
- `ai_profile_decisions`: `cryptonite_api` SELECT, INSERT; `GRANT USAGE, SELECT ON SEQUENCE ai_profile_decisions_id_seq TO cryptonite_api`.
- `ai_mandates`: unchanged (api already has SELECT, INSERT, UPDATE; notifier SELECT).
- Verify the role names exist exactly as in 049/earlier migrations; skip a grant only for a role a test DB lacks,
  following the existing `DO $$ ... IF EXISTS (SELECT 1 FROM pg_roles ...)` pattern if earlier migrations use one.

## 7. Brain view

Recreate `agent_safe_mandate_health` (as 049 does) adding: environment, markets, direction, style,
max_open_positions, dry_run, paused_reason; keep every 049 column and the `security_barrier` option; re-grant SELECT
to `cryptonite_agent_ro`.

## 8. Downgrade (to 049)

Refuse (RuntimeError with the rows) if any mandate has status STOPPING or STOPPED, `SPOT_MARGIN` in markets, or
perps away from autonomy level 1 (049's `chk_ai_mandates_perps_level` would fail). Otherwise: restore the 049 view,
drop both tables, drop the new CHECKs and columns, restore `chk_ai_mandates_status` (4 values) and
`chk_ai_mandates_perps_level` exactly as 048 defined it. Backfilled `daily_loss_limit_pct` values stay (049 allows
them).

## 9. Tests

- `tests/test_migration_050.py` (marked like the existing migration tests; uses the throwaway DB): upgrade from 049
  with fixture mandates covering: spot-only, perp-only, mixed instruments, allow_perps, NULL daily loss (ACTIVE and
  ARCHIVED), overlapping coins (expect the RuntimeError text and full rollback), no overlap (locks created per base),
  BTCUSDT and BTCUSDT-PERP in two mandates (overlap on BTC); every CHECK rejects a bad row; the view exposes the new
  columns to `cryptonite_agent_ro`; grants (`cryptonite_api` can INSERT a decision and a lock); downgrade refusals
  and a clean downgrade/upgrade round trip.
- Local base-asset rule == `cryptonite_schemas.profile.base_asset` for: BTCUSDT, ETHBTC, BNBETH, 1000PEPEUSDT,
  BTCFDUSD, USDCUSDT, WLDUSDT, XRPUSDT, BTCUSDT-PERP.BINANCE, ETHUSDT-MARGIN.BINANCE, and the no-match/empty cases.
- `uv run pytest tests -q -m destructive` (Alembic base ↔ head) passes.
