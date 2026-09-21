"""debate_tables

Revision ID: 016
Revises: 015
Create Date: 2026-09-21 13:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '016'
down_revision: Union[str, None] = '015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. debate_runs
    op.create_table(
        'debate_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, server_default='PENDING'),
        sa.Column('trigger_source', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index('ix_debate_runs_instrument_id', 'debate_runs', ['instrument_id'])
    op.create_index('ix_debate_runs_status', 'debate_runs', ['status'])

    # 2. debate_turns
    op.create_table(
        'debate_turns',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('debate_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('agent_role', sa.String(64), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)
    )
    op.create_index('ix_debate_turns_run_id', 'debate_turns', ['run_id'])

    # 3. portfolio_decisions
    op.create_table(
        'portfolio_decisions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('debate_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('recommendation', sa.String(32), nullable=False),
        sa.Column('conviction_score', sa.Numeric(5, 2), nullable=False),
        sa.Column('allocation_pct', sa.Numeric(5, 2), nullable=False),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)
    )
    op.create_index('ix_portfolio_decisions_run_id', 'portfolio_decisions', ['run_id'])
    
    # 4. reflections
    op.create_table(
        'reflections',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('debate_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('actual_outcome_pnl', sa.Numeric(16, 6), nullable=False),
        sa.Column('alpha_vs_benchmark', sa.Numeric(16, 6), nullable=False),
        sa.Column('lessons_learned', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)
    )

    # 5. paper_portfolio_decisions for Shadow Mode (Phase 6 testing)
    op.create_table(
        'paper_portfolio_decisions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('decision_payload', postgresql.JSONB, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)
    )

    # Grants
    op.execute("""
    GRANT SELECT, INSERT, UPDATE ON TABLE debate_runs, debate_turns, portfolio_decisions, reflections, paper_portfolio_decisions TO cryptonite_api;
    GRANT USAGE, SELECT ON SEQUENCE debate_turns_id_seq TO cryptonite_api;
    """)

def downgrade() -> None:
    op.drop_table('paper_portfolio_decisions')
    op.drop_table('reflections')
    op.drop_table('portfolio_decisions')
    op.drop_table('debate_turns')
    op.drop_table('debate_runs')
