import logging
import os

from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.types import CapabilitiesConfig
import google.antigravity.hooks.policy as pol
from cryptonite_agents.tools import query_postgres_readonly, query_parquet_duckdb, read_nautilus_tearsheet

logger = logging.getLogger(__name__)

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    logger.warning("GEMINI_API_KEY is not set! Agents will fail to authenticate.")

def get_research_agent(config_override: dict | None = None) -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        capabilities=CapabilitiesConfig(enabled_tools=[], enable_subagents=False),
        policies=[pol.allow_all()],
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
        capabilities=CapabilitiesConfig(enabled_tools=[], enable_subagents=False),
        policies=[pol.allow_all()],
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
        capabilities=CapabilitiesConfig(enabled_tools=[], enable_subagents=False),
        policies=[pol.allow_all()],
        system_instructions=custom_instructions or (config_override or {}).get("system_instructions") or default_prompt,
        tools=[run_execution_analysis],
        model_name=(config_override or {}).get("model_name"),
        temperature=(config_override or {}).get("temperature"),
    )
    return Agent(config=cfg)


def get_opportunity_hunter_agent(config_override: dict | None = None) -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        capabilities=CapabilitiesConfig(enabled_tools=[], enable_subagents=False),
        policies=[pol.allow_all()],
        system_instructions=(config_override or {}).get("system_instructions") or (
            "You are the Opportunity Hunter agent. You listen for Pub/Sub Candidate Alerts "
            "emitted by the Polars scanner. When an alert is received, evaluate the setup "
            "based on price action and technical indicators before suggesting trading opportunities. "
            "Do not continuously scan the market via tick-loops."
        ),
        tools=[query_postgres_readonly, query_parquet_duckdb],
        model_name=(config_override or {}).get("model_name"),
        temperature=(config_override or {}).get("temperature"),
    )
    return Agent(config=cfg)

def get_strategy_generator_agent(config_override: dict | None = None) -> Agent:
    default_prompt = (
        "You are an elite Nautilus Trader strategy developer for the Cryptonite platform. "
        "The user will describe a trading strategy idea. "
        "You must output ONLY valid, executable Python code. Do not include markdown blocks or introductory text. "
        "Your code MUST import and inherit from Cryptonite base classes:\n"
        "from cryptonite_strategies.base import CryptoniteStrategy, CryptoniteStrategyConfig\n\n"
        "Implementation rules:\n"
        "1. Define a config class inheriting from `CryptoniteStrategyConfig`.\n"
        "   In its `__init__`, call `super().__init__(instrument_id=instrument_id, bar_type=bar_type, trade_size=trade_size, evaluation_start_ns=evaluation_start_ns, **kwargs)`\n"
        "2. Define a strategy class inheriting from `CryptoniteStrategy`.\n"
        "3. In `on_start(self)`: call `super().on_start()` and register indicators.\n"
        "4. In `on_bar(self, bar: Bar)`: execute logic using helper methods: `self.buy_market()`, `self.sell_market()`, `self.close_all()`, `self.is_flat()`, `self.is_long()`, `self.is_short()`.\n"
        "5. STRICT AST COMPLIANCE: NEVER call `self.submit_order()`, `self.submit_order_list()`, or `self.cancel_order()` directly. NEVER access `self.portfolio`, `self.msgbus`, or `self.exec_engine` directly. All orders and position queries must exclusively use the CryptoniteStrategy helper methods."
    )
    cfg = LocalAgentConfig(
        api_key=api_key,
        capabilities=CapabilitiesConfig(enabled_tools=[], enable_subagents=False),
        policies=[pol.allow_all()],
        system_instructions=(config_override or {}).get("system_instructions") or default_prompt,
        tools=[],
        model_name=(config_override or {}).get("model_name"),
        temperature=(config_override or {}).get("temperature"),
    )
    return Agent(config=cfg)
