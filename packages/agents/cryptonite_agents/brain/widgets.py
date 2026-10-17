"""Typed inline widgets the Brain streams next to its text (blueprint §7.1 Brain Home).

Widgets are derived from tool calls, never from free text, so a widget always matches a call that
passed the permission gate and is recorded in the decision trace.
"""

import json
import logging
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
    bars: int = 96
    entry: float | None = None
    stop: float | None = None
    target: float | None = None


class SwingCandidateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: str
    preset_slug: str
    timeframe: str
    direction: Literal["LONG", "SHORT"] = "LONG"
    score: float
    entry: float
    stop: float
    target: float
    rr_ratio: float
    trend_4h: str = "FLAT"
    trend_1h: str = "FLAT"
    trend_15m: str = "FLAT"
    matched_rules: list[str] = Field(default_factory=list)


class SwingRadarWidget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["swing_radar"] = "swing_radar"
    id: str | None = None
    preset_slug: str = "all"
    timeframe: str = "1h"
    scanned_count: int = 0
    candidates: list[SwingCandidateItem] = Field(default_factory=list)


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
    reduce_pct: Literal[25, 50, 100] | None = Field(default=None)
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
    # UNTRACKED: a card queued before Wave 3 has no message id, so it has no run to follow (not a failure)
    status: Literal["QUEUED", "RUNNING", "VERDICT", "NO_TRADE", "GATE_REFUSED", "FAILED", "UNTRACKED"] = "QUEUED"
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


BrainWidget = Annotated[
    PriceChartWidget | TearsheetWidget | OrderCardWidget | DebateSummaryWidget | SwingRadarWidget,
    Field(discriminator="kind"),
]


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_json(content: str | None) -> dict[str, Any] | None:
    if not content:
        return None
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def widget_for_tool(
    tool: str | None = None,
    args: dict[str, Any] | None = None,
    *,
    call_id: str = "",
    status: str = "OK",
    content: str | None = None,
    trace_id: str | None = None,
    return_content: str | None = None,
    tool_name: str | None = None,
) -> dict[str, Any] | None:
    """The widget a tool call renders as, or None. Invalid arguments never become a widget."""
    active_tool = tool or tool_name or ""
    active_content = content if content is not None else return_content
    args = args or {}
    try:
        if active_tool == "show_price_chart":
            ret = _parse_json(active_content) if active_content else {}
            bars_val = args.get("bars") if args.get("bars") is not None else (ret.get("bars") if isinstance(ret, dict) else None)
            entry_val = args.get("entry") if args.get("entry") is not None else (ret.get("entry") if isinstance(ret, dict) else None)
            stop_val = args.get("stop") if args.get("stop") is not None else (ret.get("stop") if isinstance(ret, dict) else None)
            target_val = args.get("target") if args.get("target") is not None else (ret.get("target") if isinstance(ret, dict) else None)
            return PriceChartWidget(
                id=call_id,
                symbol=str(args.get("symbol") or (ret.get("symbol") if isinstance(ret, dict) else None) or "BTCUSDT").upper(),
                timeframe=str(args.get("timeframe") or (ret.get("timeframe") if isinstance(ret, dict) else None) or "1h"),
                bars=int(bars_val or 96),
                entry=_opt_float(entry_val),
                stop=_opt_float(stop_val),
                target=_opt_float(target_val),
            ).model_dump()
        if active_tool in ("explain_backtest", "read_nautilus_tearsheet") and status == "OK":
            metrics = _parse_json(active_content) or {}
            backtest_id = str(args.get("backtest_id") or metrics.get("backtest_id") or "latest")
            return TearsheetWidget(id=call_id, backtest_id=backtest_id, metrics=metrics).model_dump()
        if active_tool == "propose_order":
            # The gate holds ADD_RISK for approval (status DENIED): that is exactly when the card shows
            # A ``lessons`` argument from the model is dropped here: the Brain sets the field from the reader alone
            card = OrderCardWidget(id=call_id, trace_id=trace_id,
                                   **{k: v for k, v in args.items() if k != "lessons"})
            return card.model_dump()
        if active_tool == "stage_debate_proposal" and status == "OK":
            queued = bool(active_content) and active_content.startswith("Success")
            found = _MSG_ID.search(active_content or "")
            message_ref = found.group(1) if queued and found and _STREAM_ID.fullmatch(found.group(1)) else None
            return DebateSummaryWidget(
                id=call_id, instrument_id=str(args.get("instrument_id", "")),
                timeframe=str(args.get("timeframe") or "1m"),
                recommendation=args.get("recommendation"), conviction_score=args.get("conviction_score"),
                status="QUEUED" if queued else "FAILED", message=(active_content or "")[:500], message_ref=message_ref,
            ).model_dump()
        if active_tool == "scan_swing_setups":
            data = _parse_json(active_content)
            if isinstance(data, dict) and isinstance(data.get("candidates"), list):
                candidates: list[SwingCandidateItem] = []
                for item in data.get("candidates", []):
                    if isinstance(item, dict):
                        candidates.append(SwingCandidateItem(**item))
                widget = SwingRadarWidget(
                    id=call_id or None,
                    preset_slug=str(data.get("preset_slug") or args.get("preset_slug") or "all"),
                    timeframe=str(data.get("timeframe") or args.get("timeframe") or "1h"),
                    scanned_count=int(data.get("scanned_count") if data.get("scanned_count") is not None else len(candidates)),
                    candidates=candidates,
                )
                return widget.model_dump()
    except (ValidationError, ValueError, TypeError) as e:
        logger.warning("Tool %s produced no widget (invalid arguments): %s", active_tool, e)
    return None


def describe_widget(widget: dict[str, Any]) -> str:
    """A one-line transcript form, so later turns (and summaries) remember what was shown."""
    kind = widget.get("kind")
    if kind == "order_card":
        reduce_desc = f", reduce {widget.get('reduce_pct')}% of holding" if widget.get("side") == "SELL" and widget.get("reduce_pct") in (25, 50) else ""
        return (f"[order card {widget.get('id')}: {widget.get('side')} {widget.get('instrument_id')} "
                f"{widget.get('order_type')} ({widget.get('allocation_pct')}% of the mandate{reduce_desc}), status {widget.get('status')}"
                + (", not in the live feed" if widget.get("feed_refusal") else "")
                + (f", paper command {widget['command_id']}" if widget.get("command_id") else "") + "]")
    if kind == "price_chart":
        bars = widget.get("bars", 96)
        if any(widget.get(k) is not None for k in ("entry", "stop", "target")):
            lvl_parts = []
            if widget.get("entry") is not None:
                lvl_parts.append(f"entry={widget.get('entry')}")
            if widget.get("stop") is not None:
                lvl_parts.append(f"stop={widget.get('stop')}")
            if widget.get("target") is not None:
                lvl_parts.append(f"target={widget.get('target')}")
            lvl_str = f" {', '.join(lvl_parts)}" if lvl_parts else ""
            return f"[widget:price_chart {widget.get('symbol')} {widget.get('timeframe')} ({bars} bars{lvl_str})]"
        return f"[price chart {widget.get('symbol')} {widget.get('timeframe')}]"
    if kind == "tearsheet":
        return f"[tearsheet for backtest {widget.get('backtest_id')}]"
    if kind == "debate_summary":
        verdict = widget.get("verdict")
        return (f"[debate {str(widget.get('status', '')).lower()} for {widget.get('instrument_id')}"
                + (f", verdict {verdict['recommendation']}" if verdict else "") + "]")
    if kind == "swing_radar":
        return f"[widget:swing_radar preset={widget.get('preset_slug')} candidates={len(widget.get('candidates') or [])}]"
    return f"[{kind}]"
