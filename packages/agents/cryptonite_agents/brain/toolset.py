"""The Brain's tool set (blueprint §4.2, A3.2): the old chat personas become tools.

Market data, research SQL/Parquet and debate staging reuse the registered tools from
``cryptonite_agents.tools``. The Brain adds three of its own:

- ``show_price_chart`` (READ): renders a price chart widget.
- ``explain_backtest`` (READ): reads a backtest tearsheet and renders it.
- ``propose_order`` (ADD_RISK): stages an order card for the operator. The permission gate holds it
  (ADD_RISK needs approval), and even an approved call only returns the proposal: the Brain has no
  path to an order. The operator's Approve button calls the API's card-approve endpoint, which queues
  one PAPER order for GuardedOrderDispatcher (see ``cryptonite_api.routes.brain``).
"""

import json
from typing import Annotated, Literal

from cryptonite_agents.brain.widgets import MAX_ALLOCATION_PCT, MIN_ALLOCATION_PCT, OrderProposal
from cryptonite_agents.gateway import RiskClass, tool
from cryptonite_agents.research.tools import QUEUE_RESEARCH
from cryptonite_agents.tools import (
    COMPARE_LIVE_VS_BACKTEST,
    GET_KLINES,
    LIVE_MARKET_DATA,
    QUERY_PARQUET,
    QUERY_POSTGRES,
    SCANNER_CONTEXT,
    STAGE_DEBATE_PROPOSAL,
    read_nautilus_tearsheet,
)
from pydantic import Field


@tool(RiskClass.READ, name="show_price_chart")
def show_price_chart(symbol: str, timeframe: str = "1h") -> str:
    """Show the operator an inline price chart for a symbol (e.g. 'BTCUSDT') and timeframe
    ('1m', '5m', '15m', '1h', '4h', '1d'). Use it whenever you discuss price action."""
    return f"Chart of {symbol.upper()} {timeframe} is shown to the operator."


@tool(RiskClass.READ, name="explain_backtest")
def explain_backtest(backtest_id: str = "latest") -> str:
    """Read the tearsheet (return, drawdown, trades, win rate) of a backtest by id, or 'latest'.
    The operator sees the tearsheet as a card; explain Sharpe, drawdown and profitability in words."""
    return read_nautilus_tearsheet(backtest_id)


@tool(RiskClass.ADD_RISK, name="propose_order")
def propose_order(
    instrument_id: Annotated[str, Field(min_length=3, max_length=64)],
    side: Literal["BUY", "SELL"],
    allocation_pct: Annotated[float, Field(ge=MIN_ALLOCATION_PCT, le=MAX_ALLOCATION_PCT)],
    order_type: Literal["MARKET", "LIMIT"] = "MARKET",
    limit_price: Annotated[float | None, Field(gt=0)] = None,
    stop_loss: Annotated[float | None, Field(gt=0)] = None,
    take_profit: Annotated[float | None, Field(gt=0)] = None,
    rationale: Annotated[str, Field(max_length=2000)] = "",
) -> str:
    """Propose a PAPER order to the operator as an order card. You cannot place orders: the operator
    approves or rejects the card. instrument_id like 'BTCUSDT.BINANCE'; side BUY or SELL;
    allocation_pct is the share of the mandate's budget (1 to 25): the approved order is sized from it at
    the live price and capped at the room left under the mandate's position limit; order_type MARKET or LIMIT
    (LIMIT needs limit_price); give a short rationale with the stop and the invalidation."""
    proposal = OrderProposal(
        instrument_id=instrument_id, side=side, allocation_pct=allocation_pct,
        order_type=order_type, limit_price=limit_price, stop_loss=stop_loss, take_profit=take_profit,
        rationale=rationale,
    )
    return json.dumps({"status": "PENDING_APPROVAL", "proposal": proposal.model_dump()})


BRAIN_TOOLS = (
    LIVE_MARKET_DATA,
    GET_KLINES,
    SCANNER_CONTEXT,
    QUERY_POSTGRES,
    QUERY_PARQUET,
    COMPARE_LIVE_VS_BACKTEST,
    show_price_chart,
    explain_backtest,
    STAGE_DEBATE_PROPOSAL,
    QUEUE_RESEARCH,
    propose_order,
)
