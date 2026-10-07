"""debate run recovery: failure reason and stream message id on debate_runs (review-plan B4, F-H6)

Revision ID: 040
Revises: 039
Create Date: 2026-10-06 10:00:00.000000

``failure_reason`` says why a run ended FAILED when the worker did not finish it itself: ``REAPED_STALE``
(RUNNING for more than 30 min, closed by the housekeeping reaper or ``scripts/recover_debate_queue.py``)
or ``WORKER_LOST`` (the message was redelivered after the worker that started the run died). NULL for
every other row, including runs that failed with an error in the worker.

``stream_message_id`` (``<stream>/<entry id>``) is the Redis stream entry a debate was started
from. The partial unique index makes redelivery idempotent: a reclaimed message can never create a
second run for the same candidate or exit trigger. Populated for entry and exit debates on claim/insert;
NULL for operator rows and legacy rows from before this migration.

``claim_gen`` (B4) is the attempt generation of the run. A claim locks the row FOR UPDATE, proves the
current Redis PEL ownership of the message inside that transaction, then bumps the generation; every
terminal write is fenced on the generation, so an attempt whose run was taken over writes nothing.

Grants: none needed. The writer, the debate worker, connects as ``cryptonite_api``, which holds
table-level SELECT, INSERT, UPDATE on ``debate_runs`` (016); table-level grants cover new columns.
The table is small (one row per debate), so the index is built in the migration's transaction.

Downgrade drops the index and the columns (the reasons and claim generations are lost; the FAILED status stays).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "040"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE debate_runs ADD COLUMN failure_reason VARCHAR(64)")
    op.execute("ALTER TABLE debate_runs ADD COLUMN stream_message_id VARCHAR(128)")
    op.execute("ALTER TABLE debate_runs ADD COLUMN claim_gen BIGINT NOT NULL DEFAULT 0")
    op.execute(
        "CREATE UNIQUE INDEX uq_debate_runs_stream_message_id ON debate_runs (stream_message_id) "
        "WHERE stream_message_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_debate_runs_stream_message_id")
    op.execute("ALTER TABLE debate_runs DROP COLUMN IF EXISTS claim_gen")
    op.execute("ALTER TABLE debate_runs DROP COLUMN IF EXISTS stream_message_id")
    op.execute("ALTER TABLE debate_runs DROP COLUMN IF EXISTS failure_reason")
