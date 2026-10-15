"""agent_safe_ai_state: the AI's own state (mandates, verdicts, approvals, reflections, latches) for the Brain

Revision ID: 046
Revises: 045
Create Date: 2026-10-15 00:00:00.000000

The Brain reads the database only as ``cryptonite_agent_ro``, which may read only the ``agent_safe_*`` views (migration
014). Without these views it cannot see the mandates it proposes for, the debate verdicts and approvals the other agents
produced, or whether a kill switch or the daily loss limit is latched, so it proposes order cards blind. Each view
exposes the columns the Brain needs and nothing else: no ``created_by``, ``active_hours_utc``, ``latched_by``,
``reset_by``, ``payload``, ``decided_by``, ``trace_id`` or embeddings. The views run as their owner, so the agent role
needs no grant on the base tables. Reasoning text is cut to 1500 characters.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "046"
down_revision: str | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEWS = (
    "agent_safe_mandate_health",
    "agent_safe_debate_verdicts",
    "agent_safe_open_approvals",
    "agent_safe_reflections",
    "agent_safe_risk_latches",
    "agent_safe_daily_loss",
)


def upgrade() -> None:
    # Live (not archived) mandates, with the latest equity row of each
    op.execute("""
        CREATE OR REPLACE VIEW agent_safe_mandate_health WITH (security_barrier) AS
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
    """)
    op.execute("GRANT SELECT ON agent_safe_mandate_health TO cryptonite_agent_ro")

    # Debate runs of the last 30 days, with the latest portfolio decision of each (a run may have several rows)
    op.execute("""
        CREATE OR REPLACE VIEW agent_safe_debate_verdicts WITH (security_barrier) AS
        SELECT r.id AS run_id, r.instrument_id, r.status, r.trigger_source, r.created_at, r.completed_at,
               r.failure_reason, r.mandate_id, r.decision_trace_id,
               d.recommendation, d.conviction_score, d.conviction_level, d.allocation_pct, d.decision_price,
               left(d.reasoning, 1500) AS reasoning
        FROM debate_runs r
        LEFT JOIN LATERAL (
            SELECT recommendation, conviction_score, conviction_level, allocation_pct, decision_price, reasoning
            FROM portfolio_decisions
            WHERE run_id = r.id
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        ) d ON TRUE
        WHERE r.created_at >= now() - interval '30 days'
    """)
    op.execute("GRANT SELECT ON agent_safe_debate_verdicts TO cryptonite_agent_ro")

    # Approvals still waiting for a decision
    op.execute("""
        CREATE OR REPLACE VIEW agent_safe_open_approvals WITH (security_barrier) AS
        SELECT id, mandate_id, action_class, command_type, summary, source, level_at_creation, timeout_policy,
               expires_at, created_at
        FROM ai_approvals
        WHERE status = 'PENDING'
    """)
    op.execute("GRANT SELECT ON agent_safe_open_approvals TO cryptonite_agent_ro")

    # Post-trade reflections, with the instrument of their debate run
    op.execute("""
        CREATE OR REPLACE VIEW agent_safe_reflections WITH (security_barrier) AS
        SELECT f.id, f.run_id, r.instrument_id, f.timeframe, f.volatility_regime, f.outcome, f.actual_outcome_pnl,
               f.alpha_vs_benchmark, f.lessons_learned, f.created_at
        FROM reflections f
        LEFT JOIN debate_runs r ON r.id = f.run_id
    """)
    op.execute("GRANT SELECT ON agent_safe_reflections TO cryptonite_agent_ro")

    # Kill switch state per environment and account: whether it is latched, and why
    op.execute("""
        CREATE OR REPLACE VIEW agent_safe_risk_latches WITH (security_barrier) AS
        SELECT environment, account_id, is_latched, latched_at, reason
        FROM execution_kill_switch
    """)
    op.execute("GRANT SELECT ON agent_safe_risk_latches TO cryptonite_agent_ro")

    # The daily loss limit of each paper account: today's realized P&L and whether it is latched
    op.execute("""
        CREATE OR REPLACE VIEW agent_safe_daily_loss WITH (security_barrier) AS
        SELECT account_id, utc_trading_day, daily_realized_pnl, daily_loss_latched
        FROM paper_accounts
    """)
    op.execute("GRANT SELECT ON agent_safe_daily_loss TO cryptonite_agent_ro")


def downgrade() -> None:
    for view in reversed(VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")
