"""Expose profile limits, open-position counts, and retained decisions to the read-only Brain role.

Revision ID: 053
Revises: 052

The only grants are SELECT on two security-barrier views. The agent role never receives a grant on
``ai_mandates``, ``ai_profile_decisions``, or ``paper_positions``. The health view appends columns so
existing readers retain their ordinal contract. Downgrade restores the exact 052 column list.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "053"
down_revision: str | None = "052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HEALTH_VIEW = "agent_safe_mandate_health"
DECISIONS_VIEW = "agent_safe_profile_decisions"

# The view as migration 050 left it; migrations 051 and 052 do not change it. PostgreSQL cannot remove
# appended view columns with CREATE OR REPLACE, so downgrade drops and recreates this exact definition.
HEALTH_052 = """
    SELECT m.id, m.name, m.status, m.autonomy_level, m.level_since, m.instruments, m.capital_budget_usd,
           m.max_position_pct, m.max_drawdown_pct, m.halted_reason, m.halted_at,
           e.equity, e.hwm, e.drawdown_pct, e.ts AS equity_ts,
           m.min_conviction, m.min_rr, m.daily_loss_limit_pct, m.daily_profit_target_pct, m.profit_lock_pct,
           s.session_date, s.start_equity AS session_start_equity, s.last_pnl AS session_pnl,
           s.loss_latched_at AS session_loss_latched_at, s.target_hit_at AS session_target_hit_at,
           s.lock_fired_at AS session_lock_fired_at,
           m.environment, m.markets, m.direction, m.style, m.max_open_positions, m.dry_run, m.paused_reason
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

# ``mandate_instance_id`` is ``AI_MANDATE_`` plus the UUID's 32 hex digits. The correlated count is
# restricted to the paper account and open rows with positive quantity; only the count leaves the view.
HEALTH_053 = HEALTH_052.replace(
    "m.dry_run, m.paused_reason\n",
    "m.dry_run, m.paused_reason,\n"
    "           m.max_leverage, m.max_risk_per_trade_pct, m.veto_window_s, m.max_debates_per_day,\n"
    "           m.exit_tp_r, m.exit_tp1_scale_pct, m.exit_trail_activate_pct, m.exit_trail_pct,\n"
    "           m.exit_max_hold_hours, m.notify_level, m.breaker_max_loss_streak,\n"
    "           m.breaker_max_reject_streak, m.created_at,\n"
    "           (SELECT count(*) FROM paper_positions p\n"
    "            WHERE p.account_id = 'PAPER-ACCOUNT-001'\n"
    "              AND p.instance_id = 'AI_MANDATE_' || replace(m.id::text, '-', '')\n"
    "              AND p.status = 'OPEN' AND p.quantity > 0) AS open_positions\n",
)


def upgrade() -> None:
    """Append safe profile columns and expose only recent decision rows."""
    op.execute(f"CREATE OR REPLACE VIEW {HEALTH_VIEW} WITH (security_barrier) AS {HEALTH_053}")
    op.execute(f"GRANT SELECT ON {HEALTH_VIEW} TO cryptonite_agent_ro")
    op.execute(f"""
        CREATE VIEW {DECISIONS_VIEW} WITH (security_barrier) AS
        SELECT id, mandate_id, ts, instrument_id, stage, outcome,
               reason AS reason_text, detail AS detail_json, run_id, command_id
        FROM ai_profile_decisions
        WHERE ts >= now() - interval '90 days'
    """)
    op.execute(f"GRANT SELECT ON {DECISIONS_VIEW} TO cryptonite_agent_ro")


def downgrade() -> None:
    """Remove the decisions view and restore 052's health view without losing profile data."""
    op.execute(f"DROP VIEW IF EXISTS {DECISIONS_VIEW}")
    op.execute(f"DROP VIEW IF EXISTS {HEALTH_VIEW}")
    op.execute(f"CREATE VIEW {HEALTH_VIEW} WITH (security_barrier) AS {HEALTH_052}")
    op.execute(f"GRANT SELECT ON {HEALTH_VIEW} TO cryptonite_agent_ro")
