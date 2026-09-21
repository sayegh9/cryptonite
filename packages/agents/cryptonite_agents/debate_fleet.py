import logging
import os
from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.types import CapabilitiesConfig
import google.antigravity.hooks.policy as pol

# Tools
from cryptonite_agents.tools import query_postgres_readonly, query_parquet_duckdb

logger = logging.getLogger(__name__)
api_key = os.getenv("GEMINI_API_KEY")

def _base_cfg(role_prompt: str, model: str = "gemini-2.5-flash") -> LocalAgentConfig:
    return LocalAgentConfig(
        api_key=api_key,
        capabilities=CapabilitiesConfig(enabled_tools=[], enable_subagents=False),
        policies=[pol.allow_all()],
        system_instructions=role_prompt,
        tools=[query_postgres_readonly, query_parquet_duckdb],
        model_name=model,
    )

def get_market_analyst() -> Agent:
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the Market Analyst for Cryptonite. Your role is to analyze "
            "technical price action, volume profiles, and quantitative momentum. "
            "Output concise observations without making a final buy/sell call."
        ),
        model="gemini-2.5-flash"
    ))

def get_sentiment_analyst() -> Agent:
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the Sentiment & News Analyst for Cryptonite. Your role is to synthesize "
            "recent crypto news and social sentiment to identify catalysts."
        ),
        model="gemini-2.5-flash"
    ))

def get_onchain_analyst() -> Agent:
    # We would add an on-chain tool here, but for now we'll mock it if not present.
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the On-Chain Analyst for Cryptonite. You use RPC data (like whale movements, "
            "exchange inflows/outflows) to gauge fundamental crypto network health."
        ),
        model="gemini-2.5-flash"
    ))

def get_bull_debater() -> Agent:
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the Bull Researcher. Your goal is to aggressively argue FOR taking a LONG "
            "position based on the available market data. Ignore bearish signals and highlight upside potential. "
            "Debate fiercely but stay grounded in the provided data."
        ),
        model="gemini-2.5-flash"
    ))

def get_bear_debater() -> Agent:
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the Bear Researcher. Your goal is to aggressively argue AGAINST taking a LONG "
            "position and advocate for SHORT or cash based on the available market data. Highlight risks. "
            "Debate fiercely but stay grounded in the provided data."
        ),
        model="gemini-2.5-flash"
    ))

def get_research_manager(dynamic_context: str = "") -> Agent:
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the Research Manager (Portfolio Manager proxy) at Cryptonite. "
            "You listen to the Bull and Bear debates, and the risk analysts. "
            "Your output MUST be a strict JSON object containing: "
            "{'recommendation': 'BUY' | 'HOLD' | 'SELL', 'conviction_score': float, 'allocation_pct': float, 'reasoning': string}. "
            "allocation_pct should not exceed 25.0.\n\n"
            f"HISTORICAL LESSONS LEARNED:\n{dynamic_context}"
        ),
        model="gemini-2.5-pro"
    ))

def get_aggressive_risk_agent() -> Agent:
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the Aggressive Risk Manager. You look at the Portfolio Manager's "
            "recommendation and the overall portfolio exposure. You always advocate for "
            "maximizing allocation and sizing up on high-conviction trades to maximize alpha. "
            "Argue your case for taking on more risk."
        ),
        model="gemini-2.5-flash"
    ))

def get_conservative_risk_agent() -> Agent:
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the Conservative Risk Manager. You look at the Portfolio Manager's "
            "recommendation and the overall portfolio exposure. You always advocate for "
            "capital preservation, tight stops, and scaling down sizes. "
            "Argue your case for reducing risk."
        ),
        model="gemini-2.5-flash"
    ))

def get_neutral_risk_agent() -> Agent:
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the Neutral Risk/Compliance Manager. You strictly evaluate the math, "
            "correlation with existing portfolio positions, and standard deviation. "
            "Provide an objective, volatility-adjusted sizing recommendation."
        ),
        model="gemini-2.5-flash"
    ))

def get_chief_risk_officer() -> Agent:
    return Agent(config=_base_cfg(
        role_prompt=(
            "You are the Chief Risk Officer (CRO). Listen to the Aggressive, Conservative, "
            "and Neutral risk managers regarding the PM's initial decision. "
            "Your output MUST be a strict JSON object overriding or confirming the decision: "
            "{'final_allocation_pct': float, 'conviction_level': 'Buy' | 'Overweight' | 'Hold' | 'Underweight' | 'Sell', 'risk_reasoning': string}. "
            "Ensure allocation_pct does NOT exceed 25.0."
        ),
        model="gemini-2.5-pro"
    ))
