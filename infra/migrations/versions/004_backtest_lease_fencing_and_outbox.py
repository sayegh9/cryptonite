"""backtest lease fencing and outbox permissions

Revision ID: 004
Revises: 003
Create Date: 2026-09-07 23:20:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '004'
down_revision: str | None = '003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add attempt_id to backtest_runs for lease fencing
    op.add_column(
        'backtest_runs',
        sa.Column('attempt_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index('ix_backtest_runs_attempt_id', 'backtest_runs', ['attempt_id'])

    # 2. Grant outbox permissions to cryptonite_backtester
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_backtester') THEN
            GRANT USAGE ON SCHEMA public TO cryptonite_backtester;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cryptonite_backtester;
            GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_backtester;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_backtester') THEN
            REVOKE INSERT, SELECT ON TABLE outbox_events FROM cryptonite_backtester;
        END IF;
    END $$;
    """)
    op.drop_index('ix_backtest_runs_attempt_id', table_name='backtest_runs')
    op.drop_column('backtest_runs', 'attempt_id')
