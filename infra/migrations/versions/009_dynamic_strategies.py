"""dynamic strategies: python_code column

Revision ID: 009
Revises: 008
Create Date: 2026-09-18 21:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '009'
down_revision: Union[str, None] = '008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # Add python_code to strategy_versions
    op.add_column(
        'strategy_versions',
        sa.Column('python_code', sa.Text(), nullable=True)
    )

def downgrade() -> None:
    op.drop_column('strategy_versions', 'python_code')
