import logging
import os

from google.antigravity import Agent, LocalAgentConfig
from cryptonite_agents.tools import query_postgres_readonly, query_parquet_duckdb, read_nautilus_tearsheet

logger = logging.getLogger(__name__)

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    logger.warning("GEMINI_API_KEY is not set! Agents will fail to authenticate.")

def get_research_agent() -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        system_instructions=(
            "You are a Senior Quantitative Analyst for the Cryptonite trading platform. "
            "You have access to PostgreSQL for live execution data and DuckDB for historical Parquet ticks. "
            "Always aggregate data using DuckDB rather than dumping raw rows into context. "
            "Answer the operator's analytical questions concisely."
        ),
        tools=[query_postgres_readonly, query_parquet_duckdb],
    )
    return Agent(config=cfg)

def get_explainer_agent() -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        system_instructions=(
            "You are a Risk Manager. You explain strategy performance. "
            "Use read_nautilus_tearsheet to pull backtest metrics and provide a natural language "
            "summary of the Sharpe Ratio, Max Drawdown, and profitability."
        ),
        tools=[read_nautilus_tearsheet],
    )
    return Agent(config=cfg)
