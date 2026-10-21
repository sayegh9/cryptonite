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
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from cryptonite_agents.brain.widgets import (
    MAX_ALLOCATION_PCT,
    MIN_ALLOCATION_PCT,
    OrderProposal,
)
from cryptonite_agents.gateway import RiskClass, tool
from cryptonite_agents.research.tools import QUEUE_RESEARCH
from cryptonite_agents.tools import (
    AI_STATE,
    COMPARE_LIVE_VS_BACKTEST,
    DERIVATIVES_CONTEXT,
    GET_KLINES,
    GRADE_SETUP,
    LIVE_MARKET_DATA,
    MARKET_REGIME,
    NEWS_SENTIMENT,
    QUERY_PARQUET,
    QUERY_POSTGRES,
    SCAN_SWING_SETUPS,
    SCANNER_CONTEXT,
    STAGE_DEBATE_PROPOSAL,
    TRADE_TIMELINE,
    read_nautilus_tearsheet,
)
from pydantic import Field


@tool(RiskClass.READ, name="show_price_chart")
async def show_price_chart(
    symbol: str,
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1h",
    bars: int = 96,
    entry: float | None = None,
    stop: float | None = None,
    target: float | None = None,
) -> dict[str, Any]:
    """Render an inline candlestick chart for the operator. Only for symbols the ingest feed records (get_klines reports in_feed). Optionally pass entry, stop, and target prices to overlay swing trade geometry lines. The UI renders the chart widget automatically; explain the levels in words and never output markdown image syntax ![...](...)."""
    bars = max(24, min(bars, 240))
    clean_symbol = symbol.strip().upper().split(".")[0]
    out: dict[str, Any] = {
        "rendered": "price_chart",
        "symbol": clean_symbol,
        "timeframe": timeframe,
        "bars": bars,
        "note": "Chart widget rendered inline below your message. Summarize the price action and levels in 1-2 sentences; do NOT output markdown image tags ![...](...).",
    }
    if entry is not None:
        out["entry"] = float(entry)
    if stop is not None:
        out["stop"] = float(stop)
    if target is not None:
        out["target"] = float(target)
    return out


@tool(RiskClass.READ, name="explain_backtest")
def explain_backtest(backtest_id: str = "latest") -> str:
    """Read the tearsheet (return, drawdown, trades, win rate) of a backtest by id, or 'latest'.
    The operator sees the tearsheet as a card; explain Sharpe, drawdown and profitability in words."""
    return read_nautilus_tearsheet(backtest_id)


# The shape the model sees in the schema. It is widgets.INSTRUMENT_ID with the exchange optional: the bare "SOLUSDT"
# is a valid proposal (OrderProposal adds ".BINANCE" after this check), and INSTRUMENT_ID would refuse it here. The
# platform check still runs on the normalised id in OrderProposal.
TOOL_INSTRUMENT_ID = r"^[A-Za-z0-9]{2,20}(-PERP)?(\.[A-Za-z0-9]{2,16})?$"


class EvidenceArg(TypedDict):
    """One evidence item as the model sends it. A plain dict, so the recorded call args stay JSON (a pydantic
    model would be stored as its repr). The limits match ``widgets.Evidence``, which validates the card."""

    source_tool: Annotated[str, Field(max_length=40)]
    field: Annotated[str, Field(max_length=60)]
    value: Annotated[str, Field(max_length=120)]
    as_of: NotRequired[Annotated[str | None, Field(max_length=40)]]


@tool(RiskClass.ADD_RISK, name="propose_order")
def propose_order(
    instrument_id: Annotated[str, Field(min_length=3, max_length=64, pattern=TOOL_INSTRUMENT_ID)],
    side: Literal["BUY", "SELL"],
    allocation_pct: Annotated[float, Field(ge=MIN_ALLOCATION_PCT, le=MAX_ALLOCATION_PCT)],
    order_type: Literal["MARKET", "LIMIT"] = "MARKET",
    limit_price: Annotated[float | None, Field(gt=0)] = None,
    stop_loss: Annotated[float | None, Field(gt=0)] = None,
    take_profit: Annotated[float | None, Field(gt=0)] = None,
    reduce_pct: Literal[25, 50, 100] | None = None,
    leverage: Annotated[int | None, Field(ge=1, le=10)] = None,
    rationale: Annotated[str, Field(max_length=2000)] = "",
    thesis: Annotated[str | None, Field(max_length=300)] = None,
    evidence: Annotated[list[EvidenceArg], Field(max_length=5)] = [],
    invalidation: Annotated[str | None, Field(max_length=300)] = None,
    horizon: Literal["1h", "4h", "24h", "3d", "7d"] | None = None,
    confidence: Annotated[float | None, Field(ge=0, le=1)] = None,
) -> str:
    """Propose a PAPER order to the operator as an order card. You cannot place orders: the operator
    approves or rejects the card. If no active mandate authorizes the instrument yet, propose anyway: the operator's card creates the mandate inline on approval. instrument_id like 'BTCUSDT.BINANCE'; side BUY or SELL;
    allocation_pct is the share of the mandate's budget (1 to 25): the approved order is sized from it at
    the live price and capped at the room left under the mandate's position limit; order_type MARKET or LIMIT
    (LIMIT needs limit_price); for SELL, reduce_pct (25, 50, or 100) scales out a portion of the mandate's holding
    (e.g., 50 for taking half profit at TP1); give a short rationale.
    Perps (instrument like 'BTCUSDT-PERP.BINANCE', only in a mandate that allows perps): BUY opens a long, SELL opens
    a short; allocation_pct is the margin posted; stop_loss (a structural level) and take_profit are required; leverage
    (1-10) is optional: the card's dry run caps it at the lowest ceiling and fills in the highest allowed when omitted.
    Give thesis, evidence (each item: the tool it came from, the field, the value, and its as_of time),
    invalidation (what would prove the idea wrong), horizon and confidence (0-1)."""
    proposal = OrderProposal(
        instrument_id=instrument_id, side=side, allocation_pct=allocation_pct,
        order_type=order_type, limit_price=limit_price, stop_loss=stop_loss, take_profit=take_profit,
        reduce_pct=reduce_pct, leverage=leverage,
        rationale=rationale, thesis=thesis, evidence=evidence, invalidation=invalidation, horizon=horizon,
        confidence=confidence,
    )
    return json.dumps({"status": "PENDING_APPROVAL", "proposal": proposal.model_dump()})


BRAIN_TOOLS = (
    LIVE_MARKET_DATA,
    GET_KLINES,
    NEWS_SENTIMENT,
    DERIVATIVES_CONTEXT,
    MARKET_REGIME,
    GRADE_SETUP,
    SCANNER_CONTEXT,
    AI_STATE,
    TRADE_TIMELINE,
    QUERY_POSTGRES,
    QUERY_PARQUET,
    COMPARE_LIVE_VS_BACKTEST,
    SCAN_SWING_SETUPS,
    show_price_chart,
    explain_backtest,
    STAGE_DEBATE_PROPOSAL,
    QUEUE_RESEARCH,
    propose_order,
)
