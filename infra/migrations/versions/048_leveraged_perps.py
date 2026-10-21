"""ai_mandates: leveraged perpetuals (ADR 0009, leverage plan Stage 2.1)

Revision ID: 048
Revises: 047
Create Date: 2026-10-20 00:00:00.000000

- ``chk_ai_mandates_leverage``: ``max_leverage`` 1..10 (was 1..5; D2).
- New ``max_risk_per_trade_pct``: the loss at the stop one trade may risk, % of the budget; default 1, at most 2 (D6).
- ``chk_ai_mandates_perps_l3`` (perps only at L3) becomes ``chk_ai_mandates_perps_level``: perps only at L1, where
  every risk-adding order waits for the operator's TOTP approval (D3; L2/L3 autonomous perps are out of scope).

- ``agent_safe_trade_timeline`` (read-only, leverage plan 3.3) gains ``entry_setup_grade``, ``entry_confidence`` and
  ``entry_leverage`` from the entry command (the system's grade, the model's confidence, the position's leverage),
  and reads perp trades both ways: a perp entry is any order that is not reduce-only (a SELL opens a short), a perp
  close is CLOSE_POSITION or a reduce-only order, and the exit fill is on the closing side. Spot rows read as before.

The upgrade refuses while a mandate has perps enabled away from L1 (it would break the new CHECK; it is not
silently switched off). The downgrade refuses while any mandate has ``max_leverage`` above 5 or perps enabled
below L3, since the old CHECKs would reject them; it restores 047's view.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "048"
down_revision: str | None = "047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEW = "agent_safe_trade_timeline"


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM ai_mandates WHERE allow_perps AND autonomy_level <> 1) THEN
            RAISE EXCEPTION 'a mandate has perps enabled away from L1; disable allow_perps on it before migrating (ADR 0009 D3)';
        END IF; END $$""")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_leverage")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_leverage CHECK (max_leverage >= 1 AND max_leverage <= 10)")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_perps_l3")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_perps_level CHECK (NOT allow_perps OR autonomy_level = 1)")
    op.execute("ALTER TABLE ai_mandates ADD COLUMN max_risk_per_trade_pct NUMERIC(5, 2) NOT NULL DEFAULT 1")
    op.execute("""ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_risk_per_trade
                  CHECK (max_risk_per_trade_pct > 0 AND max_risk_per_trade_pct <= 2)""")
    # The 047 view with perp trades read both ways and three columns appended (CREATE OR REPLACE may only append)
    op.execute(f"""
        CREATE OR REPLACE VIEW {VIEW} WITH (security_barrier) AS
        SELECT p.position_id,
               m.id AS mandate_id,
               m.name AS mandate_name,
               p.instrument_id,
               ps.side,
               p.quantity,
               p.entry_price,
               p.opened_at,
               p.status,
               p.closed_at,
               cf.avg_fill_price AS exit_price,
               p.realized_pnl,
               CASE WHEN p.status = 'CLOSED' AND eo.filled_qty > 0 AND p.entry_price > 0
                    THEN p.realized_pnl / (p.entry_price * eo.filled_qty) * 100 END AS return_pct,
               e.command_id AS entry_command_id,
               a.source AS entry_source,
               left(a.summary, 300) AS entry_summary_text,
               dr.run_id AS debate_run_id,
               dv.recommendation AS debate_recommendation,
               dv.conviction_score AS debate_conviction,
               CASE
                   WHEN cl.command_id IS NULL THEN NULL
                   WHEN cl.payload->>'source' IN ('exit_debate', 'debate') THEN 'EXIT_DEBATE'
                   WHEN cl.payload->>'source' = 'brain_order_card' THEN 'SELL_CARD'
                   WHEN cl.requester_id = 'ai:debate_worker' THEN 'EXIT_DEBATE'
                   WHEN cl.requester_id = 'risk:exit_monitor' THEN cl.payload->>'reason'
                   WHEN cl.requester_id = 'risk:mandate_monitor' THEN 'MANDATE_HALT'
                   WHEN cl.command_type = 'CLOSE_POSITION'
                        AND split_part(cl.requester_id, ':', 1) NOT IN ('risk', 'ai', 'agent') THEN 'MANUAL'
                   ELSE NULL
               END AS close_reason,
               cl.command_id AS close_command_id,
               px.n AS partial_exits,
               oc.outcome_24h_pct,
               oc.outcome_72h_pct,
               left(rf.lessons_learned, 300) AS reflection_text,
               CASE WHEN e.payload->>'setup_grade' IN ('A', 'B', 'C', 'NO_TRADE') THEN e.payload->>'setup_grade' END
                   AS entry_setup_grade,
               CASE WHEN e.payload->>'confidence' ~ '^[0-9.]+$' THEN (e.payload->>'confidence')::numeric END
                   AS entry_confidence,
               COALESCE(p.leverage, CASE WHEN e.payload->>'leverage' ~ '^[0-9]+$' THEN (e.payload->>'leverage')::int END)
                   AS entry_leverage
        FROM paper_positions p
        JOIN ai_mandates m ON 'AI_MANDATE_' || replace(m.id::text, '-', '') = p.instance_id
        LEFT JOIN LATERAL (
            SELECT c.command_id, c.payload
            FROM execution_commands c
            WHERE c.target_instance_id = p.instance_id
              AND c.command_type = 'SUBMIT_ORDER'
              AND c.status = 'COMPLETED'
              AND upper(split_part(c.payload->>'instrument_id', '.', 1)) = upper(split_part(p.instrument_id, '.', 1))
              AND (c.payload->>'side' = 'BUY'
                   OR (p.instrument_id LIKE '%-PERP.%' AND c.payload->>'side' = 'SELL'
                       AND COALESCE(c.payload->>'reduce_only', 'false') <> 'true'))
              AND COALESCE(c.payload->>'reduce_only', 'false') <> 'true'
              AND c.created_at >= p.opened_at - interval '1 day'
              AND c.created_at <= p.opened_at
            ORDER BY c.created_at DESC, c.command_id DESC
            LIMIT 1
        ) e ON TRUE
        LEFT JOIN LATERAL (
            SELECT CASE WHEN p.side <> 'FLAT' THEN p.side
                        WHEN e.command_id IS NOT NULL AND e.payload->>'side' = 'SELL' THEN 'SHORT'  -- a perp short
                        WHEN e.command_id IS NOT NULL THEN 'LONG'  -- the entry is a BUY: spot positions are long only
                        ELSE p.side END AS side
        ) ps ON TRUE
        LEFT JOIN LATERAL (
            SELECT ap.source, ap.source_ref, ap.summary
            FROM ai_approvals ap
            WHERE ap.command_id = e.command_id
            ORDER BY ap.created_at DESC, ap.id DESC
            LIMIT 1
        ) a ON TRUE
        LEFT JOIN LATERAL (
            SELECT CASE WHEN a.source = 'debate'
                         AND a.source_ref ~* '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
                        THEN a.source_ref::uuid END AS run_id
        ) dr ON TRUE
        LEFT JOIN LATERAL (
            SELECT d.id, d.recommendation, d.conviction_score
            FROM portfolio_decisions d
            WHERE d.run_id = dr.run_id
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT 1
        ) dv ON TRUE
        LEFT JOIN LATERAL (
            SELECT max(o.return_pct) FILTER (WHERE o.horizon = '24h') AS outcome_24h_pct,
                   max(o.return_pct) FILTER (WHERE o.horizon = '72h') AS outcome_72h_pct
            FROM ai_decision_outcomes o
            WHERE o.decision_id = dv.id
        ) oc ON TRUE
        LEFT JOIN LATERAL (
            SELECT f.lessons_learned
            FROM reflections f
            WHERE f.run_id = dr.run_id
            ORDER BY f.created_at DESC, f.id DESC
            LIMIT 1
        ) rf ON TRUE
        LEFT JOIN LATERAL (
            SELECT c.command_id, c.command_type, c.requester_id, c.payload, c.created_at
            FROM execution_commands c
            WHERE c.target_instance_id = p.instance_id
              AND c.status = 'COMPLETED'
              AND upper(split_part(c.payload->>'instrument_id', '.', 1)) = upper(split_part(p.instrument_id, '.', 1))
              AND (c.command_type = 'CLOSE_POSITION'
                   OR (c.command_type = 'SUBMIT_ORDER' AND p.instrument_id NOT LIKE '%-PERP.%' AND c.payload->>'side' = 'SELL')
                   OR (c.command_type = 'SUBMIT_ORDER' AND p.instrument_id LIKE '%-PERP.%'
                       AND c.payload->>'reduce_only' = 'true'))
              AND c.created_at >= p.opened_at
              AND c.created_at <= p.closed_at
            ORDER BY c.created_at DESC, c.command_id DESC
            LIMIT 1
        ) cl ON TRUE
        LEFT JOIN LATERAL (
            SELECT count(*) AS n
            FROM execution_commands c
            WHERE c.target_instance_id = p.instance_id
              AND c.status = 'COMPLETED'
              AND upper(split_part(c.payload->>'instrument_id', '.', 1)) = upper(split_part(p.instrument_id, '.', 1))
              AND (c.command_type = 'CLOSE_POSITION'
                   OR (c.command_type = 'SUBMIT_ORDER' AND p.instrument_id NOT LIKE '%-PERP.%' AND c.payload->>'side' = 'SELL')
                   OR (c.command_type = 'SUBMIT_ORDER' AND p.instrument_id LIKE '%-PERP.%'
                       AND c.payload->>'reduce_only' = 'true'))
              AND c.created_at >= p.opened_at
              AND c.created_at < COALESCE(p.closed_at, now())
              AND c.command_id IS DISTINCT FROM cl.command_id
        ) px ON TRUE
        LEFT JOIN LATERAL (
            SELECT o.avg_fill_price
            FROM paper_orders o
            WHERE cl.command_id IS NOT NULL
              AND o.instance_id = p.instance_id
              AND o.instrument_id = p.instrument_id
              AND o.side = CASE WHEN ps.side = 'SHORT' THEN 'BUY' ELSE 'SELL' END
              AND o.filled_qty > 0
              AND o.created_at >= cl.created_at
              AND o.created_at <= p.closed_at
              AND (cl.command_type = 'CLOSE_POSITION'
                   OR o.client_order_id = COALESCE(cl.payload->>'client_order_id',
                                                   'AI-' || upper(left(replace(cl.command_id::text, '-', ''), 12))))
            ORDER BY o.created_at, o.client_order_id
            LIMIT 1
        ) cf ON TRUE
        LEFT JOIN LATERAL (
            SELECT o.filled_qty
            FROM paper_orders o
            WHERE o.client_order_id = COALESCE(e.payload->>'client_order_id',
                                               'AI-' || upper(left(replace(e.command_id::text, '-', ''), 12)))
            LIMIT 1
        ) eo ON TRUE
        WHERE p.opened_at >= now() - interval '90 days'
    """)
    op.execute(f"GRANT SELECT ON {VIEW} TO cryptonite_agent_ro")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM ai_mandates WHERE max_leverage > 5) THEN
            RAISE EXCEPTION 'a mandate has max_leverage above 5; lower it before downgrading below 048';
        END IF;
        IF EXISTS (SELECT 1 FROM ai_mandates WHERE allow_perps AND autonomy_level <> 3) THEN
            RAISE EXCEPTION 'a mandate has perps enabled below L3; disable allow_perps before downgrading below 048';
        END IF; END $$""")
    # 047's view: it has fewer columns, so it is dropped and created again by 047's own upgrade
    op.execute(f"DROP VIEW IF EXISTS {VIEW}")
    spec = importlib.util.spec_from_file_location("m047", Path(__file__).with_name("047_agent_safe_trade_timeline.py"))
    m047 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m047)
    m047.upgrade()
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_risk_per_trade")
    op.execute("ALTER TABLE ai_mandates DROP COLUMN max_risk_per_trade_pct")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_perps_level")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_perps_l3 CHECK (NOT allow_perps OR autonomy_level = 3)")
    op.execute("ALTER TABLE ai_mandates DROP CONSTRAINT chk_ai_mandates_leverage")
    op.execute("ALTER TABLE ai_mandates ADD CONSTRAINT chk_ai_mandates_leverage CHECK (max_leverage >= 1 AND max_leverage <= 5)")
