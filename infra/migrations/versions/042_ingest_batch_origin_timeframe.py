"""ingest_batches origin + timeframe columns

Track the origin of each ingest batch (live stream vs historical backfill) and its real timeframe so
that ``reconcile_pending_batches`` can replay pre-checkpoint history batches through the history path
(checkpoint guard bypass, no outbox, LEAST/GREATEST widening) with the correct timeframe / bar type.

Revision ID: 042
Revises: 041
Create Date: 2026-10-09 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "042"
down_revision: str | None = "041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingest_batches",
        sa.Column("timeframe", sa.String(16), nullable=False, server_default="1m"),
    )
    op.add_column(
        "ingest_batches",
        sa.Column("origin", sa.String(16), nullable=False, server_default="live"),
    )
    op.create_check_constraint(
        "ck_ingest_batches_origin",
        "ingest_batches",
        "origin IN ('live', 'history')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ingest_batches_origin", "ingest_batches", type_="check")
    op.drop_column("ingest_batches", "origin")
    op.drop_column("ingest_batches", "timeframe")