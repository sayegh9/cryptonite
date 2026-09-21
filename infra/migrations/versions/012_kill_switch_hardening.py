"""kill switch hardening

Revision ID: 012
Revises: 011
Create Date: 2026-09-19 15:30:00.000000

"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = '012'
down_revision: str | None = '011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add columns to execution_commands if they do not exist
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='execution_commands' AND column_name='account_id') THEN
            ALTER TABLE execution_commands ADD COLUMN account_id VARCHAR(64);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='execution_commands' AND column_name='environment') THEN
            ALTER TABLE execution_commands ADD COLUMN environment VARCHAR(32);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='execution_commands' AND column_name='generation') THEN
            ALTER TABLE execution_commands ADD COLUMN generation BIGINT;
        END IF;
    END $$;
    """)

    # Backfill or set default?
    op.execute("UPDATE execution_commands SET account_id = 'DEMO-ACCOUNT-001', environment = 'DEMO' WHERE account_id IS NULL;")
    op.alter_column('execution_commands', 'account_id', nullable=False, server_default='DEMO-ACCOUNT-001')
    op.alter_column('execution_commands', 'environment', nullable=False, server_default='DEMO')

    # Unique constraints
    op.create_unique_constraint(
        'uq_exec_cmd_acc_env_idem',
        'execution_commands',
        ['account_id', 'environment', 'idempotency_key']
    )
    
    op.create_unique_constraint(
        'uq_exec_cmd_acc_env_gen',
        'execution_commands',
        ['account_id', 'environment', 'generation']
    )

    # Make sure postgres_security_definer exists
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'postgres_security_definer') THEN
            CREATE ROLE postgres_security_definer NOLOGIN;
        END IF;
    END $$;
    """)

    # Create the trip_kill_switch function
    op.execute("""
    CREATE OR REPLACE FUNCTION public.trip_kill_switch(
        p_account text,
        p_env text,
        p_reason text,
        p_ip text,
        p_user text
    ) RETURNS bigint
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $func$
    DECLARE
        v_new_gen bigint;
    BEGIN
        -- 1. Lock the row to prevent concurrent trips
        SELECT latch_generation + 1 INTO v_new_gen
        FROM public.execution_kill_switch
        WHERE account_id = p_account AND environment = p_env
        FOR UPDATE;

        IF v_new_gen IS NULL THEN
            v_new_gen := 1;
            INSERT INTO public.execution_kill_switch (
                account_id, environment, is_latched, latch_generation, latched_at, latched_by, reason, updated_at
            ) VALUES (
                p_account, p_env, true, v_new_gen, now(), p_user, p_reason, now()
            );
        ELSE
            UPDATE public.execution_kill_switch
            SET is_latched = true,
                latch_generation = v_new_gen,
                latched_at = now(),
                latched_by = p_user,
                reason = p_reason,
                updated_at = now()
            WHERE account_id = p_account AND environment = p_env;
        END IF;

        -- 2. Enqueue TRIP_KILL_SWITCH command
        INSERT INTO public.execution_commands (
            account_id, environment, idempotency_key, command_type, payload, status, requester_id, generation
        ) VALUES (
            p_account, p_env, v_new_gen::text, 'TRIP_KILL_SWITCH',
            jsonb_build_object('reason', p_reason, 'ip', p_ip, 'user', p_user),
            'PENDING', p_user, v_new_gen
        );

        RETURN v_new_gen;
    END;
    $func$;

    ALTER FUNCTION public.trip_kill_switch(text, text, text, text, text) OWNER TO postgres_security_definer;
    REVOKE ALL ON FUNCTION public.trip_kill_switch(text, text, text, text, text) FROM PUBLIC;
    """)
    
    # Also create a view for cryptonite_kill_switch_state as an alias just in case
    op.execute("""
    CREATE OR REPLACE VIEW public.cryptonite_kill_switch_state AS SELECT * FROM public.execution_kill_switch;
    """)

def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS public.cryptonite_kill_switch_state;")
    op.execute("DROP FUNCTION IF EXISTS public.trip_kill_switch(text, text, text, text, text);")
    op.drop_constraint('uq_exec_cmd_acc_env_gen', 'execution_commands', type_='unique')
    op.drop_constraint('uq_exec_cmd_acc_env_idem', 'execution_commands', type_='unique')
    op.drop_column('execution_commands', 'generation')
    op.drop_column('execution_commands', 'environment')
    op.drop_column('execution_commands', 'account_id')
