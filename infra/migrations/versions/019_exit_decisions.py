"""exit_decisions

Revision ID: 019
Revises: 018
Create Date: 2024-05-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '019'
down_revision = '018'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'exit_decisions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('run_id', sa.UUID(), nullable=True),
        sa.Column('instrument_id', sa.String(), nullable=True),
        sa.Column('recommendation', sa.String(), nullable=True),
        sa.Column('conviction_score', sa.Numeric(), nullable=True),
        sa.Column('target_stop_price', sa.Numeric(), nullable=True),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['run_id'], ['debate_runs.id'], )
    )

def downgrade() -> None:
    op.drop_table('exit_decisions')
