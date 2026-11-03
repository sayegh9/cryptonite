"""H10: one immutable, versioned evidence snapshot per debate run, and the post-check's validation results

Revision ID: 061
Revises: 060
Create Date: 2026-11-03 14:00:00.000000

H10 item 1. ``debate_evidence_snapshots`` holds the facts every role of a run saw (positions on the coin's related
instruments, profile, quote and mark, bars, scanner setups, funding, open interest, news), each with its source,
``as_of``, unit and status. A refresh during the debate is a new ``version``; rows are never updated (a trigger refuses
UPDATE, and no role gets UPDATE or DELETE). ``debate_validation_results`` holds the deterministic post-check's flags
(BLOCK on an essential fact, else WARN). ``debate_runs`` gains the version the verdict was taken on and the named
non-actionable reason (``failure_reason`` keeps its 64-character code). Older runs have no snapshot row: the API and
the app show them as "no snapshot".

Grants mirror ``debate_turns`` (migration 016: the worker and API role ``cryptonite_api`` reads and writes it) without
UPDATE, and the read-only agent role may SELECT (as it reads reflections), with no write of any kind. Role guards
follow migration 059. P5 (live profiles) moves to 062.

The same H10 PR adds the Brain's side of the audit chain (2026-11-03 Brain session 391c04c0):
- ``agent_safe_debate_verdicts`` gains ``candidate_id`` (appended), so a Brain setup or order card carries the
  automatic path's verdict for the same scanner candidate.
- ``agent_safe_account_equity`` (account id, equity, updated_at; read-only) lets the Brain's profile proposal check
  the 1%-of-equity minimum order itself when the API's feasibility endpoint cannot be reached.
- ``reflections`` can hold a Brain-card TRADE lesson: ``run_id`` becomes nullable, with ``entry_command_id`` (the
  card's filled entry command, unique), ``brain_card_id`` and ``candidate_id``; one of ``run_id`` and
  ``entry_command_id`` is always set. ``agent_safe_reflections`` is recreated with those columns and the instrument
  from the reflection itself when there is no debate run; ``agent_safe_trade_timeline`` joins a reflection by its
  entry command too. The learner (``cryptonite_api``) may read the timeline view.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "061"
down_revision: str | None = "060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE debate_evidence_snapshots (
            run_id uuid NOT NULL REFERENCES debate_runs(id) ON DELETE CASCADE,
            version integer NOT NULL CHECK (version >= 1),
            schema_version integer NOT NULL,
            purpose varchar(8) NOT NULL CHECK (purpose IN ('ENTRY', 'EXIT')),
            instrument_id varchar(64) NOT NULL,
            essential_ok boolean NOT NULL,
            snapshot jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (run_id, version)
        )
    """)
    op.execute("""
        CREATE TABLE debate_validation_results (
            id bigserial PRIMARY KEY,
            run_id uuid NOT NULL REFERENCES debate_runs(id) ON DELETE CASCADE,
            snapshot_version integer NOT NULL,
            role varchar(64) NOT NULL,
            code varchar(48) NOT NULL,
            fact varchar(32) NOT NULL,
            severity varchar(8) NOT NULL CHECK (severity IN ('BLOCK', 'WARN')),
            detail text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_debate_validation_results_run ON debate_validation_results (run_id)")
    op.execute("ALTER TABLE debate_runs ADD COLUMN evidence_snapshot_version integer")
    op.execute("ALTER TABLE debate_runs ADD COLUMN non_actionable_reason text")
    op.execute("""
        CREATE FUNCTION debate_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable: save a new version instead', TG_TABLE_NAME;
        END $$
    """)
    for table in ("debate_evidence_snapshots", "debate_validation_results"):
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table}
                       FOR EACH ROW EXECUTE FUNCTION debate_evidence_immutable()""")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT, INSERT ON debate_evidence_snapshots, debate_validation_results TO cryptonite_api;
                GRANT USAGE, SELECT ON SEQUENCE debate_validation_results_id_seq TO cryptonite_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON debate_evidence_snapshots, debate_validation_results TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)


    _brain_audit_upgrade()


_VERDICTS_046 = """
    CREATE OR REPLACE VIEW agent_safe_debate_verdicts WITH (security_barrier) AS
    SELECT r.id AS run_id, r.instrument_id, r.status, r.trigger_source, r.created_at, r.completed_at,
           r.failure_reason, r.mandate_id, r.decision_trace_id,
           d.recommendation, d.conviction_score, d.conviction_level, d.allocation_pct, d.decision_price,
           left(d.reasoning, 1500) AS reasoning{extra}
    FROM debate_runs r
    LEFT JOIN LATERAL (
        SELECT recommendation, conviction_score, conviction_level, allocation_pct, decision_price, reasoning
        FROM portfolio_decisions
        WHERE run_id = r.id
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    ) d ON TRUE
    WHERE r.created_at >= now() - interval '30 days'
"""
_REFLECTIONS_VIEW = """
    CREATE VIEW agent_safe_reflections WITH (security_barrier) AS
    SELECT f.id, f.run_id, {instrument} AS instrument_id, f.timeframe, f.volatility_regime, f.outcome,
           f.actual_outcome_pnl, f.alpha_vs_benchmark, f.lessons_learned, f.created_at, f.kind, f.recall_excluded{extra}
    FROM reflections f
    LEFT JOIN debate_runs r ON r.id = f.run_id
"""
_TIMELINE_RUN_JOIN = "(f.run_id = dr.run_id)"
_TIMELINE_CARD_JOIN = "((f.run_id = dr.run_id) OR (f.entry_command_id = e.command_id))"


def _patch_timeline(old: str, new: str) -> None:
    """Rewrite the trade timeline's reflection join from Postgres' own definition of the view (048's text)."""
    op.execute(f"""
        DO $$ DECLARE d text; BEGIN
            d := pg_get_viewdef('agent_safe_trade_timeline'::regclass);
            IF position('{old}' in d) = 0 THEN
                RAISE EXCEPTION 'agent_safe_trade_timeline has no reflection join %', '{old}';
            END IF;
            EXECUTE 'CREATE OR REPLACE VIEW agent_safe_trade_timeline WITH (security_barrier) AS '
                    || replace(d, '{old}', '{new}');
        END $$
    """)


def _brain_audit_upgrade() -> None:
    op.execute(_VERDICTS_046.format(extra=", r.candidate_id"))
    op.execute("""
        CREATE VIEW agent_safe_account_equity WITH (security_barrier) AS
        SELECT account_id, total_equity, updated_at FROM paper_accounts
    """)
    op.execute("""
        ALTER TABLE reflections
            ALTER COLUMN run_id DROP NOT NULL,
            ADD COLUMN entry_command_id uuid,
            ADD COLUMN brain_card_id varchar(128),
            ADD COLUMN candidate_id uuid,
            ADD CONSTRAINT chk_reflections_source CHECK (run_id IS NOT NULL OR entry_command_id IS NOT NULL)
    """)
    op.execute("CREATE UNIQUE INDEX ux_reflections_entry_command ON reflections (entry_command_id) "
               "WHERE entry_command_id IS NOT NULL")
    op.execute("DROP VIEW agent_safe_reflections")
    op.execute(_REFLECTIONS_VIEW.format(instrument="COALESCE(r.instrument_id::text, f.instrument_type::text)",
                                        extra=", f.entry_command_id, f.brain_card_id, f.candidate_id"))
    _patch_timeline(_TIMELINE_RUN_JOIN, _TIMELINE_CARD_JOIN)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON agent_safe_reflections, agent_safe_account_equity TO cryptonite_agent_ro;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT ON agent_safe_trade_timeline TO cryptonite_api;
            END IF;
        END $$;
    """)


def _brain_audit_downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                REVOKE SELECT ON agent_safe_trade_timeline FROM cryptonite_api;
            END IF;
        END $$;
    """)
    _patch_timeline(_TIMELINE_CARD_JOIN, _TIMELINE_RUN_JOIN)
    op.execute("DROP VIEW agent_safe_reflections")
    op.execute("DELETE FROM reflections WHERE run_id IS NULL")  # Brain-card lessons have no place before 061
    op.execute("DROP INDEX IF EXISTS ux_reflections_entry_command")
    op.execute("""
        ALTER TABLE reflections DROP CONSTRAINT chk_reflections_source, DROP COLUMN candidate_id,
            DROP COLUMN brain_card_id, DROP COLUMN entry_command_id, ALTER COLUMN run_id SET NOT NULL
    """)
    op.execute(_REFLECTIONS_VIEW.replace("CREATE VIEW", "CREATE OR REPLACE VIEW").format(
        instrument="r.instrument_id", extra=""))
    op.execute("GRANT SELECT ON agent_safe_reflections TO cryptonite_agent_ro")
    op.execute("DROP VIEW agent_safe_account_equity")
    op.execute("DROP VIEW agent_safe_debate_verdicts")  # an appended column cannot be dropped by CREATE OR REPLACE
    op.execute(_VERDICTS_046.format(extra=""))
    op.execute("GRANT SELECT ON agent_safe_debate_verdicts TO cryptonite_agent_ro")


def downgrade() -> None:
    _brain_audit_downgrade()
    op.execute("DROP TABLE IF EXISTS debate_validation_results")
    op.execute("DROP TABLE IF EXISTS debate_evidence_snapshots")
    op.execute("DROP FUNCTION IF EXISTS debate_evidence_immutable()")
    op.execute("ALTER TABLE debate_runs DROP COLUMN IF EXISTS non_actionable_reason")
    op.execute("ALTER TABLE debate_runs DROP COLUMN IF EXISTS evidence_snapshot_version")
