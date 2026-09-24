"""pgvector_reflections

Revision ID: 017
Revises: 016
Create Date: 2026-09-21 23:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '017'
down_revision: Union[str, None] = '016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector;')
    op.execute('ALTER TABLE reflections ADD COLUMN IF NOT EXISTS embedding vector(768);')
    op.add_column('reflections', sa.Column('instrument_type', sa.String(32), nullable=True))
    op.add_column('reflections', sa.Column('volatility_regime', sa.String(32), nullable=True))
    op.add_column('reflections', sa.Column('timeframe', sa.String(16), nullable=True))
    op.add_column('reflections', sa.Column('outcome', sa.String(16), nullable=True))

def downgrade() -> None:
    op.drop_column('reflections', 'outcome')
    op.drop_column('reflections', 'timeframe')
    op.drop_column('reflections', 'volatility_regime')
    op.drop_column('reflections', 'instrument_type')
    op.drop_column('reflections', 'embedding')
    op.execute('DROP EXTENSION IF EXISTS vector;')
