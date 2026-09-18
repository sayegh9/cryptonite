"""system settings and dynamic agent configs

Revision ID: 010
Revises: 009
Create Date: 2026-09-18 22:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '010'
down_revision: Union[str, None] = '009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. system_settings
    op.create_table(
        'system_settings',
        sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # 2. agent_configs
    op.create_table(
        'agent_configs',
        sa.Column('agent_name', sa.String(64), primary_key=True),
        sa.Column('model_name', sa.String(64), nullable=True),
        sa.Column('temperature', sa.Float(), nullable=True),
        sa.Column('system_instructions', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # 3. Role grants
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE system_settings TO cryptonite_api;
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agent_configs TO cryptonite_api;
        END IF;
    END $$;
    """)

def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            REVOKE ALL ON TABLE system_settings FROM cryptonite_api;
            REVOKE ALL ON TABLE agent_configs FROM cryptonite_api;
        END IF;
    END $$;
    """)
    op.drop_table('agent_configs')
    op.drop_table('system_settings')
