"""The Telegram and TOTP switches readable by the notifier

Revision ID: 069
Revises: 068
Create Date: 2026-11-12 18:00:00.000000

``alerts.telegram_enabled`` (Settings -> Alerts) and ``auth.totp_enabled`` (Settings -> Security) live in
``system_settings``, which the notifier role cannot read. ``notifier_settings`` exposes only those two values to
``cryptonite_notifier``, as 068 did for the agent role.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "069"
down_revision: str | None = "068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE VIEW notifier_settings WITH (security_barrier) AS
        SELECT key, value #>> '{}' AS value_text FROM system_settings
        WHERE key IN ('alerts.telegram_enabled', 'auth.totp_enabled')
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_notifier') THEN
                GRANT SELECT ON notifier_settings TO cryptonite_notifier;
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP VIEW notifier_settings")
