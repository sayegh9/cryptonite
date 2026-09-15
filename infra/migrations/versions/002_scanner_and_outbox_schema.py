"""scanner and outbox schema

Revision ID: 002
Revises: 001
Create Date: 2026-09-07 19:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '002'
down_revision: str | None = '001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. outbox_events table
    op.create_table(
        'outbox_events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('event_id', sa.String(64), nullable=False),
        sa.Column('stream_name', sa.String(64), nullable=False),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('producer', sa.String(64), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(32), server_default='PENDING', nullable=False),
        sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('lease_owner', sa.String(64), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('published_stream_id', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id')
    )
    op.create_index('ix_outbox_events_status', 'outbox_events', ['status'])
    op.create_index(
        'ix_outbox_events_claim',
        'outbox_events',
        ['status', 'next_attempt_at', 'lease_expires_at']
    )

    # 2. scan_specs table
    op.create_table(
        'scan_specs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('slug', sa.String(64), nullable=False),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('active_version_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('is_archived', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug')
    )

    # 3. scan_spec_versions table
    op.create_table(
        'scan_spec_versions',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('spec_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('version_num', sa.Integer(), nullable=False),
        sa.Column('spec_hash', sa.String(64), nullable=False),
        sa.Column('universe', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['spec_id'], ['scan_specs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('spec_id', 'version_num', name='uq_spec_version_num')
    )
    op.create_index('ix_scan_spec_versions_hash', 'scan_spec_versions', ['spec_hash'])

    # Foreign key from scan_specs.active_version_id to scan_spec_versions.id
    op.create_foreign_key(
        'fk_scan_specs_active_version',
        'scan_specs',
        'scan_spec_versions',
        ['active_version_id'],
        ['id'],
        ondelete='SET NULL'
    )

    # 4. scan_runs table
    op.create_table(
        'scan_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('spec_version_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('spec_hash', sa.String(64), nullable=False),
        sa.Column('scanner_version', sa.String(32), nullable=False),
        sa.Column('indicator_version', sa.String(32), nullable=False),
        sa.Column('input_watermark_ns', sa.BigInteger(), nullable=False),
        sa.Column('source_partitions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('source_partition_checksums', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('trigger_reason', sa.String(32), nullable=False),
        sa.Column('trigger_event_id', sa.String(64), nullable=True),
        sa.Column('status', sa.String(32), server_default='RUNNING', nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('candidate_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['spec_version_id'], ['scan_spec_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    # Partial unique index for event-triggered runs (idempotency upon Redis redelivery)
    op.create_index(
        'uq_scan_runs_event_triggered',
        'scan_runs',
        ['spec_version_id', 'trigger_event_id'],
        unique=True,
        postgresql_where=sa.text('trigger_event_id IS NOT NULL')
    )

    # 5. scan_jobs table
    op.create_table(
        'scan_jobs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('request_id', sa.String(64), nullable=False),
        sa.Column('spec_version_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.String(32), server_default='PENDING', nullable=False),
        sa.Column('requested_watermark_ns', sa.BigInteger(), nullable=True),
        sa.Column('requested_range_start_ns', sa.BigInteger(), nullable=True),
        sa.Column('requested_range_end_ns', sa.BigInteger(), nullable=True),
        sa.Column('scan_run_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['spec_version_id'], ['scan_spec_versions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['scan_run_id'], ['scan_runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('request_id')
    )
    op.create_index('ix_scan_jobs_status', 'scan_jobs', ['status'])

    # 6. signal_candidates table
    op.create_table(
        'signal_candidates',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('scan_run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('spec_version_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('timeframe', sa.String(16), nullable=False),
        sa.Column('bar_open_ns', sa.BigInteger(), nullable=False),
        sa.Column('bar_close_inclusive_ns', sa.BigInteger(), nullable=False),
        sa.Column('interval_end_exclusive_ns', sa.BigInteger(), nullable=False),
        sa.Column('matched_rules', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('suggested_entry', sa.Numeric(38, 18), nullable=False),
        sa.Column('suggested_stop', sa.Numeric(38, 18), nullable=False),
        sa.Column('suggested_target', sa.Numeric(38, 18), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['scan_run_id'], ['scan_runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['spec_version_id'], ['scan_spec_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'spec_version_id', 'instrument_id', 'timeframe', 'bar_close_inclusive_ns',
            name='uq_signal_candidate_idempotent'
        )
    )
    op.create_index(
        'ix_signal_candidates_search',
        'signal_candidates',
        ['instrument_id', 'timeframe', 'bar_open_ns']
    )

    # 7. scanner_checkpoints table
    op.create_table(
        'scanner_checkpoints',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('spec_version_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('source_timeframe', sa.String(16), server_default='1m', nullable=False),
        sa.Column('last_processed_watermark_ns', sa.BigInteger(), nullable=False),
        sa.Column('last_run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['spec_version_id'], ['scan_spec_versions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['last_run_id'], ['scan_runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('spec_version_id', 'instrument_id', 'source_timeframe', name='uq_scanner_checkpoint')
    )


def downgrade() -> None:
    op.drop_table('scanner_checkpoints')
    op.drop_table('signal_candidates')
    op.drop_table('scan_jobs')
    op.drop_table('scan_runs')
    op.drop_constraint('fk_scan_specs_active_version', 'scan_specs', type_='foreignkey')
    op.drop_table('scan_spec_versions')
    op.drop_table('scan_specs')
    op.drop_table('outbox_events')
