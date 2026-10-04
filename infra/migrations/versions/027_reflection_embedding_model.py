"""reflection embedding model: tag every stored vector with the model that produced it

Revision ID: 027
Revises: 026
Create Date: 2026-10-04 12:00:00.000000

AI-first blueprint §4.7 / A6.4. Vectors from different embedding models live in different spaces and
must never be compared. ``reflections.embedding_model`` records the producer of ``embedding``;
retrieval only compares vectors of the current ``embed`` model, so a partial re-embed or a route
change can never mix spaces. Existing vectors came from Gemini ``text-embedding-004``.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "027"
down_revision: str | None = "026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE reflections ADD COLUMN embedding_model VARCHAR(64)")
    op.execute("UPDATE reflections SET embedding_model = 'google:text-embedding-004' WHERE embedding IS NOT NULL")
    op.execute("""
        ALTER TABLE reflections ADD CONSTRAINT chk_reflections_embedding_model
        CHECK ((embedding IS NULL) = (embedding_model IS NULL))
    """)
    op.execute("CREATE INDEX ix_reflections_embedding_model ON reflections (embedding_model)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_reflections_embedding_model")
    op.execute("ALTER TABLE reflections DROP CONSTRAINT IF EXISTS chk_reflections_embedding_model")
    op.execute("ALTER TABLE reflections DROP COLUMN IF EXISTS embedding_model")
