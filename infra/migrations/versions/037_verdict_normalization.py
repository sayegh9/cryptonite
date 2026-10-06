"""verdict normalization: portfolio_decisions.recommendation is BUY | SELL | HOLD (review-plan B2, F-C2, F-M6)

Revision ID: 037
Revises: 036
Create Date: 2026-10-05 20:00:00.000000

``recommendation`` held the CRO's raw ``conviction_level`` (Buy, Overweight, Hold, ...) in mixed case, so
scoring graded Overweight as a SELL-side miss. It now holds the direction only; the CRO's own wording moves
to the new nullable ``conviction_level`` column (shown to the operator; kept for conviction sizing, which is not
wired to debate orders yet).

Backfill (every existing row): ``conviction_level`` = the original ``recommendation`` spelling, then
``recommendation`` is normalized case-insensitively, trimming spaces, tabs and line breaks (``WHITESPACE``),
with the mapping of ``normalize_verdict()``:
Buy / Overweight / STRONG_BUY / LONG -> BUY, Sell / Underweight / STRONG_SELL / SHORT -> SELL, Hold -> HOLD.
Any other value aborts the migration with the list of offending values (nothing is guessed); the whole
upgrade runs in one transaction, so an abort leaves the table untouched. ``chk_portfolio_decisions_recommendation``
then rejects any non-normalized write, so the debate worker's write-time fix must be deployed with this.

Grants: none needed. The only writer, the debate worker, connects as ``cryptonite_api``, which holds
table-level SELECT, INSERT, UPDATE on ``portfolio_decisions`` (016); table-level grants cover new columns.
``cryptonite_agent_ro`` has no access to this table (014) and gets none.

Downgrade drops the CHECK, restores the original spelling from ``conviction_level`` where it is set, and
drops the column.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "037"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in step with cryptonite_schemas.debate.normalize_verdict(); keys are upper-cased.
VERDICTS = {
    "BUY": "BUY", "OVERWEIGHT": "BUY", "STRONG_BUY": "BUY", "LONG": "BUY",
    "SELL": "SELL", "UNDERWEIGHT": "SELL", "STRONG_SELL": "SELL", "SHORT": "SELL",
    "HOLD": "HOLD",
}
# Keep in step with cryptonite_schemas.debate.VERDICT_WHITESPACE: the characters trimmed around a value.
WHITESPACE = " \t\r\n"
_TRIM_SET = "E'" + WHITESPACE.translate(str.maketrans({"\t": r"\t", "\r": r"\r", "\n": r"\n"})) + "'"
_NORMALIZED = f"upper(btrim(recommendation, {_TRIM_SET}))"
_KEYS = ", ".join(f"'{k}'" for k in VERDICTS)
_CASE = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in VERDICTS.items())


def upgrade() -> None:
    # Fail loudly before changing anything if a stored value has no mapping.
    op.execute(f"""
    DO $$
    DECLARE unmapped TEXT;
    BEGIN
        SELECT string_agg(format('%L (%s rows)', recommendation, n), ', ' ORDER BY recommendation)
          INTO unmapped
          FROM (SELECT recommendation, count(*) AS n FROM portfolio_decisions
                 WHERE {_NORMALIZED} NOT IN ({_KEYS})
                 GROUP BY recommendation) u;
        IF unmapped IS NOT NULL THEN
            RAISE EXCEPTION '037: portfolio_decisions.recommendation has values with no BUY/SELL/HOLD mapping: %', unmapped;
        END IF;
    END $$;
    """)
    op.execute("ALTER TABLE portfolio_decisions ADD COLUMN conviction_level VARCHAR(32)")
    op.execute(f"""
    UPDATE portfolio_decisions
       SET conviction_level = recommendation,
           recommendation = CASE {_NORMALIZED} {_CASE} END
    """)
    op.execute(
        "ALTER TABLE portfolio_decisions ADD CONSTRAINT chk_portfolio_decisions_recommendation "
        "CHECK (recommendation IN ('BUY', 'SELL', 'HOLD'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE portfolio_decisions DROP CONSTRAINT IF EXISTS chk_portfolio_decisions_recommendation")
    op.execute("UPDATE portfolio_decisions SET recommendation = conviction_level WHERE conviction_level IS NOT NULL")
    op.execute("ALTER TABLE portfolio_decisions DROP COLUMN IF EXISTS conviction_level")
