"""add version to risk limit overrides

Revision ID: 041
Revises: 040
Create Date: 2026-10-07 00:00:00.000000
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "041"
down_revision: str | None = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "risk_limit_overrides",
        sa.Column("override_version", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.alter_column("risk_limit_overrides", "override_version", server_default=None)
    op.create_index(
        "ix_risk_limit_overrides_version",
        "risk_limit_overrides",
        ["environment", "account_id", "override_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_risk_limit_overrides_version", table_name="risk_limit_overrides")
    op.drop_column("risk_limit_overrides", "override_version")
