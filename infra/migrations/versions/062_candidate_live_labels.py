"""H11: live-parity candidate labels (net_r_live) next to the 057 net labels

Revision ID: 062
Revises: 061
Create Date: 2026-11-03 16:00:00.000000

``net_r_profile`` (057) replays a 5% hard stop and a +3% / 2% trail only, so on a tight structural stop its losses
run to several R. ``net_r_live`` is the exit monitor's own rules replayed bar by bar
(``cryptonite_schemas.exit_rules.simulate_live_exit``): structural stop, TP1 partial, profile trail and max hold, the
hard stop as backstop, net of cost model v1. ``live_outcome`` is the closing rule, ``live_bars`` the 1m bars to the
last fill, ``live_exit_model`` the exits used (NULL = not labelled yet; the learner backfills oldest first). The
055 / 057 columns are not rewritten. ``agent_safe_candidate_outcomes`` does not exist; the table grants (055) cover
the new columns.

It also attributes a perp position closed by its resting stop in ``agent_safe_trade_timeline``: such a close has
no closing command, so ``exit_price`` and ``close_reason`` were NULL (the BTC perp short closed 2026-11-03 13:29). The
closing fill is now also found as the position's own filled STOP_MARKET paper order (its row is written when the stop
is placed, just before ``opened_at``), and ``close_reason`` reads RESTING_STOP. The view is patched from Postgres' own
definition (as 061 does); the columns are unchanged, and past rows read right at once (nothing is stored).

Follows H10's 061 (debate evidence snapshots); deploy 061 first.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "062"
down_revision: str | None = "061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STOP = "((o.order_type)::text = 'STOP_MARKET'::text)"
# (as pg_get_viewdef prints 048/061's text, new text): every one must be found, or the migration stops
_TIMELINE_PATCHES = [
    ("SELECT o.avg_fill_price\n", "SELECT o.avg_fill_price, o.order_type\n"),
    ("WHERE ((cl.command_id IS NOT NULL) AND ", f"WHERE (((cl.command_id IS NOT NULL) OR {_STOP}) AND "),
    ("(o.created_at >= cl.created_at)", "(o.created_at >= COALESCE(cl.created_at, (p.opened_at - '00:01:00'::interval)))"),
    ("(((cl.command_type)::text = 'CLOSE_POSITION'::text) OR ((o.client_order_id)::text = ",
     f"({_STOP} OR ((cl.command_type)::text = 'CLOSE_POSITION'::text) OR ((o.client_order_id)::text = "),
    ("WHEN (cl.command_id IS NULL) THEN NULL::text",
     "WHEN (cl.command_id IS NULL) THEN CASE WHEN ((cf.order_type)::text = 'STOP_MARKET'::text) "
     "THEN 'RESTING_STOP'::text ELSE NULL::text END"),
]


# The same edits as Postgres prints them back after the upgrade (it reformats CASE and drops the patched parentheses)
_TIMELINE_UNPATCHES = [
    ("WHEN (cl.command_id IS NULL) THEN\n            CASE\n                WHEN ((cf.order_type)::text = "
     "'STOP_MARKET'::text) THEN 'RESTING_STOP'::text\n                ELSE NULL::text\n            END\n",
     "WHEN (cl.command_id IS NULL) THEN NULL::text\n"),
    (f"({_STOP} OR ((cl.command_type)::text = 'CLOSE_POSITION'::text) OR ((o.client_order_id)::text = ",
     "(((cl.command_type)::text = 'CLOSE_POSITION'::text) OR ((o.client_order_id)::text = "),
    ("(o.created_at >= COALESCE(cl.created_at, (p.opened_at - '00:01:00'::interval)))", "(o.created_at >= cl.created_at)"),
    (f"WHERE (((cl.command_id IS NOT NULL) OR {_STOP}) AND ", "WHERE ((cl.command_id IS NOT NULL) AND "),
    ("SELECT o.avg_fill_price,\n            o.order_type\n", "SELECT o.avg_fill_price\n"),
]


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _patch_timeline(patches: list[tuple[str, str]]) -> None:
    steps = "\n".join(
        f"IF position({_q(old)} in d) = 0 THEN RAISE EXCEPTION 'agent_safe_trade_timeline lacks %', {_q(old)}; END IF;\n"
        f"d := replace(d, {_q(old)}, {_q(new)});" for old, new in patches)
    op.execute(f"""
        DO $h11$ DECLARE d text; BEGIN
            d := pg_get_viewdef('agent_safe_trade_timeline'::regclass);
            {steps}
            EXECUTE 'CREATE OR REPLACE VIEW agent_safe_trade_timeline WITH (security_barrier) AS ' || d;
        END $h11$
    """)


def upgrade() -> None:
    op.execute("""
        ALTER TABLE candidate_outcomes
            ADD COLUMN IF NOT EXISTS net_r_live NUMERIC,
            ADD COLUMN IF NOT EXISTS live_outcome TEXT,
            ADD COLUMN IF NOT EXISTS live_bars INTEGER,
            ADD COLUMN IF NOT EXISTS live_exit_model TEXT
    """)
    _patch_timeline(_TIMELINE_PATCHES)


def downgrade() -> None:
    # the "SELECT ... order_type" edit goes last: the reason CASE reads cf.order_type until it is reverted
    _patch_timeline(_TIMELINE_UNPATCHES)
    op.execute("""
        ALTER TABLE candidate_outcomes
            DROP COLUMN IF EXISTS net_r_live,
            DROP COLUMN IF EXISTS live_outcome,
            DROP COLUMN IF EXISTS live_bars,
            DROP COLUMN IF EXISTS live_exit_model
    """)
