"""backtest schema

Revision ID: 003
Revises: 002
Create Date: 2026-09-07 22:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '003'
down_revision: str | None = '002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. strategies table
    op.create_table(
        'strategies',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('slug', sa.String(64), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('author', sa.String(128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
        sa.UniqueConstraint('slug')
    )

    # 2. strategy_versions table
    op.create_table(
        'strategy_versions',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('strategy_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('version_num', sa.Integer(), nullable=False),
        sa.Column('code_hash', sa.String(64), nullable=False),
        sa.Column('git_commit', sa.String(64), nullable=False),
        sa.Column('default_parameters', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('parameter_schema', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('strategy_id', 'version_num', name='uq_strategy_version_num')
    )

    # 3. backtest_runs table
    op.create_table(
        'backtest_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('strategy_version_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('bar_type', sa.String(128), nullable=False),
        sa.Column('timeframe', sa.String(16), nullable=False),
        sa.Column('parameters', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('warmup_start_ns', sa.BigInteger(), nullable=False),
        sa.Column('evaluation_start_ns', sa.BigInteger(), nullable=False),
        sa.Column('evaluation_end_ns', sa.BigInteger(), nullable=False),
        sa.Column('execution_profile', sa.String(32), nullable=False),
        sa.Column('run_config_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(32), server_default='PENDING', nullable=False),
        sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('lease_owner', sa.String(128), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['strategy_version_id'], ['strategy_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_backtest_runs_status', 'backtest_runs', ['status', 'lease_expires_at'])
    op.create_index('ix_backtest_runs_claim', 'backtest_runs', ['status', 'attempt_count', 'lease_expires_at'])
    op.create_index('ix_backtest_runs_strategy', 'backtest_runs', ['strategy_version_id', 'created_at'])

    # 4. backtest_results table
    op.create_table(
        'backtest_results',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('backtest_run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('total_return', sa.Numeric(16, 8), nullable=False),
        sa.Column('cagr', sa.Numeric(16, 8), nullable=True),
        sa.Column('sharpe_ratio', sa.Numeric(12, 6), nullable=True),
        sa.Column('sortino_ratio', sa.Numeric(12, 6), nullable=True),
        sa.Column('max_drawdown', sa.Numeric(12, 6), nullable=False),
        sa.Column('profit_factor', sa.Numeric(12, 6), nullable=True),
        sa.Column('win_rate', sa.Numeric(12, 6), nullable=False),
        sa.Column('total_trades', sa.Integer(), nullable=False),
        sa.Column('winning_trades', sa.Integer(), nullable=False),
        sa.Column('losing_trades', sa.Integer(), nullable=False),
        sa.Column('avg_trade_pnl', sa.Numeric(24, 8), nullable=False),
        sa.Column('max_consecutive_losses', sa.Integer(), nullable=False),
        sa.Column('downsampled_equity', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('monthly_returns', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('catalog_manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('artifact_manifest', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['backtest_run_id'], ['backtest_runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('backtest_run_id')
    )

    # 5. Role grants (if roles exist)
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_backtester') THEN
            GRANT SELECT ON TABLE strategies, strategy_versions, instruments, ingest_batches, ingest_checkpoints TO cryptonite_backtester;
            GRANT SELECT, UPDATE ON TABLE backtest_runs TO cryptonite_backtester;
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE backtest_results TO cryptonite_backtester;
        END IF;
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE strategies, strategy_versions, backtest_runs TO cryptonite_api;
            GRANT SELECT ON TABLE backtest_results TO cryptonite_api;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.drop_table('backtest_results')
    op.drop_table('backtest_runs')
    op.drop_table('strategy_versions')
    op.drop_table('strategies')
