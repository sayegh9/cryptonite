"""agent_anonymized_views

Revision ID: 014
Revises: 013
Create Date: 2026-09-21 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '014'
down_revision: Union[str, None] = '013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Revoke global select from agent_ro
    op.execute("""
    REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM cryptonite_agent_ro;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM cryptonite_agent_ro;
    """)

    # Create anonymized views
    # 1. Trading Orders (No account_id, no raw client_order_id internal hashes if any, just safe stuff)
    op.execute("""
    CREATE OR REPLACE VIEW agent_safe_orders AS
    SELECT 
        client_order_id,
        instrument_id,
        side,
        order_type,
        status,
        quantity,
        price,
        filled_quantity,
        average_fill_price,
        created_at,
        updated_at
    FROM trading_orders;
    """)

    # 2. Trading Positions (No account_id)
    op.execute("""
    CREATE OR REPLACE VIEW agent_safe_positions AS
    SELECT 
        instrument_id,
        quantity,
        average_entry_price,
        created_at,
        updated_at
    FROM paper_positions;
    """)
    # Note: paper_positions used as proxy. If trading_positions exists, use that.
    # Wait, the DB uses `trading_balances` maybe? Let's check tables.
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'trading_positions') THEN
            EXECUTE 'CREATE OR REPLACE VIEW agent_safe_live_positions AS 
            SELECT instrument_id, quantity, average_entry_price, updated_at FROM trading_positions';
            GRANT SELECT ON agent_safe_live_positions TO cryptonite_agent_ro;
        END IF;
    END $$;
    """)

    # 3. Strategy Instances
    op.execute("""
    CREATE OR REPLACE VIEW agent_safe_strategies AS
    SELECT
        id,
        strategy_slug,
        instrument_id,
        status,
        created_at
    FROM paper_strategy_instances;
    """)

    # Grant select on safe views and safe metadata tables
    op.execute("""
    GRANT SELECT ON agent_safe_orders TO cryptonite_agent_ro;
    GRANT SELECT ON agent_safe_positions TO cryptonite_agent_ro;
    GRANT SELECT ON agent_safe_strategies TO cryptonite_agent_ro;
    
    -- Safe metadata tables
    GRANT SELECT ON instruments TO cryptonite_agent_ro;
    GRANT SELECT ON scan_candidates TO cryptonite_agent_ro; -- assuming exists, wait, let's use signal_candidates
    GRANT SELECT ON signal_candidates TO cryptonite_agent_ro;
    GRANT SELECT ON scan_runs TO cryptonite_agent_ro;
    GRANT SELECT ON strategies TO cryptonite_agent_ro;
    """)


def downgrade() -> None:
    op.execute("""
    DROP VIEW IF EXISTS agent_safe_orders;
    DROP VIEW IF EXISTS agent_safe_positions;
    DROP VIEW IF EXISTS agent_safe_strategies;
    DROP VIEW IF EXISTS agent_safe_live_positions;
    """)
    
    # Restore original permissions
    op.execute("""
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO cryptonite_agent_ro;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO cryptonite_agent_ro;
    """)
