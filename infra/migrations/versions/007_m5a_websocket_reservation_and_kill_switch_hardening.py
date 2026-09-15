"""007_m5a_websocket_reservation_and_kill_switch_hardening

Revision ID: 007
Revises: 006
Create Date: 2026-09-09 17:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '007'
down_revision: Union[str, None] = '006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. execution_kill_switch hardening ---
    # Step 1: Drop legacy single-row constraint if present
    op.execute("ALTER TABLE execution_kill_switch DROP CONSTRAINT IF EXISTS chk_single_kill_switch_row;")

    # Step 2: Add nullable columns
    op.add_column('execution_kill_switch', sa.Column('environment', sa.String(32), nullable=True))
    op.add_column('execution_kill_switch', sa.Column('account_id', sa.String(64), nullable=True))
    op.add_column('execution_kill_switch', sa.Column('latch_generation', sa.BigInteger(), nullable=True))

    # Step 3: Backfill the existing row with defined Demo environment/logical account
    op.execute("""
    UPDATE execution_kill_switch
    SET environment = COALESCE(environment, 'DEMO'),
        account_id = COALESCE(account_id, 'DEMO-ACCOUNT-001'),
        latch_generation = COALESCE(latch_generation, CASE WHEN is_latched THEN 1 ELSE 0 END)
    WHERE environment IS NULL OR account_id IS NULL OR latch_generation IS NULL;
    """)

    # Step 4: Set environment, account_id, and latch_generation to NOT NULL with defaults
    op.alter_column('execution_kill_switch', 'environment', nullable=False, server_default='DEMO')
    op.alter_column('execution_kill_switch', 'account_id', nullable=False, server_default='DEMO-ACCOUNT-001')
    op.alter_column('execution_kill_switch', 'latch_generation', nullable=False, server_default='0')

    # Step 5: Ensure id column uses a sequence instead of defaulting to 1
    op.execute("""
    CREATE SEQUENCE IF NOT EXISTS execution_kill_switch_id_seq;
    SELECT setval('execution_kill_switch_id_seq', (SELECT COALESCE(MAX(id), 1) FROM execution_kill_switch));
    ALTER TABLE execution_kill_switch ALTER COLUMN id SET DEFAULT nextval('execution_kill_switch_id_seq');
    """)

    # Step 6: Restore from execution_kill_switch_archive if it exists (from prior downgrade)
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'execution_kill_switch_archive') THEN
            INSERT INTO execution_kill_switch (
                environment, account_id, is_latched, latched_at, latched_by, reason, latch_generation, updated_at
            )
            SELECT environment, account_id, is_latched, latched_at, latched_by, reason, latch_generation, updated_at
            FROM execution_kill_switch_archive
            ON CONFLICT DO NOTHING;
            DROP TABLE execution_kill_switch_archive;
        END IF;
    END $$;
    """)

    # Step 7: Ensure unique constraint on (environment, account_id)
    op.create_unique_constraint('uq_kill_switch_env_account', 'execution_kill_switch', ['environment', 'account_id'])

    # --- 2. trip_execution_kill_switch SECURITY DEFINER function ---
    # Schema-qualified, safe search_path, atomic audit & outbox events, only permits false -> true
    op.execute("""
    CREATE OR REPLACE FUNCTION public.trip_execution_kill_switch(
        p_environment text,
        p_account_id text,
        p_reason text,
        p_actor_id text
    ) RETURNS bigint
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
    DECLARE
        new_gen bigint;
        audit_id uuid;
        outbox_id uuid;
        event_payload jsonb;
    BEGIN
        -- 1. Atomic Latch with Compare-And-Swap/Strict Progression:
        -- Increment generation unconditionally on every trip.
        INSERT INTO public.execution_kill_switch (
            environment, account_id, is_latched, latched_at, latched_by, reason, latch_generation, updated_at
        ) VALUES (
            p_environment, p_account_id, true, now(), p_actor_id, p_reason, 1, now()
        )
        ON CONFLICT (environment, account_id) DO UPDATE
        SET is_latched = true,
            latched_at = now(),
            latched_by = p_actor_id,
            reason = p_reason,
            latch_generation = public.execution_kill_switch.latch_generation + 1,
            updated_at = now()
        RETURNING latch_generation INTO new_gen;

        -- 2. Insert audit event into public.auth_audit_events atomically
        audit_id := gen_random_uuid();
        INSERT INTO public.auth_audit_events (
            event_id, username, action, ip_address, user_agent, details, occurred_at
        ) VALUES (
            audit_id,
            p_actor_id,
            'KILL_SWITCH_TRIPPED',
            '127.0.0.1',
            'trip_execution_kill_switch',
            jsonb_build_object(
                'environment', p_environment,
                'account_id', p_account_id,
                'reason', p_reason,
                'latch_generation', new_gen
            ),
            now()
        );

        -- 3. Insert outbox event into public.outbox_events atomically
        outbox_id := gen_random_uuid();
        event_payload := jsonb_build_object(
            'event_id', outbox_id,
            'event_type', 'KILL_SWITCH_TRIPPED',
            'environment', p_environment,
            'account_id', p_account_id,
            'latch_generation', new_gen,
            'latched_by', p_actor_id,
            'reason', p_reason,
            'timestamp_ns', CAST(EXTRACT(EPOCH FROM now()) * 1000000000 AS bigint)
        );

        INSERT INTO public.outbox_events (
            event_id, stream_name, event_type, producer, payload, status, created_at
        ) VALUES (
            outbox_id::text,
            'cryptonite:events:control',
            'KILL_SWITCH_TRIPPED',
            'postgres_security_definer',
            event_payload,
            'PENDING',
            now()
        );

        RETURN new_gen;
    END;
    $$;
    REVOKE EXECUTE ON FUNCTION public.trip_execution_kill_switch(text, text, text, text) FROM PUBLIC;
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
    """)

    # --- 3. Role Permissions on Kill Switch ---
    op.execute("""
    DO $$
    BEGIN
        -- Revoke all on function from public, grant to API and executor
        REVOKE ALL ON FUNCTION public.trip_execution_kill_switch(text, text, text, text) FROM PUBLIC;
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT EXECUTE ON FUNCTION public.trip_execution_kill_switch(text, text, text, text) TO cryptonite_api;
            REVOKE UPDATE, INSERT, DELETE ON TABLE execution_kill_switch FROM cryptonite_api;
            GRANT SELECT ON TABLE execution_kill_switch TO cryptonite_api;
        END IF;

        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_executor') THEN
            GRANT EXECUTE ON FUNCTION public.trip_execution_kill_switch(text, text, text, text) TO cryptonite_executor;
            GRANT SELECT, INSERT, UPDATE ON TABLE execution_kill_switch TO cryptonite_executor;
            REVOKE DELETE ON TABLE execution_kill_switch FROM cryptonite_executor;
            GRANT USAGE, SELECT ON SEQUENCE execution_kill_switch_id_seq TO cryptonite_executor;
        END IF;
    END $$;
    """)

    # --- 4. execution_commands status constraint hardening (include SENDING) ---
    op.execute("""
    ALTER TABLE execution_commands DROP CONSTRAINT IF EXISTS chk_execution_commands_status;
    ALTER TABLE execution_commands ADD CONSTRAINT chk_execution_commands_status
    CHECK (status IN ('PENDING', 'CLAIMED', 'RUNNING', 'SENDING', 'COMPLETED', 'REJECTED', 'FAILED'));
    """)

    # Add socket_write_started flag for crash recovery ambiguity resolution
    op.add_column('execution_commands', sa.Column('socket_write_started', sa.Boolean(), server_default='false', nullable=False))

    # --- 5. execution_reservations provenance hardening ---
    # Drop old check constraint first before any updates or deferred triggers
    op.execute("ALTER TABLE execution_reservations DROP CONSTRAINT IF EXISTS chk_reservations_venue_reflected_consistency;")

    # Add release_reason and settlement_snapshot_id
    op.add_column('execution_reservations', sa.Column('release_reason', sa.String(32), nullable=True))
    op.add_column(
        'execution_reservations',
        sa.Column(
            'settlement_snapshot_id',
            postgresql.UUID(as_uuid=True),
            nullable=True,
        )
    )
    op.create_foreign_key(
        'fk_reservations_settlement_snapshot',
        'execution_reservations',
        'trading_balance_snapshots',
        ['settlement_snapshot_id'],
        ['snapshot_id'],
        ondelete='RESTRICT',
    )

    # Backfill existing rows
    op.execute("""
    UPDATE execution_reservations
    SET settlement_snapshot_id = reflected_snapshot_id
    WHERE state = 'RELEASED' AND venue_reflected = true AND settlement_snapshot_id IS NULL AND reflected_snapshot_id IS NOT NULL;
    """)
    op.execute("""
    UPDATE execution_reservations
    SET release_reason = CASE
        WHEN venue_reflected THEN 'VENUE_FILLED_SETTLED'
        ELSE 'PRE_SEND_REJECTED'
    END
    WHERE state = 'RELEASED' AND release_reason IS NULL;
    """)

    # Flush pending deferred trigger events so subsequent DDL succeeds
    op.execute("SET CONSTRAINTS ALL IMMEDIATE;")

    # Add hardened check constraint
    op.create_check_constraint(
        'chk_reservations_release_and_settlement_consistency',
        'execution_reservations',
        """
        (
            state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED')
            AND venue_reflected = FALSE
            AND release_reason IS NULL
            AND settlement_snapshot_id IS NULL
        )
        OR
        (
            state IN ('VENUE_REFLECTED', 'SETTLEMENT_PENDING')
            AND venue_reflected = TRUE
            AND release_reason IS NULL
            AND settlement_snapshot_id IS NULL
        )
        OR
        (
            state = 'RELEASED'
            AND release_reason IS NOT NULL
            AND (
                (
                    release_reason IN ('VENUE_FILLED_SETTLED', 'VENUE_CANCELLED_SETTLED')
                    AND venue_reflected = TRUE
                    AND settlement_snapshot_id IS NOT NULL
                )
                OR
                (
                    release_reason IN ('PRE_SEND_REJECTED', 'CANCELLED_UNSENT', 'OPERATOR_ABORTED_UNSENT')
                    AND venue_reflected = FALSE
                    AND settlement_snapshot_id IS NULL
                )
            )
        )
        """
    )


def downgrade() -> None:
    # 1. execution_reservations downgrade
    op.execute("ALTER TABLE execution_reservations DROP CONSTRAINT IF EXISTS chk_reservations_release_and_settlement_consistency;")
    op.drop_constraint('fk_reservations_settlement_snapshot', 'execution_reservations', type_='foreignkey')
    op.drop_column('execution_reservations', 'settlement_snapshot_id')
    op.drop_column('execution_reservations', 'release_reason')

    op.create_check_constraint(
        'chk_reservations_venue_reflected_consistency',
        'execution_reservations',
        """
        (venue_reflected = FALSE AND state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED', 'RELEASED')) OR 
        (venue_reflected = TRUE AND state IN ('VENUE_REFLECTED', 'SETTLEMENT_PENDING', 'RELEASED'))
        """
    )

    # 2. execution_commands status constraint downgrade
    op.execute("ALTER TABLE execution_commands DROP CONSTRAINT IF EXISTS chk_execution_commands_status;")
    op.drop_column('execution_commands', 'socket_write_started')

    # 3. Guard against destroying account-specific kill switch history
    bind = op.get_bind()
    cnt = bind.execute(sa.text("SELECT count(*) FROM execution_kill_switch;")).scalar()
    if cnt and cnt > 1:
        raise RuntimeError(
            f"Cannot downgrade migration 007: {cnt} account-scoped rows exist in execution_kill_switch. "
            "Downgrading to the legacy single-row model would destroy account-specific kill switch history. "
            "Please archive or remove extraneous accounts before downgrading."
        )

    op.execute("UPDATE execution_kill_switch SET id = 1 WHERE id != 1;")

    # 4. Kill switch function and columns downgrade
    op.execute("DROP FUNCTION IF EXISTS public.trip_execution_kill_switch(text, text, text, text);")
    op.drop_constraint('uq_kill_switch_env_account', 'execution_kill_switch', type_='unique')
    op.drop_column('execution_kill_switch', 'latch_generation')
    op.drop_column('execution_kill_switch', 'account_id')
    op.drop_column('execution_kill_switch', 'environment')

    op.execute("""
    ALTER TABLE execution_kill_switch ALTER COLUMN id SET DEFAULT 1;
    DROP SEQUENCE IF EXISTS execution_kill_switch_id_seq;
    """)
    op.create_check_constraint('chk_single_kill_switch_row', 'execution_kill_switch', 'id = 1')
