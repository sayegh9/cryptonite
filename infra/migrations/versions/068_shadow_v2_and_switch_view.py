"""H16: a second shadow prompt per setup, and the master switch readable by the Brain

Revision ID: 068
Revises: 067
Create Date: 2026-11-06 18:00:00.000000

``shadow_debate_verdicts`` held one row per candidate, so a second prompt version could not answer the same setup. The key
becomes (candidate_id, prompt_version). ``agent_safe_auto_trade_switch`` exposes only ``autotrade.enabled`` to the read-only
agent role, so the Brain can describe the operating mode. The role still has no access to ``shadow_debate_verdicts``.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "068"
down_revision: str | None = "067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE shadow_debate_verdicts DROP CONSTRAINT shadow_debate_verdicts_pkey")
    op.execute("ALTER TABLE shadow_debate_verdicts ADD PRIMARY KEY (candidate_id, prompt_version)")
    op.execute("""
        CREATE VIEW agent_safe_auto_trade_switch WITH (security_barrier) AS
        SELECT value #>> '{}' AS value_text FROM system_settings WHERE key = 'autotrade.enabled'
    """)
    op.execute("GRANT SELECT ON agent_safe_auto_trade_switch TO cryptonite_agent_ro")


def downgrade() -> None:
    op.execute("DROP VIEW agent_safe_auto_trade_switch")
    # The old key allows one row per candidate: keep each candidate's shadow-v1 row, as ADR 0012 counts it
    op.execute("DELETE FROM shadow_debate_verdicts WHERE prompt_version <> 'shadow-v1'")
    op.execute("ALTER TABLE shadow_debate_verdicts DROP CONSTRAINT shadow_debate_verdicts_pkey")
    op.execute("ALTER TABLE shadow_debate_verdicts ADD PRIMARY KEY (candidate_id)")
