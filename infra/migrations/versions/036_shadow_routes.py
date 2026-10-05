"""shadow routes: a candidate model answers live traffic, logged and never acted on (remediation F2)

Revision ID: 036
Revises: 035
Create Date: 2026-10-05 18:00:00.000000

``ai_model_routes`` gains ``shadow_model`` (NULL = no shadow) and ``shadow_sample_rate`` (0–1, the
share of the role's calls replayed on the shadow model; 0 = off). Protected roles may have one: the
shadow is never acted on. Routes are edited in SQL, e.g.::

    INSERT INTO ai_model_routes (role, shadow_model, shadow_sample_rate)
    VALUES ('triage', 'ollama:qwen3.5:4b', 0.25)
    ON CONFLICT (role) DO UPDATE SET shadow_model = EXCLUDED.shadow_model,
        shadow_sample_rate = EXCLUDED.shadow_sample_rate, updated_at = now();

``ai_shadow_results`` holds one row per sampled call: both outputs, latencies, costs, agreement on the
role's key field, and the error or skip reason. Grants follow ``ai_decision_traces``: the gateway's
telemetry role may only INSERT, the API may only SELECT. Paid shadow models also need
``system_settings`` ``AI_SHADOW_ALLOW_PAID = true``; that key is not created here (absent = false).

Note: 035 comes from a parallel branch; integration merges it first.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "036"
down_revision: str | None = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TELEMETRY_ROLE = "cryptonite_ai_telemetry"


def upgrade() -> None:
    op.execute("ALTER TABLE ai_model_routes ADD COLUMN shadow_model TEXT")
    op.execute("ALTER TABLE ai_model_routes ADD COLUMN shadow_sample_rate NUMERIC(5, 4) NOT NULL DEFAULT 0")
    op.execute(
        "ALTER TABLE ai_model_routes ADD CONSTRAINT chk_ai_route_shadow_rate "
        "CHECK (shadow_sample_rate >= 0 AND shadow_sample_rate <= 1)"
    )
    op.execute("""
    CREATE TABLE ai_shadow_results (
        id UUID PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        role VARCHAR(64) NOT NULL,
        primary_model VARCHAR(128) NOT NULL,
        shadow_model VARCHAR(128) NOT NULL,
        status VARCHAR(16) NOT NULL,
        primary_trace_id UUID,
        parent_trace_id UUID,
        primary_output JSONB,
        shadow_output JSONB,
        key_field VARCHAR(64),
        agrees BOOLEAN,
        primary_latency_ms INTEGER,
        shadow_latency_ms INTEGER,
        primary_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
        shadow_cost_usd NUMERIC(16, 6) NOT NULL DEFAULT 0,
        error TEXT,
        CONSTRAINT chk_ai_shadow_status CHECK (status IN ('OK', 'ERROR', 'SKIPPED')),
        CONSTRAINT chk_ai_shadow_cost_non_negative CHECK (primary_cost_usd >= 0 AND shadow_cost_usd >= 0)
    )
    """)
    op.execute("CREATE INDEX ix_ai_shadow_results_role_created ON ai_shadow_results (role, created_at)")
    op.execute("CREATE INDEX ix_ai_shadow_results_parent ON ai_shadow_results (parent_trace_id) "
               "WHERE parent_trace_id IS NOT NULL")
    op.execute(f"GRANT INSERT ON TABLE ai_shadow_results TO {TELEMETRY_ROLE}")
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT ON TABLE ai_shadow_results TO cryptonite_api;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_shadow_results")
    op.execute("ALTER TABLE ai_model_routes DROP CONSTRAINT IF EXISTS chk_ai_route_shadow_rate")
    op.execute("ALTER TABLE ai_model_routes DROP COLUMN IF EXISTS shadow_sample_rate")
    op.execute("ALTER TABLE ai_model_routes DROP COLUMN IF EXISTS shadow_model")
