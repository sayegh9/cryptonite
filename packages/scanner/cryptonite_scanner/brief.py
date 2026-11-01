"""Market brief (AI-first blueprint §4.3): a compact, deterministic text summary of one instrument.

Built by the scanner from the indicator frames it already computes, once per candidate bar, and
shared by every agent in the debate instead of each one pulling raw rows (fewer tokens, one stable
view of the market). Target size: well under 2k tokens.

``brief_data`` is the structured twin of ``build_brief`` (same numbers, as JSON) and ``hard_rejects``
applies the TRIAGE persona's hard rules to it in code, so the gate can reject the obvious cases without
a model call (remediation R3.1). The rules are the eval labels' rules
(``scripts/build_triage_eval_set.py``); stale data is the gate's own ``STALE_CANDIDATE`` check.
``brief_data`` also carries the setup grade's frame inputs (``grade_context``: trends, the 1h regime and
volatility), so a debate routed to a perp mandate is graded in code from the frames the scanner saw (Phase B).
"""

import json
import math
import re
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import polars as pl
from cryptonite_schemas.profile import PRESET_CATALOG

from cryptonite_scanner.setup_grade import (
    GRADE_CONTEXT_KEYS,
    GradeInputs,
    funding_rate_8h,
    grade_context,
    grade_setup,
)

LEVEL_LOOKBACK_BARS = 50
MIN_REWARD_TO_RISK = 1.5
CHOP_MAX_VOLUME_RATIO = 0.8
TIMEFRAME_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _load_replay() -> dict[tuple[str, str], dict[str, Any]]:
    try:
        doc = json.loads(resources.files("cryptonite_scanner").joinpath("data/preset_replay.json").read_text())
        return {(r["coin"], r["preset"]): r for r in doc.get("results", [])}
    except Exception:  # the replay is context only; a missing file never fails a brief
        return {}


_REPLAY = _load_replay()


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


def _reward_to_risk(entry: Any, stop: Any, target: Any) -> float | None:
    """(target - entry) / (entry - stop): same sign for longs and shorts; negative = wrong side."""
    try:
        risk = float(entry) - float(stop)
        return (float(target) - float(entry)) / risk if risk else None
    except (TypeError, ValueError):
        return None


def candidate_direction(candidate: dict[str, Any] | None) -> str | None:
    """LONG / SHORT for a scanner candidate: the preset's side when its slug is known, else the stop's side of the
    entry (stop above = SHORT, below = LONG), else None."""
    if not isinstance(candidate, dict):
        return None
    meta = PRESET_CATALOG.get(candidate.get("preset_slug") or candidate.get("spec_slug") or "")
    if meta is not None:
        return str(getattr(meta.side, "value", meta.side)).upper()
    entry, stop = _num(candidate.get("suggested_entry")), _num(candidate.get("suggested_stop"))
    if entry is None or stop is None or stop == entry:
        return None
    return "SHORT" if stop > entry else "LONG"


def _setup_grade(direction: str | None, cand: dict[str, Any] | None, ctx: dict[str, Any] | None,
                 derivatives: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Scan-time grade from the frames, the levels and the derivatives feed's funding. The book spread is not read
    here, so liquidity stays unknown and the grade is at most B (``grade_setup`` caps an unknown input)."""
    if not direction or not cand or not ctx:
        return None
    entry, stop, target = _num(cand.get("entry")), _num(cand.get("stop")), _num(cand.get("target"))
    if None in (entry, stop, target):
        return None
    try:
        kw = {k: ctx.get(k) for k in GRADE_CONTEXT_KEYS}
        funding = funding_rate_8h(_num((derivatives or {}).get("funding_rate")), None)
        g = grade_setup(GradeInputs(side=direction, entry=entry, stop=stop, target=target,  # type: ignore[arg-type]
                                    funding_rate_8h=funding, **kw))
    except Exception:
        return None
    return {"grade": g.grade, "reasons": list(g.reasons)[:5]}


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
        rr = _reward_to_risk(entry, stop, target)
        lines.append(
            f"Scanner candidate ({candidate.get('timeframe', '?')}): entry {_f(entry)}, stop {_f(stop)}, "
            f"target {_f(target)}, reward-to-risk {_f(rr)}; rules: {', '.join(map(str, candidate.get('matched_rules') or [])) or 'n/a'}"
        )
    if candidate:
        facts = brief_data(instrument_id, frames, candidate=candidate, derivatives=derivatives)
        parts = [f"Direction: {facts['direction']}"] if facts["direction"] else []
        if g := facts["setup_grade"]:
            parts.append(f"Setup grade: {g['grade']} ({'; '.join(g['reasons'])})")
        if rp := facts["replay"]:
            parts.append(
                f"Replay ({rp['preset']} on {rp['coin']}): {rp['trades']} trades, win {rp['win_rate'] * 100:.0f}%, "
                f"avg {rp['avg_r']:+.2f}R"
            )
        if parts:
            lines.append(" · ".join(parts))
    if derivatives:
        rate = derivatives.get("funding_rate")
        lines.append(
            f"Perp context: mark {_f(derivatives.get('mark_price'))}, funding "
            f"{_f(float(rate) * 100, 4) if rate is not None else 'n/a'}% per interval, "
            f"open interest {_f(derivatives.get('open_interest'), 0)}"
        )
    return "\n".join(lines)


def _num(value: Any) -> float | None:
    """A finite float, or None (JSON has no NaN)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _bands(row: dict[str, Any]) -> str | None:
    if row.get("bb_upper") is None or row.get("bb_lower") is None or row.get("close") is None:
        return None
    return "above" if row["close"] > row["bb_upper"] else "below" if row["close"] < row["bb_lower"] else "inside"


def timeframe_seconds(timeframe: str) -> int | None:
    """``"15m"`` -> 900; None for anything that is not ``<n><m|h|d|w>``."""
    m = re.fullmatch(r"(\d+)([mhdw])", str(timeframe or ""))
    return int(m.group(1)) * TIMEFRAME_UNIT_S[m.group(2)] if m else None


def timeframe_state(timeframe: str, df: pl.DataFrame) -> dict[str, Any] | None:
    """The numbers ``timeframe_line`` renders, as a dict (None for an empty frame)."""
    if df.is_empty():
        return None
    row = df.tail(1).to_dicts()[0]
    return {
        "timeframe": timeframe,
        "close": _num(row.get("close")),
        "trend": _trend(row),
        "rsi": _num(row.get("rsi")),
        "macd_hist": _num(row.get("macd_hist")),
        "bands": _bands(row),
        "atr": _num(row.get("atr")),
        "volume_ratio": _num(row.get("volume_ratio")),
    }


def brief_data(
    instrument_id: str,
    frames: dict[str, pl.DataFrame],
    *,
    candidate: dict[str, Any] | None = None,
    derivatives: dict[str, Any] | None = None,
    last_bar_close_ns: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Structured twin of ``build_brief`` (same arguments, same numbers), JSON-serialisable."""
    now = now or datetime.now(UTC)
    data: dict[str, Any] = {
        "instrument_id": instrument_id,
        "as_of": now.isoformat(),
        "last_bar_close_ns": last_bar_close_ns,
        "timeframes": [s for tf, df in frames.items() if (s := timeframe_state(tf, df)) is not None],
        "candidate": None,
        "derivatives": None,
        "grade_context": None,
        "direction": candidate_direction(candidate),
        "setup_grade": None,
        "replay": None,
    }
    if candidate:
        entry, stop, target = (candidate.get(k) for k in ("suggested_entry", "suggested_stop", "suggested_target"))
        data["candidate"] = {
            "timeframe": candidate.get("timeframe"),
            "entry": _num(entry),
            "stop": _num(stop),
            "target": _num(target),
            "reward_to_risk": _num(_reward_to_risk(entry, stop, target)),
            "matched_rules": [str(r) for r in candidate.get("matched_rules") or []],
        }
    if derivatives:
        data["derivatives"] = {k: _num(derivatives.get(k)) for k in ("mark_price", "funding_rate", "open_interest")}
    try:  # missing grade inputs only make the grade weaker; they never fail the brief
        data["grade_context"] = grade_context(frames)
    except Exception:
        data["grade_context"] = None
    data["setup_grade"] = _setup_grade(data["direction"], data["candidate"], data["grade_context"], data["derivatives"])
    slug = (candidate or {}).get("preset_slug") or (candidate or {}).get("spec_slug")
    coin = str(instrument_id).split(".")[0]
    if row := _REPLAY.get((coin, slug)):
        data["replay"] = {"coin": coin, "preset": slug, **{k: _num(row.get(k)) for k in ("trades", "win_rate", "avg_r")}}
    return data


def hard_rejects(data: dict[str, Any] | None) -> list[str]:
    """The TRIAGE hard rules that ``data`` (``brief_data``) clearly hits, in a fixed order.

    - ``REWARD_TO_RISK``: the candidate's reward-to-risk is below 1.5 (negative = stop on the wrong side);
    - ``TIMEFRAMES_AGAINST``: the two highest timeframes in the brief both trend against the candidate's direction
      (both down for a LONG or an unknown direction, both up for a SHORT);
    - ``CHOP``: on the candidate's timeframe, volume <= 0.8x average with price inside the Bollinger bands.

    A rule is applied only when every input it needs is present; missing data never rejects.
    """
    if not isinstance(data, dict):
        return []
    hits: list[str] = []
    candidate = data.get("candidate") if isinstance(data.get("candidate"), dict) else {}
    states = [s for s in data.get("timeframes") or [] if isinstance(s, dict)]

    rr = _num(candidate.get("reward_to_risk"))
    if rr is not None and rr < MIN_REWARD_TO_RISK:
        hits.append("REWARD_TO_RISK")

    ranked = [(timeframe_seconds(s.get("timeframe")), s.get("trend")) for s in states]
    if len(ranked) >= 2 and all(sec is not None for sec, _ in ranked):
        top = sorted(ranked, key=lambda x: x[0], reverse=True)[:2]
        against = "up" if data.get("direction") == "SHORT" else "down"
        if top[0][0] != top[1][0] and all(t == against for _, t in top):  # "unknown" trend = missing input
            hits.append("TIMEFRAMES_AGAINST")

    own = [s for s in states if candidate.get("timeframe") and s.get("timeframe") == candidate.get("timeframe")]
    if len(own) == 1:
        volume, bands = _num(own[0].get("volume_ratio")), own[0].get("bands")
        if volume is not None and volume <= CHOP_MAX_VOLUME_RATIO and bands == "inside":
            hits.append("CHOP")
    return hits
