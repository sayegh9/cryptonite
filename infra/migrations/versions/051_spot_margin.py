"""Paper isolated spot margin accounts and exact hourly-interest ledger.

Revision ID: 051
Revises: 050
Create Date: 2026-10-29 00:00:00.000000

Margin balances and interest are NUMERIC so the paper ledger can reconcile exactly. The
partial unique account index permits historical closed rows while enforcing one open
isolated account for an account/pair. Interest settlements are idempotent by account,
period end and asset.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "051"
down_revision: str | None = "050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERP_ROW = "instrument_id LIKE '%-PERP.%'"
MARGIN_ROW = "instrument_id LIKE '%-MARGIN.%'"


def upgrade() -> None:
    op.create_table(
        "paper_margin_accounts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.String(64), sa.ForeignKey("paper_accounts.account_id"), nullable=False),
        sa.Column("pair", sa.String(32), nullable=False),
        sa.Column("collateral_usdt", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("base_qty", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("quote_qty", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("borrowed_base", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("borrowed_quote", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("interest_base", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("interest_quote", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.CheckConstraint("pair ~ '^[A-Z0-9]{2,28}USDT$'", name="chk_paper_margin_pair_usdt"),
        sa.CheckConstraint("status IN ('OPEN', 'CLOSED', 'LIQUIDATED')", name="chk_paper_margin_status"),
        sa.CheckConstraint(
            "collateral_usdt >= 0 AND base_qty >= 0 AND quote_qty >= 0 AND borrowed_base >= 0 "
            "AND borrowed_quote >= 0 AND interest_base >= 0 AND interest_quote >= 0",
            name="chk_paper_margin_nonnegative",
        ),
    )
    op.create_index(
        "uq_paper_margin_accounts_open",
        "paper_margin_accounts",
        ["account_id", "pair"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_index("ix_paper_margin_accounts_account", "paper_margin_accounts", ["account_id", "updated_at"])

    op.create_table(
        "paper_margin_interest",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("margin_account_id", sa.BigInteger(),
                  sa.ForeignKey("paper_margin_accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset", sa.String(16), nullable=False),
        sa.Column("principal", sa.Numeric(38, 18), nullable=False),
        sa.Column("hourly_rate", sa.Numeric(38, 24), nullable=False),
        sa.Column("amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("period_end > period_start", name="chk_paper_margin_interest_period"),
        sa.CheckConstraint("asset ~ '^[A-Z0-9]{2,15}$'", name="chk_paper_margin_interest_asset"),
        sa.CheckConstraint("principal > 0 AND hourly_rate >= 0 AND amount >= 0",
                           name="chk_paper_margin_interest_nonnegative"),
        sa.UniqueConstraint("margin_account_id", "period_end", "asset",
                            name="uq_paper_margin_interest_period_asset"),
    )
    op.create_index("ix_paper_margin_interest_end", "paper_margin_interest", ["period_end"])

    # SPOT_MARGIN positions share the existing positions table, but only margin instruments
    # may be short or carry isolated leverage. Plain SPOT remains long-only and 1x.
    op.drop_constraint("chk_paper_position_side", "paper_positions", type_="check")
    op.create_check_constraint(
        "chk_paper_position_side", "paper_positions",
        f"side IN ('LONG', 'FLAT') OR (side = 'SHORT' AND ({PERP_ROW} OR {MARGIN_ROW}))",
    )
    op.drop_constraint("chk_paper_position_margin_mode", "paper_positions", type_="check")
    op.create_check_constraint(
        "chk_paper_position_margin_mode", "paper_positions",
        f"margin_mode IS NULL OR (margin_mode IN ('ISOLATED', 'CROSS') AND {PERP_ROW}) "
        f"OR (margin_mode = 'ISOLATED' AND {MARGIN_ROW})",
    )
    op.drop_constraint("chk_paper_position_spot_leverage", "paper_positions", type_="check")
    op.create_check_constraint(
        "chk_paper_position_spot_leverage", "paper_positions",
        f"{PERP_ROW} OR ({MARGIN_ROW} AND leverage BETWEEN 1 AND 10) OR leverage = 1",
    )

    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_executor') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE paper_margin_accounts TO cryptonite_executor;
            GRANT SELECT, INSERT ON TABLE paper_margin_interest TO cryptonite_executor;
            GRANT USAGE, SELECT ON SEQUENCE paper_margin_accounts_id_seq, paper_margin_interest_id_seq
                TO cryptonite_executor;
        END IF;
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT ON TABLE paper_margin_accounts, paper_margin_interest TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_scanner') THEN
            GRANT SELECT ON TABLE ai_mandates, ai_profile_coin_locks, instruments TO cryptonite_scanner;
            GRANT INSERT ON TABLE ai_profile_decisions TO cryptonite_scanner;
            GRANT USAGE, SELECT ON SEQUENCE ai_profile_decisions_id_seq TO cryptonite_scanner;
        END IF;
    END $$
    """)


def downgrade() -> None:
    bind = op.get_bind()
    has_rows = bind.execute(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM paper_margin_accounts
            WHERE status = 'OPEN' OR collateral_usdt <> 0 OR base_qty <> 0 OR quote_qty <> 0
               OR borrowed_base <> 0 OR borrowed_quote <> 0 OR interest_base <> 0 OR interest_quote <> 0
        ) OR EXISTS (
            SELECT 1 FROM paper_positions WHERE instrument_id LIKE '%-MARGIN.%'
        )
    """)).scalar()
    if has_rows:
        raise RuntimeError("Cannot downgrade 051: open margin accounts, margin balances, or margin positions exist")

    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_executor') THEN
            REVOKE SELECT, INSERT, UPDATE ON TABLE paper_margin_accounts FROM cryptonite_executor;
            REVOKE SELECT, INSERT ON TABLE paper_margin_interest FROM cryptonite_executor;
            REVOKE ALL ON SEQUENCE paper_margin_accounts_id_seq, paper_margin_interest_id_seq
                FROM cryptonite_executor;
        END IF;
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            REVOKE SELECT ON TABLE paper_margin_accounts, paper_margin_interest FROM cryptonite_api;
        END IF;
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_scanner') THEN
            REVOKE SELECT ON TABLE ai_mandates, ai_profile_coin_locks, instruments FROM cryptonite_scanner;
            REVOKE INSERT ON TABLE ai_profile_decisions FROM cryptonite_scanner;
            REVOKE USAGE, SELECT ON SEQUENCE ai_profile_decisions_id_seq FROM cryptonite_scanner;
        END IF;
    END $$
    """)
    op.drop_constraint("chk_paper_position_spot_leverage", "paper_positions", type_="check")
    op.create_check_constraint(
        "chk_paper_position_spot_leverage", "paper_positions",
        f"{PERP_ROW} OR leverage = 1",
    )
    op.drop_constraint("chk_paper_position_margin_mode", "paper_positions", type_="check")
    op.create_check_constraint(
        "chk_paper_position_margin_mode", "paper_positions",
        f"margin_mode IS NULL OR (margin_mode IN ('ISOLATED', 'CROSS') AND {PERP_ROW})",
    )
    op.drop_constraint("chk_paper_position_side", "paper_positions", type_="check")
    op.create_check_constraint(
        "chk_paper_position_side", "paper_positions",
        f"side IN ('LONG', 'FLAT') OR (side = 'SHORT' AND {PERP_ROW})",
    )
    op.drop_table("paper_margin_interest")
    op.drop_index("ix_paper_margin_accounts_account", table_name="paper_margin_accounts")
    op.drop_index("uq_paper_margin_accounts_open", table_name="paper_margin_accounts")
    op.drop_table("paper_margin_accounts")
