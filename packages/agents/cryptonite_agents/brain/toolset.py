"""The Brain's tool set (blueprint §4.2, A3.2): the old chat personas become tools.

Market data, research SQL/Parquet and debate staging reuse the registered tools from
``cryptonite_agents.tools``. The Brain adds three of its own:

- ``show_price_chart`` (READ): renders a price chart widget.
- ``explain_backtest`` (READ): reads a backtest tearsheet and renders it.
- ``propose_order`` (ADD_RISK): stages an order card for the operator. The permission gate holds it
  (ADD_RISK needs approval), and even an approved call only returns the proposal: the Brain has no
  path to an order. The operator's Approve button sends the card to the existing paper manual-order
  API (``POST /api/v1/paper/orders``), which enqueues a command for GuardedOrderDispatcher.
"""

import json

from cryptonite_agents.brain.widgets import OrderProposal
from cryptonite_agents.gateway import RiskClass, tool
from cryptonite_agents.tools import (
    COMPARE_LIVE_VS_BACKTEST,
    LIVE_MARKET_DATA,
    QUERY_PARQUET,
    QUERY_POSTGRES,
    SCANNER_CONTEXT,
    STAGE_DEBATE_PROPOSAL,
    read_nautilus_tearsheet,
)


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
    instrument_id: str,
    side: str,
    quantity: float,
    allocation_pct: float,
    order_type: str = "MARKET",
    limit_price: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    rationale: str = "",
) -> str:
    """Propose a PAPER order to the operator as an order card. You cannot place orders: the operator
    approves or rejects the card. instrument_id like 'BTCUSDT.BINANCE'; side BUY or SELL; quantity in
    base units; allocation_pct is the share of paper equity (1 to 25); order_type MARKET or LIMIT
    (LIMIT needs limit_price); give a short rationale with the stop and the invalidation."""
    proposal = OrderProposal(
        instrument_id=instrument_id, side=side, quantity=quantity, allocation_pct=allocation_pct,
        order_type=order_type, limit_price=limit_price, stop_loss=stop_loss, take_profit=take_profit,
        rationale=rationale,
    )
    return json.dumps({"status": "PENDING_APPROVAL", "proposal": proposal.model_dump()})


BRAIN_TOOLS = (
    LIVE_MARKET_DATA,
    SCANNER_CONTEXT,
    QUERY_POSTGRES,
    QUERY_PARQUET,
    COMPARE_LIVE_VS_BACKTEST,
    show_price_chart,
    explain_backtest,
    STAGE_DEBATE_PROPOSAL,
    propose_order,
)
