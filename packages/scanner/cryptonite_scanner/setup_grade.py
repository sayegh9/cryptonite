"""Setup grade: how strong a trade setup is, A / B / C / NO_TRADE (leverage plan §2.3, Stage 3; ADR 0009).

Computed by this code, never by a model: the same inputs always give the same grade, and the inputs are kept with it
(``SetupGrade.inputs``) so a grade can be recomputed later. The grade sets a leverage ceiling (A 10x, B 5x, C 2x by
default, operator settings); a model's confidence is stored next to it for scoring but never raises leverage.

Six inputs, each read in the trade's direction:

- timeframe alignment: the 4h, 1h and 15m trends (``BULL`` / ``BEAR`` / ``FLAT``, close and EMA20 against EMA50, as
  ``scan_swing_setups`` reads them);
- reward to TP1 over the risk to the stop: >= 2.0 for A, >= 1.5 for B, >= 1.0 at least;
- the regime's trend: ADX(14) >= 25 with +DI / -DI giving the direction (the classifier of ``regime.py``, read here
  without its HIGH_VOL precedence, so volatility is judged on its own);
- volatility: ATR% above the 90th percentile of its trailing 30 days lowers the grade one step;
- crowding: funding above +0.03% per 8 h on a long (below -0.03% on a short), or open interest down 10% or more over
  24 h, lowers the grade one step;
- liquidity: a spread above 0.10% or 24 h volume below $50m caps the grade at C (either one known is enough to cap).

The grade is A when every known input is aligned, B with one weak input, C with more. A missing regime, volatility,
funding or liquidity input is neutral: it adds no weakness, but it caps the grade at B, so A still needs every input
known (H4: a scan that cannot see funding or the book was graded C for it alone). Then the volatility and crowding
steps (A -> B -> C -> NO_TRADE), the liquidity cap and the unknown-input cap. NO_TRADE (no perp card) when the geometry is wrong, the reward is below 1.0x the risk, or the
4h and the 1h trend both run against the trade.
"""

from dataclasses import asdict, dataclass
from typing import Any, Literal

GRADE_VERSION = "setup-grade-v2"  # v2: unknown inputs are neutral, capped at B
Grade = Literal["A", "B", "C", "NO_TRADE"]
GRADES: tuple[Grade, ...] = ("A", "B", "C", "NO_TRADE")

RR_A, RR_B, RR_MIN = 2.0, 1.5, 1.0
ADX_TREND_MIN = 25.0
CROWDED_FUNDING_8H = 0.0003  # 0.03% per 8 h
OI_DROP_PCT = -10.0  # open interest falling this much over 24 h
THIN_SPREAD_PCT = 0.10
THIN_VOLUME_USD = 50_000_000.0


@dataclass(frozen=True)
class GradeInputs:
    side: Literal["LONG", "SHORT"]
    entry: float
    stop: float
    target: float
    trend_4h: str | None = None  # BULL | BEAR | FLAT
    trend_1h: str | None = None
    trend_15m: str | None = None
    adx: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    atr_pct: float | None = None
    atr_pct_q90: float | None = None
    funding_rate_8h: float | None = None  # the funding rate per 8 h, as a fraction
    oi_change_24h_pct: float | None = None
    spread_pct: float | None = None
    volume_24h_usd: float | None = None


@dataclass(frozen=True)
class SetupGrade:
    grade: Grade
    reasons: tuple[str, ...]
    rr_ratio: float | None
    inputs: dict[str, Any]
    version: str = GRADE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {"grade": self.grade, "reasons": list(self.reasons), "rr_ratio": self.rr_ratio,
                "inputs": dict(self.inputs), "version": self.version}


def _step_down(grade: Grade) -> Grade:
    return GRADES[min(GRADES.index(grade) + 1, len(GRADES) - 1)]


def side_of(side: str) -> Literal["LONG", "SHORT"]:
    """BUY / LONG -> LONG; SELL / SHORT -> SHORT."""
    return "LONG" if side.upper() in ("BUY", "LONG") else "SHORT"


def grade_setup(inputs: GradeInputs) -> SetupGrade:
    long = inputs.side == "LONG"
    kept = asdict(inputs)

    def no_trade(reason: str, rr: float | None = None) -> SetupGrade:
        return SetupGrade("NO_TRADE", (reason,), rr, kept)

    risk = (inputs.entry - inputs.stop) if long else (inputs.stop - inputs.entry)
    reward = (inputs.target - inputs.entry) if long else (inputs.entry - inputs.target)
    if inputs.entry <= 0 or risk <= 0 or reward <= 0:
        return no_trade("stop and target must sit on either side of the entry")
    rr = round(reward / risk, 2)
    if rr < RR_MIN:
        return no_trade(f"reward {rr}x the risk, below {RR_MIN}x", rr)

    with_trend, against = ("BULL", "BEAR") if long else ("BEAR", "BULL")
    trends = {"4h": inputs.trend_4h, "1h": inputs.trend_1h, "15m": inputs.trend_15m}
    if trends["4h"] == against and trends["1h"] == against:
        return no_trade("the 4h and the 1h trend both run against the trade", rr)

    weak = 0
    reasons: list[str] = []
    unknown: list[str] = []  # neutral inputs: no weakness, but the grade is capped at B
    aligned = [tf for tf, t in trends.items() if t == with_trend]
    if len(aligned) == 3:
        reasons.append("4h, 1h and 15m aligned")
    elif len(aligned) == 2:
        weak += 1
        reasons.append(f"2 of 3 timeframes aligned ({', '.join(aligned)})")
    else:
        weak += 2
        reasons.append(f"{len(aligned)} of 3 timeframes aligned")

    if rr >= RR_A:
        reasons.append(f"reward {rr}x the risk")
    elif rr >= RR_B:
        weak += 1
        reasons.append(f"reward {rr}x the risk (below {RR_A}x)")
    else:
        weak += 2
        reasons.append(f"reward {rr}x the risk (below {RR_B}x)")

    if inputs.adx is None or inputs.plus_di is None or inputs.minus_di is None:
        unknown.append("regime")
    elif inputs.adx < ADX_TREND_MIN:
        weak += 1
        reasons.append(f"no trend (ADX {inputs.adx:.0f})")
    elif (inputs.plus_di >= inputs.minus_di) == long:
        reasons.append(f"trend in the trade's direction (ADX {inputs.adx:.0f})")
    else:
        weak += 2
        reasons.append(f"trend against the trade (ADX {inputs.adx:.0f})")

    high_vol = None
    if inputs.atr_pct is None or inputs.atr_pct_q90 is None:
        unknown.append("volatility")
    else:
        high_vol = inputs.atr_pct > inputs.atr_pct_q90

    crowded_funding = None
    if inputs.funding_rate_8h is None:
        unknown.append("funding")
    else:
        crowded_funding = (inputs.funding_rate_8h > CROWDED_FUNDING_8H) if long else (inputs.funding_rate_8h < -CROWDED_FUNDING_8H)
    oi_falling = inputs.oi_change_24h_pct is not None and inputs.oi_change_24h_pct <= OI_DROP_PCT

    thin = ((inputs.spread_pct is not None and inputs.spread_pct > THIN_SPREAD_PCT)
            or (inputs.volume_24h_usd is not None and inputs.volume_24h_usd < THIN_VOLUME_USD))
    if not thin and (inputs.spread_pct is None or inputs.volume_24h_usd is None):
        unknown.append("liquidity")

    grade: Grade = "A" if weak == 0 else "B" if weak == 1 else "C"
    if high_vol:
        grade = _step_down(grade)
        reasons.append("volatility above its 90th percentile: one step down")
    if crowded_funding or oi_falling:
        grade = _step_down(grade)
        reasons.append("crowded (funding against the trade or open interest falling): one step down")
    if thin and grade in ("A", "B"):
        grade = "C"
        reasons.append("thin book: capped at C")
    elif thin:
        reasons.append("thin book")
    if unknown:
        if grade == "A":
            grade = "B"
        reasons.append(f"{', '.join(unknown)} unknown: neutral, capped at B")
    return SetupGrade(grade, tuple(reasons), rr, kept)


def trend_of(df) -> str:
    """A timeframe's trend at its last bar: BULL when the close and EMA20 are above EMA50, BEAR when both are below,
    else FLAT (also for an empty or unreadable frame). ``scan_swing_setups`` and the grade read trends this way."""
    if df is None:
        return "FLAT"
    try:
        if (hasattr(df, "is_empty") and df.is_empty()) or len(df) == 0:
            return "FLAT"
        row = df.slice(-1, 1).to_dicts()[0]
        close, ema_20, ema_50 = row.get("close"), row.get("ema_20"), row.get("ema_50")
        if close is None or ema_20 is None or ema_50 is None:
            return "FLAT"
        close_f, ema_20_f, ema_50_f = float(close), float(ema_20), float(ema_50)
        if close_f > ema_50_f and ema_20_f > ema_50_f:
            return "BULL"
        if close_f < ema_50_f and ema_20_f < ema_50_f:
            return "BEAR"
        return "FLAT"
    except Exception:
        return "FLAT"


def frame_trend(df) -> str | None:
    """``trend_of`` for the grade: None (unknown, a weak input) when there is no frame, not FLAT."""
    return None if df is None or (hasattr(df, "is_empty") and df.is_empty()) else trend_of(df)


GRADE_CONTEXT_KEYS = ("trend_4h", "trend_1h", "trend_15m", "atr_pct", "atr_pct_q90", "adx", "plus_di", "minus_di",
                      "volume_24h_usd")


def volume_24h_usd(bars_1h) -> float | None:
    """The 24 h quote volume from stored 1h bars: the sum of close x volume over the last 24 bars. None with fewer
    bars or an unreadable frame (unknown, never zero: a zero would read as a thin book)."""
    try:
        if bars_1h is None or getattr(bars_1h, "height", 0) < 24:
            return None
        last = bars_1h.tail(24)
        total = float((last["close"] * last["volume"]).sum())
    except Exception:
        return None
    return total if total == total and total > 0 else None


def grade_context(frames: dict) -> dict[str, Any]:
    """The grade inputs a scan's own frames give (4h/1h/15m trends, the 1h regime and volatility, the 24 h volume of
    the 1h bars), JSON-serialisable. Funding, open interest and the spread are not in the frames: the caller adds what
    it can read, the rest stays unknown (neutral, capped at B). A frame that cannot be read gives None for its inputs."""
    try:
        reg = regime_trend_inputs(frames.get("1h"))
    except Exception:  # too few or unreadable bars: the regime and volatility are unknown
        reg = dict.fromkeys(("atr_pct", "atr_pct_q90", "adx", "plus_di", "minus_di"))
    return {"trend_4h": frame_trend(frames.get("4h")), "trend_1h": frame_trend(frames.get("1h")),
            "trend_15m": frame_trend(frames.get("15m")), **reg, "volume_24h_usd": volume_24h_usd(frames.get("1h"))}


def funding_rate_8h(rate: float | None, interval_hours: float | None) -> float | None:
    """A funding rate per settlement interval, scaled to 8 h (the grade's crowding threshold is per 8 h). An interval
    that is not 1, 2, 4 or 8 h is read as 8 h, as ``grade_setup``'s card inputs do."""
    if rate is None:
        return None
    hours = interval_hours if interval_hours in (1.0, 2.0, 4.0, 8.0) else 8.0
    return float(rate) * 8.0 / hours


def regime_trend_inputs(bars_1h) -> dict[str, float | None]:
    """ADX, +DI, -DI, ATR% and its trailing 90th percentile at the last 1h bar (``regime.regime_inputs``), the grade's
    regime and volatility inputs; all None with too few bars."""
    from cryptonite_scanner.regime import MIN_BARS, regime_inputs

    keys = ("atr_pct", "atr_pct_q90", "adx", "plus_di", "minus_di")
    if bars_1h is None or getattr(bars_1h, "height", 0) < MIN_BARS:
        return dict.fromkeys(keys)
    row = regime_inputs(bars_1h).row(-1)
    return {k: (float(v) if v is not None else None) for k, v in zip(keys, row, strict=True)}
