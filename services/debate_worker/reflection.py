import asyncio
import logging
import duckdb
import json
import os
import asyncpg
from decimal import Decimal
from typing import Optional

from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.types import CapabilitiesConfig
import google.antigravity.hooks.policy as pol

logger = logging.getLogger(__name__)
api_key = os.getenv("GEMINI_API_KEY")
NAUTILUS_CATALOG_PATH = os.getenv("NAUTILUS_CATALOG_PATH", "/app/data/nautilus_catalog")

def get_reflection_agent() -> Agent:
    return Agent(config=LocalAgentConfig(
        api_key=api_key,
        capabilities=CapabilitiesConfig(enabled_tools=[], enable_subagents=False),
        policies=[pol.allow_all()],
        system_instructions=(
            "You are the Quantitative Reviewer at Cryptonite. "
            "Your job is to analyze completed trades, their actual PnL, and their Alpha versus "
            "the Bitcoin/Ethereum benchmarks. Identify *why* a trade succeeded or failed. "
            "Output exactly one concise paragraph of 'lessons learned' that will be injected into "
            "the Portfolio Manager's prompt in the future."
        ),
        model_name="gemini-2.5-flash",
    ))

class ReflectionEngine:
    def __init__(self, pg_pool: asyncpg.Pool):
        self.pg_pool = pg_pool
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
                
                # In a real system, we'd query execution_fills to get the exact Entry and Exit prices.
                # Here we simulate the PnL outcome.
                actual_outcome_pnl = 0.05 # 5% mock win
                
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
                
                agent = get_reflection_agent()
                async with agent:
                    resp = await agent.chat(reflection_prompt)
                    lesson = await resp.text()
                
                # Save to Postgres
                await conn.execute(
                    """
                    INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, lessons_learned)
                    VALUES ($1, $2, $3, $4)
                    """,
                    run_id, actual_outcome_pnl, alpha, lesson
                )
                logger.info(f"Generated reflection for run {run_id}: Alpha {alpha*100:.2f}%")
