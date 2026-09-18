import logging
import os

from google.antigravity import Agent, LocalAgentConfig
from cryptonite_agents.tools import query_postgres_readonly, query_parquet_duckdb, read_nautilus_tearsheet

logger = logging.getLogger(__name__)

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    logger.warning("GEMINI_API_KEY is not set! Agents will fail to authenticate.")

def get_research_agent(config_override: dict | None = None) -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        system_instructions=(config_override or {}).get("system_instructions") or (
            "You are a Senior Quantitative Analyst for the Cryptonite trading platform. "
            "You have access to PostgreSQL for live execution data and DuckDB for historical Parquet ticks. "
            "Always aggregate data using DuckDB rather than dumping raw rows into context. "
            "Answer the operator's analytical questions concisely."
        ),
        tools=[query_postgres_readonly, query_parquet_duckdb],
        model_name=(config_override or {}).get("model_name"),
        temperature=(config_override or {}).get("temperature"),
    )
    return Agent(config=cfg)

def get_explainer_agent(config_override: dict | None = None) -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        system_instructions=(config_override or {}).get("system_instructions") or (
            "You are a Risk Manager. You explain strategy performance. "
            "Use read_nautilus_tearsheet to pull backtest metrics and provide a natural language "
            "summary of the Sharpe Ratio, Max Drawdown, and profitability."
        ),
        tools=[read_nautilus_tearsheet],
        model_name=(config_override or {}).get("model_name"),
        temperature=(config_override or {}).get("temperature"),
    )
    return Agent(config=cfg)

def get_strategist_agent(custom_instructions: str | None = None, config_override: dict | None = None) -> Agent:
    from cryptonite_agents.tools import run_execution_analysis
    default_prompt = (
        "You are the Execution_Strategist_Agent, an institutional-grade quantitative analyst. "
        "Your primary directive is to protect paper and live capital by identifying high-probability setups."
    )
    
    cfg = LocalAgentConfig(
        api_key=api_key,
        system_instructions=custom_instructions or (config_override or {}).get("system_instructions") or default_prompt,
        tools=[run_execution_analysis],
        model_name=(config_override or {}).get("model_name"),
        temperature=(config_override or {}).get("temperature"),
    )
    return Agent(config=cfg)


def get_opportunity_hunter_agent(config_override: dict | None = None) -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        system_instructions=(config_override or {}).get("system_instructions") or (
            "You are the Opportunity Hunter agent. You continuously scan live market data "
            "and suggest trading opportunities based on price action and technical indicators."
        ),
        tools=[query_postgres_readonly, query_parquet_duckdb],
        model_name=(config_override or {}).get("model_name"),
        temperature=(config_override or {}).get("temperature"),
    )
    return Agent(config=cfg)

def get_strategy_generator_agent(config_override: dict | None = None) -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        system_instructions=(config_override or {}).get("system_instructions") or (
            "You are an elite Nautilus Trader strategy developer. "
            "The user will describe a trading strategy idea. "
            "You must output ONLY valid, executable Python code. Do not include markdown blocks. "
            "Your code must define two classes:\n"
            "1. A config class inheriting from `nautilus_trader.config.StrategyConfig`\n"
            "2. A strategy class inheriting from `nautilus_trader.core.Strategy`.\n"
            "The config class MUST have fields: instrument_id: str, bar_type: str, evaluation_start_ns: int, plus any custom parameters."
        ),
        tools=[],
        model_name=(config_override or {}).get("model_name"),
        temperature=(config_override or {}).get("temperature"),
    )
    return Agent(config=cfg)
