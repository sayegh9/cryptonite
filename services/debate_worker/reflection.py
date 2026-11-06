"""Reflections from real outcomes (AI-first blueprint A7.1, replacing the F10 heuristic).

A decision gets one reflection once its 24h outcome is labelled (``outcomes.label_pending``): the
signed outcome (BUY profits from a rise; SELL/HOLD, i.e. exit or stay out, from a fall), its alpha
against BTC over the same window (for BTC itself, the outcome), and a short lesson written by the
reviewer persona. The lesson is embedded and tagged with its embedding model (A6.4).

H7a: most verdicts never fill (HOLD, refused, vetoed or expired cards). A reflection on a run with no fill is a
``WHAT_IF`` verdict evaluation: its prompt says no trade was executed and its stored lesson is labelled so. A run with
a fill gets a ``TRADE`` reflection that sees the fill price and the commission paid.

H10: a closed, filled Brain order-card trade (``agent_safe_trade_timeline`` entry source ``brain_card``) gets a TRADE
reflection too, once, keyed by its entry command (migration 061), linked to the card and to the scanner candidate its
card carried. Its outcome is the realized return; its alpha is against BTC over the holding period. What-if lessons
stay as H7a keeps them: a card that never filled has no closed trade and no lesson here.
"""

import datetime
import logging
import uuid

import asyncpg
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.persona import Persona
from cryptonite_schemas.debate import VERDICT_DIRECTIONS, VERDICT_WHITESPACE
from outcomes import signed_outcome

logger = logging.getLogger(__name__)

REFLECTION_HORIZON = "24h"
BENCHMARK = "BTCUSDT"
BATCH = 5
WHAT_IF, TRADE = "WHAT_IF", "TRADE"
WHAT_IF_LABEL = "What-if verdict evaluation (no trade was executed): "

# The run's entry fills: its SUBMIT_ORDER, queued directly (idempotency key ``debate:<run>``) or through its approval
# card, joined to the paper order by client order id (the default id rule of the executor, as in migration 047).
# Migration 060 backfills ``reflections.kind`` with the same rule.
RUN_FILLS_SQL = """
    SELECT sum(o.filled_qty) AS filled_qty,
           sum(o.filled_qty * o.avg_fill_price) / NULLIF(sum(o.filled_qty), 0) AS avg_fill_price,
           sum(t.commission) AS commission
    FROM execution_commands c
    JOIN paper_orders o ON o.client_order_id = COALESCE(
        c.payload->>'client_order_id', 'AI-' || upper(left(replace(c.command_id::text, '-', ''), 12)))
    LEFT JOIN LATERAL (SELECT sum(pt.commission) AS commission FROM paper_trades pt
                       WHERE pt.client_order_id = o.client_order_id) t ON TRUE
    WHERE c.command_type = 'SUBMIT_ORDER' AND o.filled_qty > 0
      AND (c.idempotency_key = 'debate:' || {run}::text
           OR c.command_id IN (SELECT a.command_id FROM ai_approvals a
                               WHERE a.source = 'debate' AND a.source_ref = {run}::text))
"""

REVIEWER = Persona(
    "Quantitative Reviewer", "reflection",
    "You are the Quantitative Reviewer at Cryptonite. "
    "Your job is to analyze debate verdicts against what the market did next, and their alpha versus "
    "the benchmark the prompt gives. The prompt says whether "
    "a trade was executed: when it says no trade was executed, the verdict is a what-if evaluation and you "
    "must never describe it as a trade, a position, a fill or realized PnL. State the assumptions behind the figures "
    "and whether costs are included. One outcome cannot prove why a decision succeeded or failed: offer likely causes "
    "as hypotheses, not as fact. "
    "Output exactly one concise paragraph of 'lessons learned' that will be injected into "
    "the Portfolio Manager's prompt in the future.",
)


def benchmark_return(query_engine, start: datetime.datetime, horizon: datetime.timedelta) -> float | None:
    """BTC's return over [start, start + horizon] from the 1m lake, or None without data."""
    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int((start + horizon).timestamp() * 1_000_000_000)
    bars = query_engine.query_bars_df(symbol=BENCHMARK, timeframe="1m", start_ns=start_ns - 300 * 10**9, end_ns=end_ns)
    if bars.is_empty():
        return None
    bars = bars.sort("ts_event_ns")
    before = bars.filter(bars["ts_event_ns"] <= start_ns)
    if before.is_empty():
        return None
    first, last = float(before["close"][-1]), float(bars["close"][-1])
    return last / first - 1 if first else None


def reflection_kind(row) -> str:
    """TRADE when the run's entry filled, else WHAT_IF."""
    filled = row.get("filled_qty") if hasattr(row, "get") else None
    return TRADE if filled is not None and float(filled) > 0 else WHAT_IF


def _execution_line(row) -> str:
    if reflection_kind(row) == WHAT_IF:
        return ("Execution: NO trade was executed for this verdict (no fill). This is a what-if evaluation of the "
                "verdict only; do not describe it as a trade.\n")
    price, fee = row.get("avg_fill_price"), row.get("commission")
    return ("Execution: the entry filled"
            + (f" at {float(price):g}" if price is not None else "")
            + f" for {float(row['filled_qty']):g} units"
            + (f", commission paid {float(fee):g}" if fee is not None else ", commission unknown") + ".\n")


def reflection_prompt(row, outcome: float, alpha: float) -> str:
    """The reviewer's prompt for one labelled decision (also used by
    ``scripts/regenerate_inverted_reflections.py``)."""
    return (
        f"Decision for {row['instrument_id']}: {row['recommendation']}\n"
        + _execution_line(row) +
        f"Original reasoning: {row['reasoning']}\n"
        f"Asset move over the next 24h: {float(row['return_pct']) * 100:+.2f}% "
        f"(best {float(row['mfe']) * 100:+.2f}%, worst {float(row['mae']) * 100:+.2f}%)\n"
        f"Outcome for this decision: {outcome * 100:+.2f}%; alpha vs BTC: {alpha * 100:+.2f}%\n\n"
        "What is the key lesson learned here?"
    )


CARD_TRADE_TIMEFRAME = "trade"  # a card lesson covers the holding period, not a fixed horizon
CARD_TRADES_SQL = """
    SELECT t.position_id, t.instrument_id, t.side, t.entry_price, t.exit_price, t.opened_at, t.closed_at,
           t.realized_pnl, t.return_pct, t.close_reason, t.entry_command_id, t.entry_summary_text,
           c.payload->>'brain_card_id' AS brain_card_id,
           c.payload->'debate_link'->>'candidate_id' AS candidate_id
    FROM agent_safe_trade_timeline t
    JOIN execution_commands c ON c.command_id = t.entry_command_id
    LEFT JOIN reflections f ON f.entry_command_id = t.entry_command_id
    WHERE t.entry_source = 'brain_card' AND t.status = 'CLOSED' AND t.return_pct IS NOT NULL
      AND t.entry_command_id IS NOT NULL AND f.id IS NULL
    ORDER BY t.closed_at
    LIMIT $1
"""


def card_trade_prompt(row, outcome: float, alpha: float | None) -> str:
    """The reviewer's prompt for one closed Brain order-card trade (a filled trade, never a what-if)."""
    return (
        f"Brain order card trade on {row['instrument_id']}: {row['side']}, approved by the operator from a Brain card.\n"
        f"Execution: the entry filled at {float(row['entry_price']):g}; it closed at "
        + (f"{float(row['exit_price']):g}" if row.get("exit_price") is not None else "an unknown price")
        + f" by {row.get('close_reason') or 'an unrecorded exit'}; realized P&L {float(row['realized_pnl']):+g}.\n"
        f"Card summary: {row.get('entry_summary_text') or 'none'}\n"
        f"Realized return: {outcome * 100:+.2f}%"
        + (f"; alpha vs BTC over the holding period: {alpha * 100:+.2f}%" if alpha is not None else "") + "\n\n"
        "What is the key lesson learned here?"
    )


def outcome_tag(outcome: float) -> str:
    return "WIN" if outcome > 0 else "LOSS" if outcome < 0 else "FLAT"


class ReflectionEngine:
    def __init__(self, pg_pool: asyncpg.Pool, query_engine, gateway: Gateway | None = None):
        self.pg_pool = pg_pool
        self.query_engine = query_engine
        self.gateway = gateway or Gateway()

    async def process_pending_reflections(self) -> int:
        async with self.pg_pool.acquire() as conn:
            pending = await conn.fetch(
                f"""SELECT pd.run_id, pd.instrument_id, pd.recommendation, pd.reasoning, pd.created_at,
                          o.return_pct, o.mfe, o.mae, fill.filled_qty, fill.avg_fill_price, fill.commission
                   FROM portfolio_decisions pd
                   JOIN ai_decision_outcomes o ON o.decision_id = pd.id AND o.horizon = $1
                   LEFT JOIN reflections r ON r.run_id = pd.run_id
                   LEFT JOIN LATERAL ({RUN_FILLS_SQL.format(run="pd.run_id")}) fill ON TRUE
                   WHERE r.id IS NULL
                     -- a verdict with no direction can never be reflected on; it must not hold a batch slot
                     AND upper(btrim(pd.recommendation, $3)) = ANY($4::text[])
                   ORDER BY pd.created_at
                   LIMIT $2""", REFLECTION_HORIZON, BATCH, VERDICT_WHITESPACE, list(VERDICT_DIRECTIONS))
        written = 0
        for row in pending:
            try:
                outcome = signed_outcome(row["recommendation"], float(row["return_pct"]))
            except ValueError as e:  # one unmappable verdict must not stop the rest of the batch
                logger.warning("Reflection skipped for run %s: %s", row["run_id"], e)
                continue
            if row["instrument_id"].startswith(BENCHMARK):
                alpha = outcome
            else:
                bench = benchmark_return(self.query_engine, row["created_at"], datetime.timedelta(hours=24))
                if bench is None:
                    continue
                alpha = outcome - signed_outcome(row["recommendation"], bench)
            kind = reflection_kind(row)
            lesson = str((await REVIEWER.run(self.gateway, reflection_prompt(row, outcome, alpha))).output)
            if kind == WHAT_IF and not lesson.startswith(WHAT_IF_LABEL):
                lesson = WHAT_IF_LABEL + lesson  # the label rides in the text the PM recalls, not only in a column
            embedding_str = embedding_model = None
            try:
                (vector,), embedding_model = await self.gateway.embed_tagged([lesson], kind="document")
                embedding_str = f"[{','.join(map(str, vector))}]"
            except Exception as e:  # stored without a vector: retrieval falls back to recency for it
                logger.warning("Lesson embedding failed: %s", e)
                embedding_model = None
            tag = outcome_tag(outcome)
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, lessons_learned, embedding,
                                                embedding_model, instrument_type, timeframe, kind, outcome)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                    row["run_id"], round(outcome, 6), round(alpha, 6), lesson, embedding_str, embedding_model,
                    row["instrument_id"], REFLECTION_HORIZON, kind, tag)
            written += 1
            logger.info("Reflection (%s) for run %s: outcome %+.2f%%, alpha %+.2f%%", kind, row["run_id"],
                        outcome * 100, alpha * 100)
        return written

    async def process_card_trade_reflections(self) -> int:
        """H10: one TRADE lesson per closed, filled Brain order-card trade."""
        async with self.pg_pool.acquire() as conn:
            pending = await conn.fetch(CARD_TRADES_SQL, BATCH)
        written = 0
        for row in pending:
            outcome = float(row["return_pct"]) / 100
            alpha = None
            if row["opened_at"] and row["closed_at"]:
                if row["instrument_id"].startswith(BENCHMARK):
                    alpha = 0.0
                else:
                    bench = benchmark_return(self.query_engine, row["opened_at"], row["closed_at"] - row["opened_at"])
                    if bench is not None:
                        alpha = outcome - (bench if row["side"] != "SHORT" else -bench)
            lesson = str((await REVIEWER.run(self.gateway, card_trade_prompt(row, outcome, alpha))).output)
            embedding_str = embedding_model = None
            try:
                (vector,), embedding_model = await self.gateway.embed_tagged([lesson], kind="document")
                embedding_str = f"[{','.join(map(str, vector))}]"
            except Exception as e:
                logger.warning("Lesson embedding failed: %s", e)
                embedding_model = None
            try:
                candidate = uuid.UUID(row["candidate_id"]) if row["candidate_id"] else None
            except ValueError:
                candidate = None
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO reflections (run_id, entry_command_id, brain_card_id, candidate_id, actual_outcome_pnl,
                                                alpha_vs_benchmark, lessons_learned, embedding, embedding_model,
                                                instrument_type, timeframe, kind, outcome)
                       VALUES (NULL, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                       ON CONFLICT (entry_command_id) WHERE entry_command_id IS NOT NULL DO NOTHING""",
                    row["entry_command_id"], (row["brain_card_id"] or "")[:128] or None, candidate,
                    round(outcome, 6), round(alpha if alpha is not None else 0.0, 6), lesson, embedding_str,
                    embedding_model, str(row["instrument_id"])[:32], CARD_TRADE_TIMEFRAME, TRADE, outcome_tag(outcome))
            written += 1
            logger.info("Reflection (TRADE) for Brain card %s: outcome %+.2f%%", row["brain_card_id"], outcome * 100)
        return written
