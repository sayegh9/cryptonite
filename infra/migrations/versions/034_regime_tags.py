"""regime tags: market regime at decision time on labelled outcomes (remediation Phase 8, F1)

Revision ID: 034
Revises: 033
Create Date: 2026-10-05 12:00:00.000000

``ai_decision_outcomes.regime`` holds the regime of the decision's instrument when the decision was
taken (``TREND_UP``, ``TREND_DOWN``, ``CHOP``, ``HIGH_VOL``; ``cryptonite_scanner.regime``), from the
1h lake bars up to the decision. The learner writes it with each label; rows labelled before this
migration are filled by ``scripts/backfill_regime_tags.py``. Nullable: too few bars give no regime.
Reporting only (outcomes by regime); nothing reads it for a trade decision. The table's grants are
table-level, so the new column needs no new grants.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "034"
down_revision: str | None = "033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai_decision_outcomes ADD COLUMN regime TEXT")
    op.execute(
        "ALTER TABLE ai_decision_outcomes ADD CONSTRAINT chk_outcome_regime "
        "CHECK (regime IS NULL OR regime IN ('TREND_UP', 'TREND_DOWN', 'CHOP', 'HIGH_VOL'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ai_decision_outcomes DROP CONSTRAINT IF EXISTS chk_outcome_regime")
    op.execute("ALTER TABLE ai_decision_outcomes DROP COLUMN IF EXISTS regime")
