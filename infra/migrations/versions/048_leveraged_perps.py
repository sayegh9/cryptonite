"""ai_mandates: leveraged perpetuals (ADR 0009, leverage plan Stage 2.1)

Revision ID: 048
Revises: 047
Create Date: 2026-10-20 00:00:00.000000

- ``chk_ai_mandates_leverage``: ``max_leverage`` 1..10 (was 1..5; D2).
- New ``max_risk_per_trade_pct``: the loss at the stop one trade may risk, % of the budget; default 1, at most 2 (D6).
- ``chk_ai_mandates_perps_l3`` (perps only at L3) becomes ``chk_ai_mandates_perps_level``: perps only at L1, where
  every risk-adding order waits for the operator's TOTP approval (D3; L2/L3 autonomous perps are out of scope).

The upgrade refuses while a mandate has perps enabled away from L1 (it would break the new CHECK; it is not
silently switched off). The downgrade refuses while any mandate has ``max_leverage`` above 5 or perps enabled
below L3, since the old CHECKs would reject them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "048"
down_revision: str | None = "047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM ai_mandates WHERE allow_perps AND autonomy_level <> 1) THEN
            RAISE EXCEPTION 'a mandate has perps enabled away from L1; disable allow_perps on it before migrating (ADR 0009 D3)';
        END IF; END $$""")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_leverage")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_leverage CHECK (max_leverage >= 1 AND max_leverage <= 10)")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_perps_l3")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_perps_level CHECK (NOT allow_perps OR autonomy_level = 1)")
    op.execute("ALTER TABLE ai_mandates ADD COLUMN max_risk_per_trade_pct NUMERIC(5, 2) NOT NULL DEFAULT 1")
    op.execute("""ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_risk_per_trade
                  CHECK (max_risk_per_trade_pct > 0 AND max_risk_per_trade_pct <= 2)""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM ai_mandates WHERE max_leverage > 5) THEN
            RAISE EXCEPTION 'a mandate has max_leverage above 5; lower it before downgrading below 048';
        END IF;
        IF EXISTS (SELECT 1 FROM ai_mandates WHERE allow_perps AND autonomy_level <> 3) THEN
            RAISE EXCEPTION 'a mandate has perps enabled below L3; disable allow_perps before downgrading below 048';
        END IF; END $$""")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_risk_per_trade")
    op.execute("ALTER TABLE ai_mandates DROP COLUMN max_risk_per_trade_pct")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_perps_level")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_perps_l3 CHECK (NOT allow_perps OR autonomy_level = 3)")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_leverage")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_leverage CHECK (max_leverage >= 1 AND max_leverage <= 5)")
