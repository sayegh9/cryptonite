"""Reflections from real outcomes (AI-first blueprint A7.1, replacing the F10 heuristic).

A decision gets one reflection once its 24h outcome is labelled (``outcomes.label_pending``): the
signed outcome (BUY profits from a rise; SELL/HOLD, i.e. exit or stay out, from a fall), its alpha
against BTC over the same window (for BTC itself, the outcome), and a short lesson written by the
reviewer persona. The lesson is embedded and tagged with its embedding model (A6.4).
"""

import datetime
import logging

import asyncpg
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.persona import Persona
from outcomes import signed_outcome

logger = logging.getLogger(__name__)

REFLECTION_HORIZON = "24h"
BENCHMARK = "BTCUSDT"
BATCH = 5

REVIEWER = Persona(
    "Quantitative Reviewer", "reflection",
    "You are the Quantitative Reviewer at Cryptonite. "
    "Your job is to analyze completed trades, their actual PnL, and their Alpha versus "
    "the Bitcoin/Ethereum benchmarks. Identify *why* a trade succeeded or failed. "
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


class ReflectionEngine:
    def __init__(self, pg_pool: asyncpg.Pool, query_engine, gateway: Gateway | None = None):
        self.pg_pool = pg_pool
        self.query_engine = query_engine
        self.gateway = gateway or Gateway()

    async def process_pending_reflections(self) -> int:
        async with self.pg_pool.acquire() as conn:
            pending = await conn.fetch(
                """SELECT pd.run_id, pd.instrument_id, pd.recommendation, pd.reasoning, pd.created_at,
                          o.return_pct, o.mfe, o.mae
                   FROM portfolio_decisions pd
                   JOIN ai_decision_outcomes o ON o.decision_id = pd.id AND o.horizon = $1
                   LEFT JOIN reflections r ON r.run_id = pd.run_id
                   WHERE r.id IS NULL
                   ORDER BY pd.created_at
                   LIMIT $2""", REFLECTION_HORIZON, BATCH)
        written = 0
        for row in pending:
            outcome = signed_outcome(row["recommendation"], float(row["return_pct"]))
            if row["instrument_id"].startswith(BENCHMARK):
                alpha = outcome
            else:
                bench = benchmark_return(self.query_engine, row["created_at"], datetime.timedelta(hours=24))
                if bench is None:
                    continue
                alpha = outcome - signed_outcome(row["recommendation"], bench)
            prompt = (
                f"Decision for {row['instrument_id']}: {row['recommendation']}\n"
                f"Original reasoning: {row['reasoning']}\n"
                f"Asset move over the next 24h: {float(row['return_pct']) * 100:+.2f}% "
                f"(best {float(row['mfe']) * 100:+.2f}%, worst {float(row['mae']) * 100:+.2f}%)\n"
                f"Outcome for this decision: {outcome * 100:+.2f}%; alpha vs BTC: {alpha * 100:+.2f}%\n\n"
                "What is the key lesson learned here?"
            )
            lesson = str((await REVIEWER.run(self.gateway, prompt)).output)
            embedding_str = embedding_model = None
            try:
                (vector,), embedding_model = await self.gateway.embed_tagged([lesson], kind="document")
                embedding_str = f"[{','.join(map(str, vector))}]"
            except Exception as e:  # stored without a vector: retrieval falls back to recency for it
                logger.warning("Lesson embedding failed: %s", e)
                embedding_model = None
            tag = "WIN" if outcome > 0 else "LOSS" if outcome < 0 else "FLAT"
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, lessons_learned, embedding,
                                                embedding_model, instrument_type, timeframe, outcome)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                    row["run_id"], round(outcome, 6), round(alpha, 6), lesson, embedding_str, embedding_model,
                    row["instrument_id"], REFLECTION_HORIZON, tag)
            written += 1
            logger.info("Reflection for run %s: outcome %+.2f%%, alpha %+.2f%%", row["run_id"], outcome * 100, alpha * 100)
        return written
