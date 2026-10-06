"""no active test specs outside test databases (review-plan C3, F-M9, F-H2)

Revision ID: 038
Revises: 037
Create Date: 2026-10-06 09:00:00.000000

Integration tests created ``test-spec-*`` scanner specs in the dev DB and left them active; the scanner
evaluated them on every closed bar (A1a: 48 s per event instead of 18.6 s, ~2,100 duplicate candidates
per 3 days). A BEFORE INSERT / UPDATE OF slug, is_archived trigger on ``scan_specs`` now refuses an
active spec whose slug starts with ``test-`` or ``test_`` (any case) in a protected database:

- the database named ``cryptonite`` (the dev/paper database), whatever the session settings, or
- any database with ``cryptonite.protected_db = on`` (``ALTER DATABASE <db> SET cryptonite.protected_db = on``
  for another long-lived database; tests set it per session to exercise the guard).

Throwaway test databases (``cryptonite_ci*``, ``cryptonite_<prefix>_<hex>``) are unaffected, so the
scanner integration tests keep creating test specs there. Existing rows are not checked or changed:
``scripts/cleanup_test_pollution.py`` archives the active test specs. Archiving (is_archived = true)
is always allowed. The error is SQLSTATE 23514 (check_violation).

Grants: none. The trigger function runs as the invoker and reads only ``current_database()`` and a GUC.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "038"
down_revision: str | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.cryptonite_is_protected_db() RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT current_database() = 'cryptonite'
                OR coalesce(current_setting('cryptonite.protected_db', true), '') = 'on'
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.scan_specs_reject_active_test_spec() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT NEW.is_archived AND NEW.slug ~* '^test[-_]' AND public.cryptonite_is_protected_db() THEN
                RAISE EXCEPTION 'scan spec "%" is a test spec and cannot be active in database "%"',
                        NEW.slug, current_database()
                    USING ERRCODE = 'check_violation',
                          HINT = 'Tests must use a throwaway database (cryptonite_ci_*), never the dev DB.';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_scan_specs_no_active_test_spec
        BEFORE INSERT OR UPDATE OF slug, is_archived ON public.scan_specs
        FOR EACH ROW EXECUTE FUNCTION public.scan_specs_reject_active_test_spec();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_scan_specs_no_active_test_spec ON public.scan_specs;")
    op.execute("DROP FUNCTION IF EXISTS public.scan_specs_reject_active_test_spec();")
    op.execute("DROP FUNCTION IF EXISTS public.cryptonite_is_protected_db();")
