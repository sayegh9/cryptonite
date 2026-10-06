"""debate run recovery: failure reason and stream message id on debate_runs (review-plan B4, F-H6)

Revision ID: 039
Revises: 038
Create Date: 2026-10-06 10:00:00.000000

``failure_reason`` says why a run ended FAILED when the worker did not finish it itself: ``REAPED_STALE``
(RUNNING for more than 30 min, closed by the housekeeping reaper or ``scripts/recover_debate_queue.py``)
or ``WORKER_LOST`` (the message was redelivered after the worker that started the run died). NULL for
every other row, including runs that failed with an error in the worker.

``stream_message_id`` (``<stream>/<entry id>``) is the Redis stream entry an entry debate was started
from. The partial unique index makes redelivery idempotent: a reclaimed message can never create a
second run for the same candidate. NULL for exit debates (their run row is created by the producer and
carried in the payload), operator rows and every row from before this migration.

Grants: none needed. The writer, the debate worker, connects as ``cryptonite_api``, which holds
table-level SELECT, INSERT, UPDATE on ``debate_runs`` (016); table-level grants cover new columns.
The table is small (one row per debate), so the index is built in the migration's transaction.

Downgrade drops the index and both columns (the reasons are lost; the FAILED status stays).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "039"
down_revision: str | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE debate_runs ADD COLUMN failure_reason VARCHAR(64)")
    op.execute("ALTER TABLE debate_runs ADD COLUMN stream_message_id VARCHAR(128)")
    op.execute(
        "CREATE UNIQUE INDEX uq_debate_runs_stream_message_id ON debate_runs (stream_message_id) "
        "WHERE stream_message_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_debate_runs_stream_message_id")
    op.execute("ALTER TABLE debate_runs DROP COLUMN IF EXISTS stream_message_id")
    op.execute("ALTER TABLE debate_runs DROP COLUMN IF EXISTS failure_reason")
