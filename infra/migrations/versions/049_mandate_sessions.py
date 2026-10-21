"""ai_mandates: daily sessions and entry filters (ADR 0010, auto-trade Phase A)

Revision ID: 049
Revises: 048
Create Date: 2026-10-21 00:00:00.000000

- ``ai_mandates`` gains two refuse-only entry filters (``min_conviction``, ``min_rr``) and the daily session limits
  (``daily_loss_limit_pct``, ``daily_profit_target_pct``, ``profit_lock_pct``). The filters and limits are NULL (off)
  by default; ``profit_lock_pct`` defaults to 50. A mandate that already has perps enabled gets the ADR 0010 default
  daily loss limit, 3 x its risk per trade capped at its hard stop, as a new perps mandate does.
- ``ai_mandate_daily_sessions``: one row per mandate and UTC day, written by the orchestrator's mandate monitor. It
  holds the day's start equity, P&L and the times the loss latch, the target and the profit lock were recorded. There
  is no state column: the verdict is derived (``cryptonite_schemas.session.session_verdict``). ``start_budget`` is the
  budget when the row was opened, so a budget edit during the day does not read as P&L.
- ``cryptonite_api`` (API, debate worker) may read the sessions; ``cryptonite_agent_ro`` gets nothing on the table.
  ``agent_safe_mandate_health`` (the Brain's read-only view, migration 046) gains the limits and today's session.

The downgrade drops the table and the columns and restores 046's view.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "049"
down_revision: str | None = "048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HEALTH_VIEW = "agent_safe_mandate_health"

# 046's view, as it was (the downgrade restores it)
HEALTH_046 = """
    SELECT m.id, m.name, m.status, m.autonomy_level, m.level_since, m.instruments, m.capital_budget_usd,
           m.max_position_pct, m.max_drawdown_pct, m.halted_reason, m.halted_at,
           e.equity, e.hwm, e.drawdown_pct, e.ts AS equity_ts
    FROM ai_mandates m
    LEFT JOIN LATERAL (
        SELECT equity, hwm, drawdown_pct, ts
        FROM ai_mandate_equity
        WHERE mandate_id = m.id
        ORDER BY ts DESC, id DESC
        LIMIT 1
    ) e ON TRUE
    WHERE m.status <> 'ARCHIVED'
"""


def upgrade() -> None:
    op.execute("""
        ALTER TABLE ai_mandates
            ADD COLUMN min_conviction NUMERIC(3, 2) NULL,
            ADD COLUMN min_rr NUMERIC(4, 2) NULL,
            ADD COLUMN daily_loss_limit_pct NUMERIC(5, 2) NULL,
            ADD COLUMN daily_profit_target_pct NUMERIC(5, 2) NULL,
            ADD COLUMN profit_lock_pct NUMERIC(5, 2) NOT NULL DEFAULT 50
    """)
    op.execute("""ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_min_conviction
                  CHECK (min_conviction BETWEEN 0.50 AND 0.95)""")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_min_rr CHECK (min_rr >= 1.00)")
    op.execute("""ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_daily_loss_limit
                  CHECK (daily_loss_limit_pct > 0 AND daily_loss_limit_pct <= max_drawdown_pct)""")
    op.execute("""ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_daily_profit_target
                  CHECK (daily_profit_target_pct > 0 AND daily_profit_target_pct <= 100)""")
    op.execute("""ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_profit_lock
                  CHECK (profit_lock_pct BETWEEN 0 AND 100)""")
    # ADR 0010: a perps mandate without a daily loss limit gets 3 x its risk per trade, at most its hard stop
    op.execute("""UPDATE ai_mandates SET daily_loss_limit_pct = LEAST(3 * max_risk_per_trade_pct, max_drawdown_pct)
                  WHERE allow_perps AND daily_loss_limit_pct IS NULL
                    AND LEAST(3 * max_risk_per_trade_pct, max_drawdown_pct) >= max_risk_per_trade_pct""")
    # One stopped trade must fit in the day: also in the DB, so a row that breaks it cannot reach load_mandate
    op.execute("""ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_daily_loss_ge_risk
                  CHECK (daily_loss_limit_pct IS NULL OR daily_loss_limit_pct >= max_risk_per_trade_pct)""")

    op.execute("""
        CREATE TABLE ai_mandate_daily_sessions (
            mandate_id UUID NOT NULL REFERENCES ai_mandates(id) ON DELETE CASCADE,
            session_date DATE NOT NULL,
            start_equity NUMERIC(16, 2) NOT NULL,
            start_budget NUMERIC(16, 2) NOT NULL,
            peak_pnl NUMERIC(16, 2) NOT NULL DEFAULT 0,
            last_pnl NUMERIC(16, 2) NOT NULL DEFAULT 0,
            loss_latched_at TIMESTAMPTZ NULL,
            target_hit_at TIMESTAMPTZ NULL,
            lock_fired_at TIMESTAMPTZ NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (mandate_id, session_date)
        )
    """)
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT ON TABLE ai_mandate_daily_sessions TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            REVOKE ALL ON TABLE ai_mandate_daily_sessions FROM cryptonite_agent_ro;
        END IF;
    END $$;
    """)

    # The Brain's view: 046's columns, then the limits and today's session (CREATE OR REPLACE may only append)
    op.execute(f"""
        CREATE OR REPLACE VIEW {HEALTH_VIEW} WITH (security_barrier) AS
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
    """)
    op.execute(f"GRANT SELECT ON {HEALTH_VIEW} TO cryptonite_agent_ro")


def downgrade() -> None:
    # 046's view has fewer columns: drop it and create it again
    op.execute(f"DROP VIEW IF EXISTS {HEALTH_VIEW}")
    op.execute(f"CREATE VIEW {HEALTH_VIEW} WITH (security_barrier) AS {HEALTH_046}")
    op.execute(f"GRANT SELECT ON {HEALTH_VIEW} TO cryptonite_agent_ro")
    op.execute("DROP TABLE IF EXISTS ai_mandate_daily_sessions")
    for name in ("daily_loss_ge_risk", "profit_lock", "daily_profit_target", "daily_loss_limit", "min_rr", "min_conviction"):
        op.execute(f"ALTER TABLE ai_mandates DROP CONSTRAINT IF EXISTS chk_ai_mandates_{name}")
    op.execute("""
        ALTER TABLE ai_mandates
            DROP COLUMN profit_lock_pct,
            DROP COLUMN daily_profit_target_pct,
            DROP COLUMN daily_loss_limit_pct,
            DROP COLUMN min_rr,
            DROP COLUMN min_conviction
    """)
