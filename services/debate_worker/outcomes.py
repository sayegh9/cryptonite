"""Outcome labeller (AI-first blueprint A7.1): what the market did after each portfolio decision.

For every decision with a ``decision_price`` and each horizon that has fully elapsed, the asset's
return from the decision price to the horizon close plus the best (MFE) and worst (MAE) excursion in
between, from the 1m analytics lake. Values are unsigned fractions of the decision price; the
decision's direction is applied where they are used (``signed_outcome``).

Each labelled row also stores the market regime at decision time (F1, reporting only), classified
from the 1h lake bars up to the decision; ``None`` when the bars are missing or too few.
"""

import datetime
import logging
from decimal import Decimal
from typing import Any

import asyncpg
import polars as pl
from cryptonite_scanner.regime import HIGH_VOL_WINDOW_BARS, MIN_BARS, classify_regime
from cryptonite_schemas.debate import normalize_verdict

logger = logging.getLogger("cryptonite.debate_worker.outcomes")

HORIZONS_S = {"1h": 3600, "4h": 14_400, "24h": 86_400, "72h": 259_200}
MIN_COVERAGE = 0.98  # a horizon with more than 2% of its 1m bars missing is left for the backfill
BATCH = 50
REGIME_LOOKBACK_S = (HIGH_VOL_WINDOW_BARS + MIN_BARS) * 3600  # trailing 1h bars the regime needs


def regime_at(query_engine, symbol: str, decision_ns: int) -> str | None:
    """The regime from the 1h bars up to the decision; ``None`` on missing data or any error, so a
    regime problem never blocks the outcome label itself."""
    try:
        bars = query_engine.query_bars_df(symbol=symbol, timeframe="1h",
                                          start_ns=decision_ns - REGIME_LOOKBACK_S * 1_000_000_000, end_ns=decision_ns)
        if bars is None or bars.is_empty() or "ts_event_ns" not in bars.columns:
            return None
        return classify_regime(bars.filter(pl.col("ts_event_ns") <= decision_ns))
    except Exception as e:  # reporting only
        logger.warning("Regime unavailable for %s at %d: %s", symbol, decision_ns, e)
        return None


def label_window(price: float, decision_ns: int, horizon_s: int, bars: pl.DataFrame) -> dict[str, Any] | None:
    """Return / MFE / MAE over (decision, decision + horizon], or None when the window is too gappy."""
    end_ns = decision_ns + horizon_s * 1_000_000_000
    w = bars.filter((pl.col("ts_event_ns") > decision_ns) & (pl.col("ts_event_ns") <= end_ns)).sort("ts_event_ns")
    if price <= 0 or w.height < (horizon_s // 60) * MIN_COVERAGE:
        return None
    close = float(w["close"][-1])
    return {"return_pct": close / price - 1, "mfe": float(w["high"].max()) / price - 1,
            "mae": float(w["low"].min()) / price - 1, "bars": w.height}


def signed_outcome(recommendation: str, return_pct: float) -> float:
    """BUY profits from a rise. SELL / HOLD (spot: exit or stay out) are right when the asset falls.
    The verdict is read by its direction (``normalize_verdict``: Overweight is a BUY); an unknown
    verdict raises ``ValueError`` rather than being scored as a short."""
    return return_pct if normalize_verdict(recommendation) == "BUY" else -return_pct


async def label_pending(pool, query_engine, now: datetime.datetime | None = None) -> int:
    """Label every elapsed, not yet labelled (decision, horizon). Returns the number of rows written."""
    now = now or datetime.datetime.now(datetime.UTC)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT pd.id, pd.instrument_id, pd.decision_price, pd.created_at,
                      COALESCE(array_agg(o.horizon) FILTER (WHERE o.horizon IS NOT NULL), '{}') AS done
               FROM portfolio_decisions pd
               LEFT JOIN ai_decision_outcomes o ON o.decision_id = pd.id
               WHERE pd.decision_price IS NOT NULL AND pd.decision_price > 0
                 AND pd.created_at < $1::timestamptz - interval '1 hour'
                 AND pd.created_at > $1::timestamptz - interval '30 days'  -- a permanently gappy window cannot starve newer ones
               GROUP BY pd.id
               HAVING count(o.horizon) < $2
               ORDER BY pd.created_at
               LIMIT $3""", now, len(HORIZONS_S), BATCH)
    written = 0
    for row in rows:
        decision_ns = int(row["created_at"].timestamp() * 1_000_000_000)
        due = [h for h, s in HORIZONS_S.items()
               if h not in row["done"] and row["created_at"] + datetime.timedelta(seconds=s) <= now]
        if not due:
            continue
        symbol = row["instrument_id"].split(".")[0]
        longest = max(HORIZONS_S[h] for h in due)
        bars = query_engine.query_bars_df(symbol=symbol, timeframe="1m", start_ns=decision_ns,
                                          end_ns=decision_ns + longest * 1_000_000_000)
        if bars.is_empty():
            continue
        price = float(row["decision_price"])
        labels = {h: out for h in due if (out := label_window(price, decision_ns, HORIZONS_S[h], bars)) is not None}
        if not labels:
            continue
        regime = regime_at(query_engine, symbol, decision_ns)
        async with pool.acquire() as conn:
            for h, out in labels.items():
                values = (row["id"], h, Decimal(str(out["return_pct"])), Decimal(str(out["mfe"])),
                          Decimal(str(out["mae"])), out["bars"])
                try:
                    await conn.execute(
                        """INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars, regime)
                           VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (decision_id, horizon) DO NOTHING""",
                        *values, regime)
                except asyncpg.UndefinedColumnError:  # learner deployed before migration 034: label without it
                    await conn.execute(
                        """INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars)
                           VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (decision_id, horizon) DO NOTHING""",
                        *values)
                written += 1
    if written:
        logger.info("Labelled %d decision outcomes", written)
    return written


async def backfill_regimes(pool, query_engine, dry_run: bool = False) -> dict[str, int]:
    """Fill ``regime`` on outcome rows labelled before migration 034 (one regime per decision, written
    to all its horizons). Rows whose regime cannot be computed stay NULL. Returns counts."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT pd.id, pd.instrument_id, pd.created_at
               FROM ai_decision_outcomes o JOIN portfolio_decisions pd ON pd.id = o.decision_id
               WHERE o.regime IS NULL ORDER BY pd.created_at""")
    tagged = untagged = 0
    for row in rows:
        decision_ns = int(row["created_at"].timestamp() * 1_000_000_000)
        regime = regime_at(query_engine, row["instrument_id"].split(".")[0], decision_ns)
        if regime is None:
            untagged += 1
            continue
        tagged += 1
        if not dry_run:
            async with pool.acquire() as conn:
                await conn.execute("UPDATE ai_decision_outcomes SET regime = $2 WHERE decision_id = $1 AND regime IS NULL",
                                   row["id"], regime)
    return {"decisions": len(rows), "tagged": tagged, "untagged": untagged}
