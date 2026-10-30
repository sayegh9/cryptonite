"""Grant the debate worker its missing exit-decision privileges.

Revision ID: 052
Revises: 051
Create Date: 2026-10-30 00:00:00.000000

The debate worker connects as ``cryptonite_api``.  Migration 019 created
``exit_decisions`` without granting that role access.  Save the role's prior
effective privileges so downgrade removes only privileges this revision added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "052"
down_revision: str | None = "051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE_TABLE = "h1_052_exit_decisions_grant_state"


def upgrade() -> None:
    # PostgreSQL ACLs do not record which migration first granted a privilege.
    # Capture the pre-migration state to make downgrade safe when a privilege
    # was already available through an earlier direct or inherited grant.
    op.create_table(
        _STATE_TABLE,
        sa.Column("role_name", sa.String(63), primary_key=True),
        sa.Column("had_select", sa.Boolean(), nullable=False),
        sa.Column("had_insert", sa.Boolean(), nullable=False),
        sa.Column("had_update", sa.Boolean(), nullable=False),
        sa.Column("sequence_name", sa.Text(), nullable=True),
        sa.Column("had_sequence_usage", sa.Boolean(), nullable=True),
        sa.Column("had_sequence_select", sa.Boolean(), nullable=True),
    )

    op.execute(f"""
    DO $$
    DECLARE
        role_name text := 'cryptonite_api';
        seq_name text;
        had_select boolean;
        had_insert boolean;
        had_update boolean;
        had_sequence_usage boolean;
        had_sequence_select boolean;
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            had_select := has_table_privilege(role_name, 'public.exit_decisions', 'SELECT');
            had_insert := has_table_privilege(role_name, 'public.exit_decisions', 'INSERT');
            had_update := has_table_privilege(role_name, 'public.exit_decisions', 'UPDATE');
            seq_name := pg_get_serial_sequence('public.exit_decisions', 'id');

            IF seq_name IS NOT NULL THEN
                had_sequence_usage := has_sequence_privilege(role_name, seq_name, 'USAGE');
                had_sequence_select := has_sequence_privilege(role_name, seq_name, 'SELECT');
            END IF;

            INSERT INTO public.{_STATE_TABLE}
                (role_name, had_select, had_insert, had_update, sequence_name,
                 had_sequence_usage, had_sequence_select)
            VALUES
                (role_name, had_select, had_insert, had_update, seq_name,
                 had_sequence_usage, had_sequence_select);

            IF NOT had_select THEN
                GRANT SELECT ON TABLE public.exit_decisions TO cryptonite_api;
            END IF;
            IF NOT had_insert THEN
                GRANT INSERT ON TABLE public.exit_decisions TO cryptonite_api;
            END IF;
            IF NOT had_update THEN
                GRANT UPDATE ON TABLE public.exit_decisions TO cryptonite_api;
            END IF;
            IF seq_name IS NOT NULL THEN
                IF NOT had_sequence_usage THEN
                    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO cryptonite_api', seq_name);
                END IF;
                IF NOT had_sequence_select THEN
                    EXECUTE format('GRANT SELECT ON SEQUENCE %s TO cryptonite_api', seq_name);
                END IF;
            END IF;
        END IF;
    END $$
    """)


def downgrade() -> None:
    # Revoke only the privileges that were absent before this migration.  If
    # the role was absent during upgrade, the snapshot is empty and no revoke
    # occurs even if the role has since been created.
    op.execute(f"""
    DO $$
    DECLARE
        state record;
    BEGIN
        SELECT * INTO state FROM public.{_STATE_TABLE}
        WHERE role_name = 'cryptonite_api';

        IF FOUND AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = state.role_name) THEN
            IF NOT state.had_select THEN
                REVOKE SELECT ON TABLE public.exit_decisions FROM cryptonite_api;
            END IF;
            IF NOT state.had_insert THEN
                REVOKE INSERT ON TABLE public.exit_decisions FROM cryptonite_api;
            END IF;
            IF NOT state.had_update THEN
                REVOKE UPDATE ON TABLE public.exit_decisions FROM cryptonite_api;
            END IF;
            IF state.sequence_name IS NOT NULL THEN
                IF NOT state.had_sequence_usage THEN
                    EXECUTE format('REVOKE USAGE ON SEQUENCE %s FROM cryptonite_api', state.sequence_name);
                END IF;
                IF NOT state.had_sequence_select THEN
                    EXECUTE format('REVOKE SELECT ON SEQUENCE %s FROM cryptonite_api', state.sequence_name);
                END IF;
            END IF;
        END IF;
    END $$
    """)
    op.drop_table(_STATE_TABLE)
