"""add decision_price to portfolio_decisions

Revision ID: 018
Revises: 017
Create Date: 2024-05-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '018'
down_revision = '017'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('portfolio_decisions', sa.Column('decision_price', sa.Numeric(precision=18, scale=8), nullable=True))

def downgrade() -> None:
    op.drop_column('portfolio_decisions', 'decision_price')
