"""perpetuals schema: margin columns on paper_positions, funding_rates history

Revision ID: 020
Revises: 019
Create Date: 2026-09-24 15:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '020'
down_revision: str | None = '019'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERP_ROW = "instrument_id LIKE '%-PERP.%'"


def upgrade() -> None:
    # 1. paper_positions: SHORT side for perps; quantity stays absolute (>= 0), side carries direction
    op.drop_constraint('chk_paper_position_side', 'paper_positions', type_='check')
    op.create_check_constraint(
        'chk_paper_position_side',
        'paper_positions',
        f"side IN ('LONG', 'FLAT') OR (side = 'SHORT' AND {PERP_ROW})",
    )

    # 2. Margin columns. liquidation_price / maintenance_margin are snapshot values
    #    written with each position update; the engine recomputes them from mark price.
    op.add_column('paper_positions', sa.Column('leverage', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('paper_positions', sa.Column('margin_mode', sa.String(16), nullable=True))
    op.add_column('paper_positions', sa.Column('isolated_margin', sa.Numeric(38, 18), nullable=False, server_default='0'))
    op.add_column('paper_positions', sa.Column('liquidation_price', sa.Numeric(38, 18), nullable=True))
    op.add_column('paper_positions', sa.Column('maintenance_margin', sa.Numeric(38, 18), nullable=True))

    # Existing perp rows (long-only until now) default to CROSS, the Binance default
    op.execute(f"UPDATE paper_positions SET margin_mode = 'CROSS' WHERE {PERP_ROW}")

    op.create_check_constraint('chk_paper_position_leverage', 'paper_positions', 'leverage BETWEEN 1 AND 125')
    # Spot rows never carry a margin mode. Perp rows may still be NULL because the
    # EventBridgeWorker position insert does not write margin_mode yet; tighten to
    # NOT NULL for perps once perp fill accounting (roadmap C9) populates it.
    op.create_check_constraint(
        'chk_paper_position_margin_mode',
        'paper_positions',
        f"margin_mode IS NULL OR (margin_mode IN ('ISOLATED', 'CROSS') AND {PERP_ROW})",
    )
    op.create_check_constraint(
        'chk_paper_position_isolated_margin',
        'paper_positions',
        "isolated_margin >= 0 AND (margin_mode = 'ISOLATED' OR isolated_margin = 0)",
    )
    op.create_check_constraint(
        'chk_paper_position_spot_leverage',
        'paper_positions',
        f"{PERP_ROW} OR leverage = 1",
    )

    # 3. funding_rates: settled funding history (1h/4h/8h intervals), market-level
    op.create_table(
        'funding_rates',
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('funding_time_ns', sa.BigInteger(), nullable=False),
        sa.Column('funding_rate', sa.Numeric(38, 18), nullable=False),
        sa.Column('mark_price', sa.Numeric(38, 18), nullable=True),
        sa.Column('funding_interval_hours', sa.SmallInteger(), nullable=True),
        sa.Column('source', sa.String(32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        # PK doubles as the idempotency key: a replayed settlement is a no-op via ON CONFLICT DO NOTHING
        sa.PrimaryKeyConstraint('instrument_id', 'funding_time_ns', name='pk_funding_rates'),
        sa.CheckConstraint(PERP_ROW, name='chk_funding_rate_perp_only'),
        sa.CheckConstraint("funding_interval_hours IN (1, 2, 4, 8)", name='chk_funding_interval_hours'),
        sa.CheckConstraint("source IN ('BINANCE_REST', 'STREAM_SETTLEMENT')", name='chk_funding_rate_source'),
    )
    op.create_index('ix_funding_rates_funding_time_ns', 'funding_rates', ['funding_time_ns'])

    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_writer') THEN
            GRANT SELECT, INSERT ON TABLE funding_rates TO cryptonite_writer;
        END IF;
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT ON TABLE funding_rates TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_scanner') THEN
            GRANT SELECT ON TABLE funding_rates TO cryptonite_scanner;
        END IF;
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            GRANT SELECT ON TABLE funding_rates TO cryptonite_agent_ro;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.drop_index('ix_funding_rates_funding_time_ns', table_name='funding_rates')
    op.drop_table('funding_rates')

    # Shorts cannot be represented after downgrade; refuse rather than silently rewriting them
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM paper_positions WHERE side = 'SHORT') THEN
            RAISE EXCEPTION 'Cannot downgrade 020: paper_positions contains SHORT rows';
        END IF;
    END $$;
    """)

    for name in (
        'chk_paper_position_spot_leverage',
        'chk_paper_position_isolated_margin',
        'chk_paper_position_margin_mode',
        'chk_paper_position_leverage',
    ):
        op.drop_constraint(name, 'paper_positions', type_='check')
    for col in ('maintenance_margin', 'liquidation_price', 'isolated_margin', 'margin_mode', 'leverage'):
        op.drop_column('paper_positions', col)

    op.drop_constraint('chk_paper_position_side', 'paper_positions', type_='check')
    op.create_check_constraint('chk_paper_position_side', 'paper_positions', "side IN ('LONG', 'FLAT')")
