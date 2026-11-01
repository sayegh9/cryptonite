"""AI Phase 2: net-cost candidate labels, shadow policy decisions, GATE rows linked to candidates, no 5m scans

Revision ID: 057
Revises: 056
Create Date: 2026-11-01 12:00:00.000000

Phase 2 plan, migration 057.
1. ``candidate_outcomes`` gains the cost-aware labels (``entry_next_open``, ``net_r_preset``, ``net_r_profile``,
   ``cost_model``); the learner (``cryptonite_api``) already holds SELECT/INSERT/UPDATE on the table.
2. ``policy_decisions``: one row per (candidate, policy A/B/C). The debate worker runs as ``cryptonite_api``; the
   Brain reads it only through the ``agent_safe_policy_decisions`` security-barrier view.
3. ``ai_profile_decisions.candidate_id`` (nullable, indexed, no foreign key so a decision log write never fails).
4. O4: active global scan specs whose timeframes include ``5m`` get a new version without it, the way
   ``PUT /api/v1/scanners/{id}`` versions a spec; the old version's checkpoints are copied so the scanner does not
   rescan history. Downgrade re-activates the preceding version when the active one is exactly it minus ``5m``.
"""
import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "057"
down_revision: str | None = "056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEW = "agent_safe_policy_decisions"
DROP_TF = "5m"


def _spec_hash(universe: dict, rules: list) -> str:
    # cryptonite_scanner.models.compute_spec_hash as called by routes/scanners.py: rules wrapped in {"rules": ...}
    canonical = {"rules": {"rules": rules}, "universe": universe}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _drop_5m_from_active_specs() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT s.id AS spec_id, v.id AS version_id, v.universe, v.rules "
        "FROM scan_specs s JOIN scan_spec_versions v ON v.id = s.active_version_id "
        "WHERE NOT s.is_archived AND (v.universe -> 'timeframes') ? :tf"
    ), {"tf": DROP_TF}).mappings().all()
    for row in rows:
        universe = _json(row["universe"])
        rules = _json(row["rules"])
        universe = {**universe, "timeframes": [tf for tf in universe["timeframes"] if tf != DROP_TF]}
        new_id = uuid.uuid4()
        conn.execute(sa.text(
            "INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules) "
            "SELECT :id, :spec_id, COALESCE(MAX(version_num), 0) + 1, :hash, CAST(:universe AS jsonb), "
            "CAST(:rules AS jsonb) FROM scan_spec_versions WHERE spec_id = :spec_id"
        ), {"id": new_id, "spec_id": row["spec_id"], "hash": _spec_hash(universe, rules),
            "universe": json.dumps(universe), "rules": json.dumps(rules)})
        conn.execute(sa.text(
            "INSERT INTO scanner_checkpoints (spec_version_id, instrument_id, source_timeframe, "
            "last_processed_watermark_ns, last_run_id) "
            "SELECT :new_id, instrument_id, source_timeframe, last_processed_watermark_ns, last_run_id "
            "FROM scanner_checkpoints WHERE spec_version_id = :old_id"
        ), {"new_id": new_id, "old_id": row["version_id"]})
        conn.execute(sa.text(
            "UPDATE scan_specs SET active_version_id = :new_id, updated_at = now() "
            "WHERE id = :spec_id AND active_version_id = :old_id"
        ), {"new_id": new_id, "spec_id": row["spec_id"], "old_id": row["version_id"]})


def _restore_5m_on_active_specs() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT s.id AS spec_id, v.id AS version_id, p.id AS prev_id, v.universe, v.rules, "
        "       p.universe AS prev_universe, p.rules AS prev_rules "
        "FROM scan_specs s JOIN scan_spec_versions v ON v.id = s.active_version_id "
        "JOIN scan_spec_versions p ON p.spec_id = s.id AND p.version_num = v.version_num - 1 "
        "WHERE NOT s.is_archived AND (p.universe -> 'timeframes') ? :tf AND NOT (v.universe -> 'timeframes') ? :tf"
    ), {"tf": DROP_TF}).mappings().all()
    for row in rows:
        prev = _json(row["prev_universe"])
        stripped = {**prev, "timeframes": [tf for tf in prev["timeframes"] if tf != DROP_TF]}
        if stripped != _json(row["universe"]) or _json(row["prev_rules"]) != _json(row["rules"]):
            continue
        conn.execute(sa.text(
            "UPDATE scan_specs SET active_version_id = :prev_id, updated_at = now() "
            "WHERE id = :spec_id AND active_version_id = :version_id"
        ), {"prev_id": row["prev_id"], "spec_id": row["spec_id"], "version_id": row["version_id"]})


def upgrade() -> None:
    op.execute("""
        ALTER TABLE candidate_outcomes
            ADD COLUMN entry_next_open NUMERIC,
            ADD COLUMN net_r_preset NUMERIC,
            ADD COLUMN net_r_profile NUMERIC,
            ADD COLUMN cost_model TEXT
    """)
    op.execute("""
        CREATE TABLE policy_decisions (
            candidate_id UUID NOT NULL REFERENCES signal_candidates(id) ON DELETE CASCADE,
            policy CHAR(1) NOT NULL,
            decision TEXT NOT NULL,
            side TEXT,
            size_factor NUMERIC,
            reason TEXT,
            model TEXT,
            cost_usd NUMERIC,
            decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_policy_decisions PRIMARY KEY (candidate_id, policy),
            CONSTRAINT chk_policy_decisions_policy CHECK (policy IN ('A', 'B', 'C')),
            CONSTRAINT chk_policy_decisions_decision CHECK (decision IN ('TAKE', 'SKIP', 'ERROR'))
        )
    """)
    op.execute("CREATE INDEX ix_policy_decisions_decided_at ON policy_decisions (decided_at)")
    op.execute(f"CREATE VIEW {VIEW} WITH (security_barrier) AS SELECT * FROM policy_decisions")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT, INSERT, UPDATE ON policy_decisions TO cryptonite_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON agent_safe_policy_decisions TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE ai_profile_decisions ADD COLUMN candidate_id UUID")
    op.execute(
        "CREATE INDEX ix_ai_profile_decisions_candidate ON ai_profile_decisions (candidate_id) "
        "WHERE candidate_id IS NOT NULL"
    )
    _drop_5m_from_active_specs()


def downgrade() -> None:
    _restore_5m_on_active_specs()
    op.execute("DROP INDEX IF EXISTS ix_ai_profile_decisions_candidate")
    op.execute("ALTER TABLE ai_profile_decisions DROP COLUMN IF EXISTS candidate_id")
    op.execute(f"DROP VIEW IF EXISTS {VIEW}")
    op.execute("DROP TABLE IF EXISTS policy_decisions")
    op.execute("""
        ALTER TABLE candidate_outcomes
            DROP COLUMN IF EXISTS entry_next_open,
            DROP COLUMN IF EXISTS net_r_preset,
            DROP COLUMN IF EXISTS net_r_profile,
            DROP COLUMN IF EXISTS cost_model
    """)
