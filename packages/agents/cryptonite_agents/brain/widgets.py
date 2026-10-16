"""Typed inline widgets the Brain streams next to its text (blueprint §7.1 Brain Home).

Widgets are derived from tool calls, never from free text, so a widget always matches a call that
passed the permission gate and is recorded in the decision trace.
"""

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger("cryptonite.agents.brain.widgets")

MIN_ALLOCATION_PCT, MAX_ALLOCATION_PCT = 1.0, 25.0  # mirrors the dispatcher's invariant equity clamp
# A platform instrument (BTCUSDT.BINANCE, BTCUSDT-PERP.BINANCE). Checked after the normaliser in OrderProposal,
# so a value the server echoes into a system event (the reject line) cannot carry a line or a tag.
INSTRUMENT_ID = r"^[A-Z0-9]{2,20}(-PERP)?\.[A-Z0-9]{2,16}$"
_MSG_ID = re.compile(r"Msg ID: (\S+)\)")  # the staging tool's reply: "(Msg ID: <stream id>)"
_STREAM_ID = re.compile(r"\d+-\d+")  # a Redis stream id, as the trigger returns it


class PriceChartWidget(BaseModel):
    kind: Literal["price_chart"] = "price_chart"
    id: str
    symbol: str
    timeframe: str = "1h"


class TearsheetWidget(BaseModel):
    kind: Literal["tearsheet"] = "tearsheet"
    id: str
    backtest_id: str
    metrics: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """One fact a proposal rests on: the tool it came from, the field, its value and the time it describes.

    The model wrote it: it is shown on the card as plain text, never as markup.
    """

    source_tool: str = Field(max_length=40)
    field: str = Field(max_length=60)
    value: str = Field(max_length=120)
    as_of: str | None = Field(default=None, max_length=40)


class OrderProposal(BaseModel):
    """What ``propose_order`` may stage. The card is a proposal; the dispatcher still decides."""

    instrument_id: str = Field(min_length=3, max_length=64, pattern=INSTRUMENT_ID)
    side: Literal["BUY", "SELL"]
    # Optional and display-free: an approved order is sized from allocation_pct (a share of the mandate's
    # budget) at the live price, capped at the room left under the mandate's position limit
    quantity: float | None = Field(default=None, gt=0)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: float | None = Field(default=None, gt=0)
    allocation_pct: float = Field(ge=MIN_ALLOCATION_PCT, le=MAX_ALLOCATION_PCT)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    rationale: str = Field(default="", max_length=2000)
    # Optional structured rationale: a card made before these existed still validates and renders without them
    thesis: str | None = Field(default=None, max_length=300)
    evidence: list[Evidence] = Field(default_factory=list, max_length=5)
    invalidation: str | None = Field(default=None, max_length=300)
    horizon: Literal["1h", "4h", "24h", "3d", "7d"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("side", "order_type", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @field_validator("instrument_id", mode="before")
    @classmethod
    def _instrument(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        v = v.strip().upper()
        return v if "." in v else f"{v}.BINANCE"

    @model_validator(mode="after")
    def _limit_needs_price(self) -> "OrderProposal":
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT orders need a limit_price")
        return self


class CardLesson(BaseModel):
    """A past lesson on an order card. ``lesson_text`` was written by a model: it is shown as quoted data."""

    lesson_text: str
    outcome: str | None = None
    alpha_pct: float | None = None
    created_at: str


class OrderCardWidget(OrderProposal):
    kind: Literal["order_card"] = "order_card"
    id: str
    risk_class: Literal["ADD_RISK"] = "ADD_RISK"
    status: Literal["PENDING_APPROVAL", "APPROVED", "REJECTED", "FAILED"] = "PENDING_APPROVAL"
    trace_id: str | None = None
    command_id: str | None = None
    # Why ingest does not feed the symbol (an ``IngestUniverse.refusal`` line), or None when it does
    feed_refusal: str | None = None
    # The instrument's past lessons, attached by the Brain from the reader alone (lessons.py); never from the model
    lessons: list[CardLesson] = Field(default_factory=list, max_length=3)


class DebateVerdict(BaseModel):
    """A debate's decision, read from its run (the card shows it; the model never wrote it)."""

    recommendation: str
    conviction_score: float | None = None
    allocation_pct: float | None = None
    approval_status: str | None = None  # the debate's approval, or None when it has none


class DebateSummaryWidget(BaseModel):
    kind: Literal["debate_summary"] = "debate_summary"
    id: str
    instrument_id: str = Field(pattern=INSTRUMENT_ID)
    timeframe: str = "1m"
    recommendation: str | None = None
    conviction_score: float | None = None
    status: Literal["QUEUED", "RUNNING", "VERDICT", "NO_TRADE", "GATE_REFUSED", "FAILED"] = "QUEUED"
    message: str = ""
    # The queue message the staging tool returned ("Msg ID"): the card follows the debate run recorded under it
    message_ref: str | None = None
    run_id: str | None = None
    verdict: DebateVerdict | None = None
    reason: str | None = None  # why it did not trade, or was not run (a code from the run, never model text)

    @field_validator("instrument_id", mode="before")
    @classmethod
    def _instrument(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        v = v.strip().upper()
        return v if "." in v else f"{v}.BINANCE"  # the same canonical form the debate trigger queues under


def _parse_json(content: str | None) -> dict[str, Any] | None:
    if not content:
        return None
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def widget_for_tool(
    tool: str,
    args: dict[str, Any] | None,
    *,
    call_id: str,
    status: str = "OK",
    content: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any] | None:
    """The widget a tool call renders as, or None. Invalid arguments never become a widget."""
    args = args or {}
    try:
        if tool == "show_price_chart":
            return PriceChartWidget(id=call_id, symbol=str(args.get("symbol", "BTCUSDT")).upper(),
                                    timeframe=str(args.get("timeframe") or "1h")).model_dump()
        if tool in ("explain_backtest", "read_nautilus_tearsheet") and status == "OK":
            metrics = _parse_json(content) or {}
            backtest_id = str(args.get("backtest_id") or metrics.get("backtest_id") or "latest")
            return TearsheetWidget(id=call_id, backtest_id=backtest_id, metrics=metrics).model_dump()
        if tool == "propose_order":
            # The gate holds ADD_RISK for approval (status DENIED): that is exactly when the card shows
            # A ``lessons`` argument from the model is dropped here: the Brain sets the field from the reader alone
            card = OrderCardWidget(id=call_id, trace_id=trace_id,
                                   **{k: v for k, v in args.items() if k != "lessons"})
            return card.model_dump()
        if tool == "stage_debate_proposal" and status == "OK":
            queued = bool(content) and content.startswith("Success")
            found = _MSG_ID.search(content or "")
            message_ref = found.group(1) if queued and found and _STREAM_ID.fullmatch(found.group(1)) else None
            return DebateSummaryWidget(
                id=call_id, instrument_id=str(args.get("instrument_id", "")),
                timeframe=str(args.get("timeframe") or "1m"),
                recommendation=args.get("recommendation"), conviction_score=args.get("conviction_score"),
                status="QUEUED" if queued else "FAILED", message=(content or "")[:500], message_ref=message_ref,
            ).model_dump()
    except (ValidationError, ValueError, TypeError) as e:
        logger.warning("Tool %s produced no widget (invalid arguments): %s", tool, e)
    return None


def describe_widget(widget: dict[str, Any]) -> str:
    """A one-line transcript form, so later turns (and summaries) remember what was shown."""
    kind = widget.get("kind")
    if kind == "order_card":
        return (f"[order card {widget.get('id')}: {widget.get('side')} {widget.get('instrument_id')} "
                f"{widget.get('order_type')} ({widget.get('allocation_pct')}% of the mandate), status {widget.get('status')}"
                + (", not in the live feed" if widget.get("feed_refusal") else "")
                + (f", paper command {widget['command_id']}" if widget.get("command_id") else "") + "]")
    if kind == "price_chart":
        return f"[price chart {widget.get('symbol')} {widget.get('timeframe')}]"
    if kind == "tearsheet":
        return f"[tearsheet for backtest {widget.get('backtest_id')}]"
    if kind == "debate_summary":
        verdict = widget.get("verdict")
        return (f"[debate {str(widget.get('status', '')).lower()} for {widget.get('instrument_id')}"
                + (f", verdict {verdict['recommendation']}" if verdict else "") + "]")
    return f"[{kind}]"
