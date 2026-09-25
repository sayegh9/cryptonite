"""ai evals: eval runs and per-case results

Revision ID: 023
Revises: 022
Create Date: 2026-09-25 20:00:00.000000

AI-first blueprint §8. Case sets live in the repo (packages/agents/cryptonite_agents/evals/sets,
versioned by git); the database keeps what each run measured.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '023'
down_revision: str | None = '022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'ai_eval_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('set_version', sa.String(64), nullable=False),
        sa.Column('model', sa.String(128), nullable=False),
        sa.Column('metrics', postgresql.JSONB(), nullable=False),
        sa.Column('passed', sa.Boolean(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_table(
        'ai_eval_results',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('ai_eval_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('case_id', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('model', sa.String(128), nullable=False),
        sa.Column('passed', sa.Boolean(), nullable=False),
        sa.Column('valid', sa.Boolean(), nullable=False),
        sa.Column('checks', postgresql.JSONB(), nullable=False),
        sa.Column('output', postgresql.JSONB(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('cost_usd', sa.Numeric(16, 6), nullable=False, server_default='0'),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('tool_calls', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_index('ix_ai_eval_results_run_id', 'ai_eval_results', ['run_id'])
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT ON TABLE ai_eval_runs, ai_eval_results TO cryptonite_api;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.drop_index('ix_ai_eval_results_run_id', table_name='ai_eval_results')
    op.drop_table('ai_eval_results')
    op.drop_table('ai_eval_runs')
