"""backtester role: least privilege for the backtest worker (LE-5)

Revision ID: 030
Revises: 029
Create Date: 2026-10-04 15:00:00.000000

The backtest worker connected as the database owner. ``cryptonite_backtester`` gets only what it uses:
read and update ``backtest_runs`` (claim, lease, status), read and insert ``backtest_results`` and
``outbox_events`` (completion events), read ``strategies`` / ``strategy_versions`` (model-written
code). Everything else, paper trading and mandates included, is out of reach.
"""
import os
from collections.abc import Sequence

from alembic import op

revision: str = "030"
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "cryptonite_backtester"


def upgrade() -> None:
    password = os.getenv("BACKTESTER_DB_PASSWORD", "backtester_dev_pass").replace("'", "''")
    op.execute(f"""
    DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            CREATE ROLE {ROLE} LOGIN PASSWORD '{password}';
        END IF;
    END $$;
    GRANT USAGE ON SCHEMA public TO {ROLE};
    GRANT SELECT, UPDATE ON TABLE backtest_runs TO {ROLE};
    GRANT SELECT, INSERT ON TABLE backtest_results, outbox_events TO {ROLE};
    GRANT SELECT ON TABLE strategies, strategy_versions TO {ROLE};
    GRANT USAGE ON SEQUENCE outbox_events_id_seq TO {ROLE};
    """)


def downgrade() -> None:
    # The role itself is cluster-wide (other databases may still use it), so it is kept.
    op.execute(f"""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
            REVOKE ALL ON TABLE backtest_runs, backtest_results, outbox_events, strategies, strategy_versions FROM {ROLE};
            REVOKE ALL ON SEQUENCE outbox_events_id_seq FROM {ROLE};
        END IF;
    END $$;
    """)
