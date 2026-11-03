"""H11: live-parity candidate labels (net_r_live) next to the 057 net labels

Revision ID: 062
Revises: 061
Create Date: 2026-11-03 16:00:00.000000

``net_r_profile`` (057) replays a 5% hard stop and a +3% / 2% trail only, so on a tight structural stop its losses
run to several R. ``net_r_live`` is the exit monitor's own rules replayed bar by bar
(``cryptonite_schemas.exit_rules.simulate_live_exit``): structural stop, TP1 partial, profile trail and max hold, the
hard stop as backstop, net of cost model v1. ``live_outcome`` is the closing rule, ``live_bars`` the 1m bars to the
last fill, ``live_exit_model`` the exits used (NULL = not labelled yet; the learner backfills oldest first). The
055 / 057 columns are not rewritten. ``agent_safe_candidate_outcomes`` does not exist; the table grants (055) cover
the new columns.

Follows H10's 061 (debate evidence snapshots); deploy 061 first.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "062"
down_revision: str | None = "061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE candidate_outcomes
            ADD COLUMN IF NOT EXISTS net_r_live NUMERIC,
            ADD COLUMN IF NOT EXISTS live_outcome TEXT,
            ADD COLUMN IF NOT EXISTS live_bars INTEGER,
            ADD COLUMN IF NOT EXISTS live_exit_model TEXT
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE candidate_outcomes
            DROP COLUMN IF EXISTS net_r_live,
            DROP COLUMN IF EXISTS live_outcome,
            DROP COLUMN IF EXISTS live_bars,
            DROP COLUMN IF EXISTS live_exit_model
    """)
