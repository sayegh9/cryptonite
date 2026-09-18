"""market data: coingecko_id on instruments, user_alerts table

Revision ID: 008
Revises: 007
Create Date: 2026-09-18 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '008'
down_revision: Union[str, None] = '007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add coingecko_id to instruments for accurate CoinGecko lookups
    op.add_column(
        'instruments',
        sa.Column('coingecko_id', sa.String(64), nullable=True)
    )

    # 2. user_alerts table
    op.create_table(
        'user_alerts',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column(
            'instrument_id',
            sa.String(64),
            sa.ForeignKey('instruments.instrument_id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('condition', sa.String(16), nullable=False),
        sa.Column('threshold', sa.Numeric(38, 18), nullable=False),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default='true',
        ),
        sa.Column(
            'triggered_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.CheckConstraint(
            "condition IN ('above', 'below')",
            name='chk_user_alerts_condition',
        ),
        sa.CheckConstraint(
            'threshold > 0',
            name='chk_user_alerts_threshold_pos',
        ),
    )
    op.create_index(
        'ix_user_alerts_active',
        'user_alerts',
        ['instrument_id', 'is_active'],
    )

    # 3. Role grants
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE user_alerts TO cryptonite_api;
            GRANT UPDATE (coingecko_id) ON TABLE instruments TO cryptonite_api;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cryptonite_api;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE user_alerts FROM cryptonite_api;
        END IF;
    END $$;
    """)
    op.drop_index('ix_user_alerts_active', table_name='user_alerts')
    op.drop_table('user_alerts')
    op.drop_column('instruments', 'coingecko_id')
