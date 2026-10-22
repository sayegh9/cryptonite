"""ai_mandates become auto-trade profiles (ADR 0011, auto-trade profiles P0.3)

Revision ID: 050
Revises: 049
Create Date: 2026-10-22 00:00:00.000000

- ``ai_mandates`` gains the profile columns (environment, markets, direction, style, position and debate caps, exits,
  dry-run, notification level, breaker streaks, pause reason) with named ``chk_ai_mandates_*`` CHECKs. ``markets`` is
  the truth for which markets a profile trades; ``allow_perps`` stays as a derived column (``chk_ai_mandates_perps_markets``).
- ``chk_ai_mandates_perps_level`` is dropped (ADR 0011 D4: perps at any mode); ``chk_ai_mandates_status`` gains
  STOPPING and STOPPED. A running (ACTIVE) profile needs a daily loss limit (D9): every non-archived mandate without
  one gets 3 x its risk per trade, at most its hard stop.
- ``ai_profile_coin_locks`` (D6): one row per base asset, primary key = exclusivity. Backfilled from the mandates in
  ACTIVE, PAUSED or HALTED; two such mandates on one coin abort the migration (RuntimeError, full rollback).
- ``ai_profile_decisions``: the append-only decision log of the profile pipeline.
- ``agent_safe_mandate_health`` (the Brain's view, 049) gains the profile columns.

The base-asset rule is local (no app imports in a migration); a test proves it equals
``cryptonite_schemas.profile.base_asset``.

``ai_mandates_profile_defaults()`` (BEFORE INSERT OR UPDATE trigger) keeps writers that predate profiles working: a row
without ``markets`` gets SPOT derived from its non-perp ``instruments`` plus PERP only when ``allow_perps`` (a listed perp
instrument never grants perps; SPOT_MARGIN is never derived; empty becomes SPOT), and an ACTIVE row without
a daily loss limit gets 3 x its risk per trade capped at its hard stop (ADR 0011 D9). A supplied ``markets`` is never
touched; ``chk_ai_mandates_perps_markets`` still enforces that ``allow_perps`` agrees with it.

The downgrade refuses while a mandate is STOPPING or STOPPED, trades SPOT_MARGIN, or has perps away from level 1; it
restores 049's view, the 4-value status CHECK and 048's ``chk_ai_mandates_perps_level``. Backfilled daily loss limits stay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "050"
down_revision: str | None = "049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HEALTH_VIEW = "agent_safe_mandate_health"

# Same order as cryptonite_schemas.profile.QUOTE_ASSETS, sorted longest first (stable)
_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "BTC", "ETH", "BNB", "EUR", "TRY")
_QUOTES_LONGEST_FIRST = tuple(sorted(_QUOTES, key=len, reverse=True))
LOCKING_STATUSES = ("ACTIVE", "PAUSED", "HALTED")

NEW_COLUMNS = (
    "environment", "markets", "direction", "style", "max_open_positions", "max_debates_per_day", "exit_tp_r",
    "exit_tp1_scale_pct", "exit_trail_activate_pct", "exit_trail_pct", "exit_max_hold_hours", "dry_run",
    "notify_level", "breaker_max_loss_streak", "breaker_max_reject_streak", "paused_reason",
)
# Named CHECKs created in the column phase and in the cross-field phase (the downgrade drops both lists)
COLUMN_CHECKS = (
    "environment", "markets", "direction", "style", "max_open_positions", "max_debates", "exit_tp_r", "exit_tp1",
    "exit_trail_activate", "exit_trail", "exit_max_hold", "notify_level", "breaker_loss", "breaker_reject",
)
CROSS_CHECKS = ("short_market", "leverage_market", "perps_markets", "running_daily_loss")

# 049's view, as it was (the downgrade restores it)
HEALTH_049 = """
    SELECT m.id, m.name, m.status, m.autonomy_level, m.level_since, m.instruments, m.capital_budget_usd,
           m.max_position_pct, m.max_drawdown_pct, m.halted_reason, m.halted_at,
           e.equity, e.hwm, e.drawdown_pct, e.ts AS equity_ts,
           m.min_conviction, m.min_rr, m.daily_loss_limit_pct, m.daily_profit_target_pct, m.profit_lock_pct,
           s.session_date, s.start_equity AS session_start_equity, s.last_pnl AS session_pnl,
           s.loss_latched_at AS session_loss_latched_at, s.target_hit_at AS session_target_hit_at,
           s.lock_fired_at AS session_lock_fired_at
    FROM ai_mandates m
    LEFT JOIN LATERAL (
        SELECT equity, hwm, drawdown_pct, ts
        FROM ai_mandate_equity
        WHERE mandate_id = m.id
        ORDER BY ts DESC, id DESC
        LIMIT 1
    ) e ON TRUE
    LEFT JOIN ai_mandate_daily_sessions s
           ON s.mandate_id = m.id AND s.session_date = (now() AT TIME ZONE 'UTC')::date
    WHERE m.status <> 'ARCHIVED'
"""

# 049's columns, then the profile columns (CREATE OR REPLACE VIEW may only append)
HEALTH_050 = HEALTH_049.replace(
    "s.lock_fired_at AS session_lock_fired_at\n",
    "s.lock_fired_at AS session_lock_fired_at,\n"
    "           m.environment, m.markets, m.direction, m.style, m.max_open_positions, m.dry_run, m.paused_reason\n",
)

TRIGGER = "trg_ai_mandates_profile_defaults"
TRIGGER_FN = """
CREATE OR REPLACE FUNCTION ai_mandates_profile_defaults() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.markets IS NULL THEN
        NEW.markets := COALESCE((
                SELECT array_agg(DISTINCT x ORDER BY x) FROM (
                    SELECT 'SPOT' AS x FROM unnest(NEW.instruments) AS i WHERE split_part(upper(i), '.', 1) NOT LIKE '%-PERP'
                    UNION
                    SELECT 'PERP' WHERE NEW.allow_perps
                ) t
            ), ARRAY['SPOT']::text[]);
        NEW.allow_perps := 'PERP' = ANY(NEW.markets);
    END IF;
    IF NEW.status = 'ACTIVE' AND NEW.daily_loss_limit_pct IS NULL THEN
        NEW.daily_loss_limit_pct := LEAST(3 * NEW.max_risk_per_trade_pct, NEW.max_drawdown_pct);
    END IF;
    RETURN NEW;
END
$fn$
"""


# ai_autonomy_events.kind as 045 left it, and the lifecycle kinds 050 adds (start, stop, finish stop, operator mode change)
EVENT_KINDS_045 = ("'CREATED', 'PROPOSED', 'PROMOTED', 'DEMOTED', 'HALTED', 'RESUMED', 'PAUSED', 'BREACH', 'EDITED', "
                   "'BACKSTOP', 'BREACH_VOIDED'")
EVENT_KINDS_NEW = ("'STARTED', 'STOPPING', 'STOPPED', 'MODE_SET'",)


def base_asset(instrument_id: str) -> str:
    """The base asset of an instrument id (BTCUSDT-PERP.BINANCE -> BTC); equals profile.base_asset."""
    s = instrument_id.strip().upper()
    local = s.split(".", 1)[0] if "." in s else s
    for suffix in ("-PERP", "-MARGIN"):
        if local.endswith(suffix):
            local = local[: -len(suffix)]
            break
    for quote in _QUOTES_LONGEST_FIRST:
        if local.endswith(quote):
            base = local[: -len(quote)]
            if base:
                return base
            break
    raise RuntimeError(f"cannot find the base asset of {instrument_id!r}")


def _coin_owners(rows) -> dict[str, list]:
    owners: dict[str, list] = {}
    for row in rows:
        for base in sorted({base_asset(i) for i in row.instruments}):
            owners.setdefault(base, []).append(row)
    return owners


def _check_coin_overlap(owners: dict[str, list]) -> None:
    clashes = {b: rows for b, rows in owners.items() if len(rows) > 1}
    if not clashes:
        return
    lines = [f"  {b}: " + ", ".join(f"{r.name} ({r.status})" for r in rows) for b, rows in sorted(clashes.items())]
    raise RuntimeError(
        "migration 050: base assets owned by more than one mandate in ACTIVE, PAUSED or HALTED:\n"
        + "\n".join(lines)
        + "\nFix: pause, stop or edit so each coin has one owner "
        "(reports/autotrade-profiles-plan.md O1), then run the migration again."
    )


def upgrade() -> None:
    bind = op.get_bind()

    # Fail before any change when two live mandates share a coin (the transaction would roll back anyway)
    locking = bind.execute(
        sa.text("SELECT id, name, status, instruments FROM ai_mandates WHERE status = ANY(:s) ORDER BY name"),
        {"s": list(LOCKING_STATUSES)},
    ).fetchall()
    owners = _coin_owners(locking)
    _check_coin_overlap(owners)

    op.execute("ALTER TABLE ai_autonomy_events DROP CONSTRAINT chk_ai_autonomy_events_kind")
    op.execute(f"ALTER TABLE ai_autonomy_events ADD CONSTRAINT chk_ai_autonomy_events_kind "
               f"CHECK (kind IN ({EVENT_KINDS_045}, {EVENT_KINDS_NEW[0]}))")

    op.execute("""
        ALTER TABLE ai_mandates
            ADD COLUMN environment VARCHAR(8) NOT NULL DEFAULT 'PAPER',
            ADD COLUMN markets TEXT[] NULL,
            ADD COLUMN direction VARCHAR(8) NOT NULL DEFAULT 'LONG',
            ADD COLUMN style VARCHAR(10) NOT NULL DEFAULT 'SWING',
            ADD COLUMN max_open_positions INTEGER NOT NULL DEFAULT 3,
            ADD COLUMN max_debates_per_day INTEGER NOT NULL DEFAULT 6,
            ADD COLUMN exit_tp_r NUMERIC(5, 2) NOT NULL DEFAULT 2.00,
            ADD COLUMN exit_tp1_scale_pct NUMERIC(5, 2) NOT NULL DEFAULT 50,
            ADD COLUMN exit_trail_activate_pct NUMERIC(5, 2) NULL,
            ADD COLUMN exit_trail_pct NUMERIC(5, 2) NULL,
            ADD COLUMN exit_max_hold_hours INTEGER NULL,
            ADD COLUMN dry_run BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN notify_level VARCHAR(10) NOT NULL DEFAULT 'IMPORTANT',
            ADD COLUMN breaker_max_loss_streak INTEGER NOT NULL DEFAULT 4,
            ADD COLUMN breaker_max_reject_streak INTEGER NOT NULL DEFAULT 5,
            ADD COLUMN paused_reason TEXT NULL
    """)

    # Backfill 1: markets from the instruments (plus PERP when allow_perps), then allow_perps follows markets
    op.execute("""
        UPDATE ai_mandates m SET markets = COALESCE((
                SELECT array_agg(DISTINCT x ORDER BY x) FROM (
                    SELECT 'SPOT' AS x FROM unnest(m.instruments) AS i WHERE split_part(upper(i), '.', 1) NOT LIKE '%-PERP'
                    UNION
                    SELECT 'PERP' WHERE m.allow_perps
                ) t
            ), ARRAY['SPOT']::text[])
    """)
    op.execute("UPDATE ai_mandates SET allow_perps = ('PERP' = ANY(markets))")
    # Leverage only exists on PERP and SPOT_MARGIN: a spot-only mandate with max_leverage > 1 could never use it
    op.execute("""UPDATE ai_mandates SET max_leverage = 1
                  WHERE max_leverage <> 1 AND NOT (markets && ARRAY['SPOT_MARGIN', 'PERP']::text[])""")
    op.execute("ALTER TABLE ai_mandates ALTER COLUMN markets SET NOT NULL")

    # Backfill 2: D9, every non-archived mandate gets a daily loss limit (049's chk_..._daily_loss_ge_risk holds)
    op.execute("""UPDATE ai_mandates SET daily_loss_limit_pct = LEAST(3 * max_risk_per_trade_pct, max_drawdown_pct)
                  WHERE daily_loss_limit_pct IS NULL AND status <> 'ARCHIVED'""")

    # Column CHECKs
    checks = {
        "environment": "environment IN ('PAPER')",
        "markets": "cardinality(markets) >= 1 AND markets <@ ARRAY['SPOT', 'SPOT_MARGIN', 'PERP']::text[]",
        "direction": "direction IN ('LONG', 'SHORT', 'BOTH')",
        "style": "style IN ('SWING', 'INTRADAY')",
        "max_open_positions": "max_open_positions BETWEEN 1 AND 20",
        "max_debates": "max_debates_per_day BETWEEN 0 AND 48",
        "exit_tp_r": "exit_tp_r BETWEEN 0.5 AND 20",
        "exit_tp1": "exit_tp1_scale_pct BETWEEN 0 AND 100",
        "exit_trail_activate": "exit_trail_activate_pct IS NULL OR (exit_trail_activate_pct > 0 AND exit_trail_activate_pct <= 50)",
        "exit_trail": """exit_trail_pct IS NULL OR (exit_trail_pct > 0 AND exit_trail_pct <= 50
                         AND (exit_trail_activate_pct IS NULL OR exit_trail_pct <= exit_trail_activate_pct))""",
        "exit_max_hold": "exit_max_hold_hours IS NULL OR exit_max_hold_hours BETWEEN 1 AND 720",
        "notify_level": "notify_level IN ('ALL', 'IMPORTANT', 'NONE')",
        "breaker_loss": "breaker_max_loss_streak BETWEEN 1 AND 50",
        "breaker_reject": "breaker_max_reject_streak BETWEEN 1 AND 50",
    }
    assert tuple(checks) == COLUMN_CHECKS
    for name, rule in checks.items():
        op.execute(f"ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_{name} CHECK ({rule})")

    # D4 / status: perps at any mode; STOPPING and STOPPED are new run states
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_perps_level")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_status")
    op.execute("""ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_status
                  CHECK (status IN ('ACTIVE', 'HALTED', 'PAUSED', 'STOPPING', 'STOPPED', 'ARCHIVED'))""")

    # Cross-field CHECKs, after the backfill
    cross = {
        "short_market": "direction = 'LONG' OR markets && ARRAY['SPOT_MARGIN', 'PERP']::text[]",
        "leverage_market": "max_leverage = 1 OR markets && ARRAY['SPOT_MARGIN', 'PERP']::text[]",
        "perps_markets": "allow_perps = ('PERP' = ANY(markets))",
        "running_daily_loss": "status <> 'ACTIVE' OR daily_loss_limit_pct IS NOT NULL",
    }
    assert tuple(cross) == CROSS_CHECKS
    for name, rule in cross.items():
        op.execute(f"ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_{name} CHECK ({rule})")

    # Coin locks (D6)
    op.execute("""
        CREATE TABLE ai_profile_coin_locks (
            base_asset VARCHAR(20) PRIMARY KEY,
            mandate_id UUID NOT NULL REFERENCES ai_mandates(id) ON DELETE CASCADE,
            locked_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_ai_profile_coin_locks_mandate ON ai_profile_coin_locks (mandate_id)")
    for base, rows in sorted(owners.items()):
        bind.execute(
            sa.text("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES (:b, :m)"),
            {"b": base, "m": rows[0].id},
        )

    # Decision log
    op.execute("""
        CREATE TABLE ai_profile_decisions (
            id BIGSERIAL PRIMARY KEY,
            mandate_id UUID NOT NULL REFERENCES ai_mandates(id) ON DELETE CASCADE,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            instrument_id VARCHAR(64),
            stage VARCHAR(12) NOT NULL CONSTRAINT chk_ai_profile_decisions_stage CHECK (stage IN
                ('SCAN', 'GATE', 'DEBATE', 'ROUTE', 'APPROVAL', 'ORDER', 'FILL', 'EXIT', 'LIFECYCLE', 'BREAKER', 'SESSION')),
            outcome VARCHAR(40) NOT NULL,
            reason TEXT,
            detail JSONB NOT NULL DEFAULT '{}'::jsonb,
            run_id UUID,
            command_id UUID
        )
    """)
    op.execute("CREATE INDEX ix_ai_profile_decisions_mandate_ts ON ai_profile_decisions (mandate_id, ts DESC)")
    op.execute("CREATE INDEX ix_ai_profile_decisions_ts ON ai_profile_decisions (ts)")

    # Grants (the pg_roles guard follows 049); orchestrator, scanner and exit monitor connect as the owner
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT, INSERT, DELETE ON TABLE ai_profile_coin_locks TO cryptonite_api;
            GRANT SELECT, INSERT ON TABLE ai_profile_decisions TO cryptonite_api;
            GRANT USAGE, SELECT ON SEQUENCE ai_profile_decisions_id_seq TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            REVOKE ALL ON TABLE ai_profile_coin_locks, ai_profile_decisions FROM cryptonite_agent_ro;
        END IF;
    END $$;
    """)

    # Writers that predate profiles (API create, Brain card remedy, fixtures) omit markets and the daily loss limit
    op.execute(TRIGGER_FN)
    op.execute(f"""CREATE TRIGGER {TRIGGER} BEFORE INSERT OR UPDATE ON ai_mandates
                  FOR EACH ROW EXECUTE FUNCTION ai_mandates_profile_defaults()""")

    # The Brain's view: 049's columns, then the profile columns
    op.execute(f"CREATE OR REPLACE VIEW {HEALTH_VIEW} WITH (security_barrier) AS {HEALTH_050}")
    op.execute(f"GRANT SELECT ON {HEALTH_VIEW} TO cryptonite_agent_ro")


def downgrade() -> None:
    bind = op.get_bind()
    blockers = bind.execute(sa.text("""
        SELECT name, status, markets, allow_perps, autonomy_level FROM ai_mandates
        WHERE status IN ('STOPPING', 'STOPPED') OR 'SPOT_MARGIN' = ANY(markets)
           OR (allow_perps AND autonomy_level <> 1)
        ORDER BY name
    """)).fetchall()
    if blockers:
        rows = "\n".join(
            f"  {r.name}: status={r.status} markets={list(r.markets)} allow_perps={r.allow_perps} level={r.autonomy_level}"
            for r in blockers
        )
        raise RuntimeError(
            "migration 050 downgrade refused: these mandates cannot exist in 049 (status STOPPING/STOPPED, SPOT_MARGIN "
            "market, or perps away from level 1). Archive, edit or delete them first:\n" + rows
        )

    # Dropping the kinds would delete audit history: refuse while any lifecycle event exists
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM ai_autonomy_events WHERE kind IN ('STARTED', 'STOPPING', 'STOPPED', 'MODE_SET')) THEN
            RAISE EXCEPTION 'migration 050 downgrade refused: ai_autonomy_events has STARTED/STOPPING/STOPPED/MODE_SET rows';
        END IF; END $$""")
    op.execute("ALTER TABLE ai_autonomy_events DROP CONSTRAINT chk_ai_autonomy_events_kind")
    op.execute(f"ALTER TABLE ai_autonomy_events ADD CONSTRAINT chk_ai_autonomy_events_kind CHECK (kind IN ({EVENT_KINDS_045}))")
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON ai_mandates")
    op.execute("DROP FUNCTION IF EXISTS ai_mandates_profile_defaults()")
    op.execute(f"DROP VIEW IF EXISTS {HEALTH_VIEW}")
    op.execute(f"CREATE VIEW {HEALTH_VIEW} WITH (security_barrier) AS {HEALTH_049}")
    op.execute(f"GRANT SELECT ON {HEALTH_VIEW} TO cryptonite_agent_ro")
    op.execute("DROP TABLE IF EXISTS ai_profile_decisions")
    op.execute("DROP TABLE IF EXISTS ai_profile_coin_locks")
    for name in (*CROSS_CHECKS, *COLUMN_CHECKS):
        op.execute(f"ALTER TABLE ai_mandates DROP CONSTRAINT IF EXISTS chk_ai_mandates_{name}")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_status")
    op.execute("""ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_status
                  CHECK (status IN ('ACTIVE', 'HALTED', 'PAUSED', 'ARCHIVED'))""")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_perps_level CHECK (NOT allow_perps OR autonomy_level = 1)")
    op.execute("ALTER TABLE ai_mandates " + ", ".join(f"DROP COLUMN {c}" for c in NEW_COLUMNS))
