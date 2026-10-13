"""ai_autonomy_events: BREACH_VOIDED, so a breach the system caused can be set aside without deleting the audit row

Revision ID: 045
Revises: 044
Create Date: 2026-10-13 00:00:00.000000

A BREACH counts against the autonomy gate ("0 breaches"). On 2026-10-13 the orchestrator itself mis-sized an
approved entry and recorded the refusal as the AI's breach. Audit rows are never edited or deleted: a
``BREACH_VOIDED`` event names the breach it sets aside (``detail.voids``, the breach event id), with the operator's
reason, and the gate counts only breaches that are not voided. A breach can be voided once.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "045"
down_revision: str | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("'CREATED', 'PROPOSED', 'PROMOTED', 'DEMOTED', 'HALTED', 'RESUMED', 'PAUSED', 'BREACH', 'EDITED', "
         "'BACKSTOP'")


def upgrade() -> None:
    op.execute("ALTER TABLE ai_autonomy_events DROP CONSTRAINT chk_ai_autonomy_events_kind")
    op.execute(f"ALTER TABLE ai_autonomy_events ADD CONSTRAINT chk_ai_autonomy_events_kind "
               f"CHECK (kind IN ({KINDS}, 'BREACH_VOIDED'))")
    op.execute("""CREATE UNIQUE INDEX uq_ai_autonomy_breach_voided ON ai_autonomy_events ((detail->>'voids'))
                  WHERE kind = 'BREACH_VOIDED'""")


def downgrade() -> None:
    # Dropping the kind would delete audit history: refuse while any voiding exists
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM ai_autonomy_events WHERE kind = 'BREACH_VOIDED') THEN
            RAISE EXCEPTION 'ai_autonomy_events has BREACH_VOIDED rows; downgrading would lose audit history';
        END IF; END $$""")
    op.execute("DROP INDEX IF EXISTS uq_ai_autonomy_breach_voided")
    op.execute("ALTER TABLE ai_autonomy_events DROP CONSTRAINT chk_ai_autonomy_events_kind")
    op.execute(f"ALTER TABLE ai_autonomy_events ADD CONSTRAINT chk_ai_autonomy_events_kind CHECK (kind IN ({KINDS}))")
