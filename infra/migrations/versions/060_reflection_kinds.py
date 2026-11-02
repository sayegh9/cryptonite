"""H7a: reflections are labelled WHAT_IF (the run never filled) or TRADE; unlabelled what-if lessons leave recall

Revision ID: 060
Revises: 059
Create Date: 2026-11-02 19:00:00.000000

H7a item 5. The learner reflected on every directional verdict with a 24h outcome, filled or not, and some lessons
describe refused or vetoed cards as trades; ``recall_lessons`` fed them back to the PM. ``kind`` says which a
reflection is: TRADE when the run's entry SUBMIT_ORDER (queued directly, idempotency key ``debate:<run>``, or through
its approval card) has a paper order with a fill, else WHAT_IF. ``recall_excluded`` keeps a lesson out of recall: the
backfill sets it on every existing WHAT_IF row, whose text was written without the what-if label. New rows default to
WHAT_IF (the learner writes the kind it found) and are recalled. ``agent_safe_reflections`` gains both columns so the
Brain's card lessons skip excluded rows too. The fill rule is ``reflection.RUN_FILLS_SQL``; keep the two in step.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "060"
down_revision: str | None = "059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEW_046 = """
    CREATE OR REPLACE VIEW agent_safe_reflections WITH (security_barrier) AS
    SELECT f.id, f.run_id, r.instrument_id, f.timeframe, f.volatility_regime, f.outcome, f.actual_outcome_pnl,
           f.alpha_vs_benchmark, f.lessons_learned, f.created_at{extra}
    FROM reflections f
    LEFT JOIN debate_runs r ON r.id = f.run_id
"""


def upgrade() -> None:
    op.execute("""
        ALTER TABLE reflections
            ADD COLUMN kind varchar(8) NOT NULL DEFAULT 'WHAT_IF',
            ADD COLUMN recall_excluded boolean NOT NULL DEFAULT false,
            ADD CONSTRAINT chk_reflections_kind CHECK (kind IN ('WHAT_IF', 'TRADE'))
    """)
    op.execute("""
        UPDATE reflections f SET kind = 'TRADE'
        WHERE EXISTS (
            SELECT 1
            FROM execution_commands c
            JOIN paper_orders o ON o.client_order_id = COALESCE(
                c.payload->>'client_order_id', 'AI-' || upper(left(replace(c.command_id::text, '-', ''), 12)))
            WHERE c.command_type = 'SUBMIT_ORDER' AND o.filled_qty > 0
              AND (c.idempotency_key = 'debate:' || f.run_id::text
                   OR c.command_id IN (SELECT a.command_id FROM ai_approvals a
                                       WHERE a.source = 'debate' AND a.source_ref = f.run_id::text)))
    """)
    op.execute("UPDATE reflections SET recall_excluded = true WHERE kind = 'WHAT_IF'")
    op.execute(_VIEW_046.format(extra=", f.kind, f.recall_excluded"))
    op.execute("GRANT SELECT ON agent_safe_reflections TO cryptonite_agent_ro")


def downgrade() -> None:
    op.execute("DROP VIEW agent_safe_reflections")  # a view's columns cannot be dropped by CREATE OR REPLACE
    op.execute(_VIEW_046.format(extra=""))
    op.execute("GRANT SELECT ON agent_safe_reflections TO cryptonite_agent_ro")
    op.execute("ALTER TABLE reflections DROP CONSTRAINT chk_reflections_kind, DROP COLUMN recall_excluded, "
               "DROP COLUMN kind")
