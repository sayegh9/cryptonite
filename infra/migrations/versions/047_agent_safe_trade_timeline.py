"""agent_safe_trade_timeline: one row per AI-mandate position of the last 90 days, with how it was entered and closed

Revision ID: 047
Revises: 046
Create Date: 2026-10-16 00:00:00.000000

The Brain answers "why did we win or lose on X?" from one row per trade: the entry (a brain card or a debate, and its
summary), the debate verdict, the fills, how the position closed, the realized return, and the learner's 24h and 72h
outcome and reflection. The rows come from the base tables, joined as their owner, so the agent role needs no grant on
them. Only the columns below are exposed: no ``payload``, ``requester_id``, ``decided_by``, TOTP or HMAC columns. Free
text is cut to 300 characters.

Rules the view applies (derived only from what the base tables record):
- A position belongs to a mandate when its ``instance_id`` is ``AI_MANDATE_`` plus the mandate's id without dashes.
- The entry is the latest COMPLETED BUY ``SUBMIT_ORDER`` of that instance and instrument at or before ``opened_at``.
  Its approval is the ``ai_approvals`` row bound to that command. A debate approval's ``source_ref`` is the debate run.
- The debate verdict is the latest ``portfolio_decisions`` row of that run. Outcomes are its ``ai_decision_outcomes``
  rows at 24h and 72h. The reflection is the latest ``reflections`` row of that run.
- The close is the earliest COMPLETED ``CLOSE_POSITION`` (or SELL ``SUBMIT_ORDER``) of that instance and instrument
  created between ``opened_at`` and ``closed_at``. Its ``close_reason`` follows the command that wrote it. Approved
  commands are requested as ``approval:<via>:<who>``, so the payload's ``source`` is read first: ``debate`` or
  ``exit_debate`` gives EXIT_DEBATE, and ``brain_order_card`` gives SELL_CARD. Then the requester: ``ai:debate_worker``
  gives EXIT_DEBATE, ``risk:exit_monitor`` gives the payload's reason (HARD_STOP or TRAILING_STOP), and
  ``risk:mandate_monitor`` gives MANDATE_HALT. Any other CLOSE_POSITION (the operator's) gives MANUAL. Anything else is
  null.
- The exit price is the average fill of the close's own order, and it is never a later position's fill. A SUBMIT_ORDER
  close matches its order by ``client_order_id`` (the payload's, else ``AI-`` plus the first 12 hex digits of the command).
  A CLOSE_POSITION has no order id, so it takes the first SELL fill from the close to ``closed_at``. Null when nothing
  matches. A closed position reads FLAT with quantity 0, so its side comes from the entry command and its return is
  realized_pnl over the entry fill's filled quantity times the entry price, and is null while the position is open.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "047"
down_revision: str | None = "046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEW = "agent_safe_trade_timeline"


def upgrade() -> None:
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
               oc.outcome_24h_pct,
               oc.outcome_72h_pct,
               left(rf.lessons_learned, 300) AS reflection_text
        FROM paper_positions p
        JOIN ai_mandates m ON 'AI_MANDATE_' || replace(m.id::text, '-', '') = p.instance_id
        LEFT JOIN LATERAL (
            SELECT c.command_id, c.payload
            FROM execution_commands c
            WHERE c.target_instance_id = p.instance_id
              AND c.command_type = 'SUBMIT_ORDER'
              AND c.status = 'COMPLETED'
              AND upper(split_part(c.payload->>'instrument_id', '.', 1)) = upper(split_part(p.instrument_id, '.', 1))
              AND c.payload->>'side' = 'BUY'
              AND c.created_at <= p.opened_at
            ORDER BY c.created_at DESC, c.command_id DESC
            LIMIT 1
        ) e ON TRUE
        LEFT JOIN LATERAL (
            SELECT CASE WHEN p.side <> 'FLAT' THEN p.side
                        WHEN e.payload->>'side' = 'BUY' THEN 'LONG'
                        WHEN e.payload->>'side' = 'SELL' THEN 'SHORT'
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
                   OR (c.command_type = 'SUBMIT_ORDER' AND c.payload->>'side' = 'SELL'))
              AND c.created_at >= p.opened_at
              AND c.created_at <= p.closed_at
            ORDER BY c.created_at, c.command_id
            LIMIT 1
        ) cl ON TRUE
        LEFT JOIN LATERAL (
            SELECT o.avg_fill_price
            FROM paper_orders o
            WHERE cl.command_id IS NOT NULL
              AND o.instance_id = p.instance_id
              AND o.instrument_id = p.instrument_id
              AND o.side = 'SELL'
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
    op.execute(f"DROP VIEW IF EXISTS {VIEW}")
