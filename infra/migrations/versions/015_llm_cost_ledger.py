"""llm_cost_ledger

Revision ID: 015
Revises: 014
Create Date: 2026-09-21 13:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '015'
down_revision: Union[str, None] = '014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # LLM Cost Ledger
    op.create_table(
        'llm_cost_ledger',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('request_id', sa.String(128), unique=True, nullable=False),
        sa.Column('agent_name', sa.String(64), nullable=False),
        sa.Column('model_name', sa.String(64), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_cost_usd', sa.Numeric(16, 6), nullable=False, server_default='0.00'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    
    op.create_index('ix_llm_cost_ledger_created_at', 'llm_cost_ledger', ['created_at'])

    # Add daily budget ceiling circuit breaker setting
    op.execute("""
    INSERT INTO system_settings (key, value)
    VALUES ('LLM_DAILY_BUDGET_USD', '18.00')  -- max daily spend across all agents before the circuit breaker trips
    ON CONFLICT (key) DO NOTHING;
    """)

    # Grant api to write to ledger
    op.execute("""
    GRANT SELECT, INSERT ON TABLE llm_cost_ledger TO cryptonite_api;
    GRANT USAGE, SELECT ON SEQUENCE llm_cost_ledger_id_seq TO cryptonite_api;
    """)

def downgrade() -> None:
    op.drop_table('llm_cost_ledger')
