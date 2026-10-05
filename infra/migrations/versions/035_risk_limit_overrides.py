"""risk limit overrides: operator-configurable, tighten-only risk limits (remediation Phase 8, F7)

Revision ID: 035
Revises: 034
Create Date: 2026-10-05 18:00:00.000000

One row per (environment, account_id). Each limit is in percent and nullable: NULL means no override (the
code default applies). The CHECKs repeat the invariants (``cryptonite_execution.risk_limits``): a value is
never above its code default (5% daily loss, 25% order clamp, 25% cross-mandate cap) and the order cap is
never below the 1% minimum order. The 20% account backstop is not configurable at all. Which change needs
the operator's TOTP (loosening) is decided by the API before it writes.

Grants (least privilege, as in 025/026): the API role reads and writes the row; the orchestrator reads it
(it connects as the owner today; ``cryptonite_executor`` gets SELECT for the day it moves to that role);
the agents' read-only role gets nothing.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "035"
down_revision: str | None = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE risk_limit_overrides (
        environment VARCHAR(16) NOT NULL,
        account_id VARCHAR(64) NOT NULL,
        max_daily_loss_pct NUMERIC(8, 4),
        max_order_equity_pct NUMERIC(8, 4),
        cross_mandate_exposure_pct NUMERIC(8, 4),
        updated_by VARCHAR(128) NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        reason TEXT NOT NULL,
        PRIMARY KEY (environment, account_id),
        CONSTRAINT chk_rlo_daily_loss CHECK (max_daily_loss_pct IS NULL OR (max_daily_loss_pct > 0 AND max_daily_loss_pct <= 5)),
        CONSTRAINT chk_rlo_order_equity CHECK (max_order_equity_pct IS NULL OR (max_order_equity_pct >= 1 AND max_order_equity_pct <= 25)),
        CONSTRAINT chk_rlo_cross_mandate CHECK (cross_mandate_exposure_pct IS NULL OR (cross_mandate_exposure_pct > 0 AND cross_mandate_exposure_pct <= 25)),
        CONSTRAINT chk_rlo_reason CHECK (length(reason) >= 5)
    )
    """)
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE risk_limit_overrides TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_executor') THEN
            GRANT SELECT ON TABLE risk_limit_overrides TO cryptonite_executor;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            REVOKE ALL ON TABLE risk_limit_overrides FROM cryptonite_agent_ro;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS risk_limit_overrides")
