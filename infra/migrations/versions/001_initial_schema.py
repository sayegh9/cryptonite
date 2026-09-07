"""initial schema

Revision ID: 001
Revises: 
Create Date: 2026-09-07 18:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. instruments
    op.create_table(
        'instruments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False, unique=True),
        sa.Column('raw_symbol', sa.String(32), nullable=False),
        sa.Column('venue', sa.String(32), nullable=False, server_default='BINANCE'),
        sa.Column('market_type', sa.String(32), nullable=False, server_default='SPOT'),
        sa.Column('base_currency', sa.String(16), nullable=False),
        sa.Column('quote_currency', sa.String(16), nullable=False),
        sa.Column('price_precision', sa.Integer(), nullable=False),
        sa.Column('size_precision', sa.Integer(), nullable=False),
        sa.Column('price_increment', sa.Numeric(38, 18), nullable=False),
        sa.Column('size_increment', sa.Numeric(38, 18), nullable=False),
        sa.Column('min_notional', sa.Numeric(38, 18), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='TRADING'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_instruments_raw_symbol', 'instruments', ['raw_symbol'])

    # 2. ingest_batches
    op.create_table(
        'ingest_batches',
        sa.Column('batch_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('venue', sa.String(32), nullable=False),
        sa.Column('market_type', sa.String(32), nullable=False),
        sa.Column('symbol', sa.String(32), nullable=False),
        sa.Column('bar_type', sa.String(128), nullable=False),
        sa.Column('start_ns', sa.BigInteger(), nullable=False),
        sa.Column('end_ns', sa.BigInteger(), nullable=False),
        sa.Column('bar_count', sa.Integer(), nullable=False),
        sa.Column('payload_checksum', sa.String(64), nullable=False),
        sa.Column('canonical_payload_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('nautilus_paths', postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column('analytics_paths', postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column('nautilus_file_checksum', sa.String(64), nullable=True),
        sa.Column('analytics_file_checksum', sa.String(64), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('committed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('batch_id'),
        sa.UniqueConstraint('venue', 'market_type', 'bar_type', 'start_ns', 'end_ns', 'payload_checksum', name='uq_batch_data_range')
    )
    op.create_index('ix_ingest_batches_status', 'ingest_batches', ['status'])

    # 3. ingest_checkpoints
    op.create_table(
        'ingest_checkpoints',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('venue', sa.String(32), nullable=False),
        sa.Column('market_type', sa.String(32), nullable=False),
        sa.Column('symbol', sa.String(32), nullable=False),
        sa.Column('bar_type', sa.String(128), nullable=False),
        sa.Column('timeframe', sa.String(16), nullable=False),
        sa.Column('first_timestamp_ns', sa.BigInteger(), nullable=False),
        sa.Column('last_timestamp_ns', sa.BigInteger(), nullable=False),
        sa.Column('total_bars', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('last_batch_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('last_updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['last_batch_id'], ['ingest_batches.batch_id'], ondelete='SET NULL'),
        sa.UniqueConstraint('venue', 'market_type', 'symbol', 'timeframe', name='uq_checkpoint_stream')
    )

    # 4. quarantine_bars
    op.create_table(
        'quarantine_bars',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('venue', sa.String(32), nullable=False),
        sa.Column('symbol', sa.String(32), nullable=False),
        sa.Column('bar_type', sa.String(128), nullable=False),
        sa.Column('ts_event_ns', sa.BigInteger(), nullable=False),
        sa.Column('conflicting_data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('existing_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('quarantined_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # 5. audit_events
    op.create_table(
        'audit_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('source', sa.String(64), nullable=False),
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('audit_events')
    op.drop_table('quarantine_bars')
    op.drop_table('ingest_checkpoints')
    op.drop_table('ingest_batches')
    op.drop_table('instruments')
