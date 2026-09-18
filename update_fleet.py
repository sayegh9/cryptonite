with open("packages/agents/cryptonite_agents/fleet.py", "r") as f:
    code = f.read()

hunters = """
def get_opportunity_hunter_agent() -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        system_instructions=(
            "You are the Opportunity Hunter agent. You continuously scan live market data "
            "and suggest trading opportunities based on price action and technical indicators."
        ),
        tools=[query_postgres_readonly, query_parquet_duckdb],
    )
    return Agent(config=cfg)

def get_strategy_generator_agent() -> Agent:
    cfg = LocalAgentConfig(
        api_key=api_key,
        system_instructions=(
            "You are an elite Nautilus Trader strategy developer. "
            "The user will describe a trading strategy idea. "
            "You must output ONLY valid, executable Python code that defines a class inheriting from "
            "`nautilus_trader.core.Strategy`. "
            "Do not include markdown blocks, just the raw code. "
            "Implement on_start, on_bar, on_trade, and order management."
        ),
        tools=[],
    )
    return Agent(config=cfg)
"""

code = code + "\n" + hunters

with open("packages/agents/cryptonite_agents/fleet.py", "w") as f:
    f.write(code)
