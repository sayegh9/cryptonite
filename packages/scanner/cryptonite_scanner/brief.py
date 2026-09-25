"""Market brief (AI-first blueprint §4.3): a compact, deterministic text summary of one instrument.

Built by the scanner from the indicator frames it already computes, once per candidate bar, and
shared by every agent in the debate instead of each one pulling raw rows (fewer tokens, one stable
view of the market). Target size: well under 2k tokens.
"""

from datetime import UTC, datetime
from typing import Any

import polars as pl

LEVEL_LOOKBACK_BARS = 50


def _f(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(a: Any, b: Any) -> float | None:
    try:
        a, b = float(a), float(b)
        return (a - b) / b * 100 if b else None
    except (TypeError, ValueError):
        return None


def _trend(row: dict[str, Any]) -> str:
    close, e20, e50, e200 = (row.get(k) for k in ("close", "ema_20", "ema_50", "ema_200"))
    if None in (close, e20, e50):
        return "unknown"
    if close > e20 > e50 and (e200 is None or e50 > e200):
        return "up"
    if close < e20 < e50 and (e200 is None or e50 < e200):
        return "down"
    return "mixed"


def timeframe_line(timeframe: str, df: pl.DataFrame) -> str | None:
    """One line of state for an indicator-enriched frame (``ScannerEngine.enrich_indicators``)."""
    if df.is_empty():
        return None
    row = df.tail(1).to_dicts()[0]
    window = df.tail(LEVEL_LOOKBACK_BARS)
    first_close = window.head(1)["close"].item()
    change = _pct(row.get("close"), first_close)
    atr_pct = _pct((row.get("close") or 0) + (row.get("atr") or 0), row.get("close")) if row.get("atr") else None
    bb = "n/a"
    if row.get("bb_upper") is not None and row.get("bb_lower") is not None:
        if row["close"] > row["bb_upper"]:
            bb = "above upper band"
        elif row["close"] < row["bb_lower"]:
            bb = "below lower band"
        else:
            bb = "inside bands"
    macd = row.get("macd_hist")
    return (
        f"{timeframe}: close {_f(row.get('close'))}, {_f(change)}% over last {window.height} bars, "
        f"trend {_trend(row)}, RSI {_f(row.get('rsi'), 1)}, MACD hist {'+' if macd and macd > 0 else '-' if macd else '0'}, "
        f"{bb}, ATR {_f(atr_pct)}% of price, volume {_f(row.get('volume_ratio'))}x avg, "
        f"range {_f(window['low'].min())}-{_f(window['high'].max())}"
    )


def build_brief(
    instrument_id: str,
    frames: dict[str, pl.DataFrame],
    *,
    candidate: dict[str, Any] | None = None,
    derivatives: dict[str, Any] | None = None,
    last_bar_close_ns: int | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    lines = [f"MARKET BRIEF {instrument_id} (as of {now:%Y-%m-%d %H:%M} UTC)"]
    if last_bar_close_ns:
        age_min = (now.timestamp() - last_bar_close_ns / 1e9) / 60
        lines.append(f"Data freshness: last closed bar {age_min:.0f} min ago")
    for tf, df in frames.items():
        if (line := timeframe_line(tf, df)) is not None:
            lines.append(line)
    if candidate:
        entry, stop, target = (candidate.get(k) for k in ("suggested_entry", "suggested_stop", "suggested_target"))
        rr = None
        try:  # (target - entry) / (entry - stop): same sign for longs and shorts; negative = wrong side
            risk = float(entry) - float(stop)
            rr = (float(target) - float(entry)) / risk if risk else None
        except (TypeError, ValueError):
            pass
        lines.append(
            f"Scanner candidate ({candidate.get('timeframe', '?')}): entry {_f(entry)}, stop {_f(stop)}, "
            f"target {_f(target)}, reward-to-risk {_f(rr)}; rules: {', '.join(map(str, candidate.get('matched_rules') or [])) or 'n/a'}"
        )
    if derivatives:
        rate = derivatives.get("funding_rate")
        lines.append(
            f"Perp context: mark {_f(derivatives.get('mark_price'))}, funding "
            f"{_f(float(rate) * 100, 4) if rate is not None else 'n/a'}% per interval, "
            f"open interest {_f(derivatives.get('open_interest'), 0)}"
        )
    return "\n".join(lines)
