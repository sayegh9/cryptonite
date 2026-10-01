import asyncio
import logging
import duckdb
import json
import os
import asyncpg
from decimal import Decimal
from typing import Optional

from cryptonite_agents.gateway import Gateway
from cryptonite_agents.persona import Persona

logger = logging.getLogger(__name__)
NAUTILUS_CATALOG_PATH = os.getenv("NAUTILUS_CATALOG_PATH", "/app/data/nautilus_catalog")

REVIEWER = Persona(
    "Quantitative Reviewer", "reflection",
    "You are the Quantitative Reviewer at Cryptonite. "
    "Your job is to analyze completed trades, their actual PnL, and their Alpha versus "
    "the Bitcoin/Ethereum benchmarks. Identify *why* a trade succeeded or failed. "
    "Output exactly one concise paragraph of 'lessons learned' that will be injected into "
    "the Portfolio Manager's prompt in the future.",
)

class ReflectionEngine:
    def __init__(self, pg_pool: asyncpg.Pool, gateway: Gateway | None = None):
        self.pg_pool = pg_pool
        self.gateway = gateway or Gateway()
        # Connect to DuckDB for benchmark data (read-only)
        self.duck = duckdb.connect(database=':memory:')

    def _get_benchmark_return(self, instrument_id: str, start_ts: str, end_ts: str) -> float:
        """Query DuckDB Parquet lake to get benchmark return over the trade's lifespan."""
        try:
            # Construct a safe glob path for the catalog
            # Assuming partition format: /app/data/nautilus_catalog/bars/BTCUSDT.BINANCE-1m/*.parquet
            benchmark_symbol = "BTCUSDT.BINANCE" if "USDT" in instrument_id else "ETHUSDT.BINANCE"
            path = f"{NAUTILUS_CATALOG_PATH}/bars/{benchmark_symbol}-1m/*.parquet"
            
            # This is a naive query assuming 'close' and 'ts_init' exist in the parquet schema.
            # In production, this matches the exact schema emitted by nautilus_trader.
            query = f"""
                SELECT 
                    (SELECT close FROM read_parquet('{path}') WHERE ts_init >= '{start_ts}' ORDER BY ts_init ASC LIMIT 1) as start_price,
                    (SELECT close FROM read_parquet('{path}') WHERE ts_init <= '{end_ts}' ORDER BY ts_init DESC LIMIT 1) as end_price
            """
            result = self.duck.execute(query).fetchone()
            if result and result[0] and result[1]:
                start_price, end_price = result[0], result[1]
                return (end_price - start_price) / start_price
            return 0.0
        except Exception as e:
            logger.warning(f"DuckDB benchmark query failed: {e}. Defaulting to 0.0")
            return 0.0

    async def run_reflection_loop(self):
        """Continuously polls for closed decisions to generate reflections."""
        logger.info("Starting Reflection Engine Loop...")
        while True:
            try:
                await self.process_pending_reflections()
            except Exception as e:
                logger.error(f"Reflection loop error: {e}")
            await asyncio.sleep(60) # Run every minute

    async def process_pending_reflections(self):
        async with self.pg_pool.acquire() as conn:
            # Find completed debate_runs that don't have a reflection yet, but the position is closed.
            # We mock the 'position closed' logic for this implementation by selecting runs older than 1 hour
            # that don't exist in the reflections table.
            query = """
                SELECT dr.id as run_id, dr.instrument_id, pd.recommendation, pd.reasoning, dr.created_at, dr.completed_at
                FROM debate_runs dr
                JOIN portfolio_decisions pd ON dr.id = pd.run_id
                LEFT JOIN reflections r ON dr.id = r.run_id
                WHERE dr.status = 'COMPLETED' 
                  AND r.id IS NULL
                  AND dr.completed_at < now() - interval '1 hour'
                LIMIT 10
            """
            pending = await conn.fetch(query)
            
            for row in pending:
                run_id = row['run_id']
                instrument_id = row['instrument_id']
                
                # Query actual realized PnL from closed paper positions
                pnl_row = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(realized_pnl), 0) as total_pnl,
                           COALESCE(AVG(entry_price), 0) as avg_entry
                    FROM paper_positions
                    WHERE instrument_id = $1
                      AND status = 'CLOSED'
                      AND closed_at >= $2
                      AND closed_at <= $3
                    """,
                    instrument_id,
                    row['created_at'],
                    row['completed_at']
                )

                if pnl_row and pnl_row['avg_entry'] and float(pnl_row['avg_entry']) > 0:
                    actual_outcome_pnl = float(pnl_row['total_pnl']) / float(pnl_row['avg_entry'])
                else:
                    # No matching closed positions found — skip reflection for this run
                    logger.info(f"No closed positions found for {instrument_id} in debate window. Skipping reflection.")
                    continue
                
                # Fetch benchmark return from DuckDB
                benchmark_return = self._get_benchmark_return(
                    instrument_id, 
                    str(row['created_at']), 
                    str(row['completed_at'])
                )
                
                alpha = actual_outcome_pnl - benchmark_return
                
                # Generate Lesson via Agent
                reflection_prompt = (
                    f"Trade for {instrument_id}:\n"
                    f"Recommendation: {row['recommendation']}\n"
                    f"Original Reasoning: {row['reasoning']}\n"
                    f"Actual PnL: {actual_outcome_pnl*100:.2f}%\n"
                    f"Benchmark Return: {benchmark_return*100:.2f}%\n"
                    f"Alpha: {alpha*100:.2f}%\n\n"
                    "What is the key lesson learned here?"
                )
                
                lesson = (await REVIEWER.run(self.gateway, reflection_prompt)).output

                # Generate Embedding for the lesson
                try:
                    (embedding_vector,) = await self.gateway.embed([lesson])
                    embedding_str = f"[{','.join(map(str, embedding_vector))}]"
                except Exception as e:
                    logger.error(f"Failed to generate embedding: {e}")
                    embedding_str = None

                outcome_tag = "WIN" if actual_outcome_pnl > 0 else "LOSS" if actual_outcome_pnl < 0 else "FLAT"

                # Save to Postgres
                await conn.execute(
                    """
                    INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, lessons_learned, embedding, instrument_type, outcome)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    run_id, actual_outcome_pnl, alpha, lesson, embedding_str, instrument_id, outcome_tag
                )
                logger.info(f"Generated reflection for run {run_id}: Alpha {alpha*100:.2f}%")
