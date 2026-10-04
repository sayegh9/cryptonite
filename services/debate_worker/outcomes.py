"""Outcome labeller (AI-first blueprint A7.1): what the market did after each portfolio decision.

For every decision with a ``decision_price`` and each horizon that has fully elapsed, the asset's
return from the decision price to the horizon close plus the best (MFE) and worst (MAE) excursion in
between, from the 1m analytics lake. Values are unsigned fractions of the decision price; the
decision's direction is applied where they are used (``signed_outcome``).
"""

import datetime
import logging
from decimal import Decimal
from typing import Any

import polars as pl

logger = logging.getLogger("cryptonite.debate_worker.outcomes")

HORIZONS_S = {"1h": 3600, "4h": 14_400, "24h": 86_400, "72h": 259_200}
MIN_COVERAGE = 0.98  # a horizon with more than 2% of its 1m bars missing is left for the backfill
BATCH = 50


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
    """BUY profits from a rise. SELL / HOLD (spot: exit or stay out) are right when the asset falls."""
    return return_pct if recommendation.upper() == "BUY" else -return_pct


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
        async with pool.acquire() as conn:
            for h in due:
                out = label_window(price, decision_ns, HORIZONS_S[h], bars)
                if out is None:
                    continue
                await conn.execute(
                    """INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars)
                       VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (decision_id, horizon) DO NOTHING""",
                    row["id"], h, Decimal(str(out["return_pct"])), Decimal(str(out["mfe"])),
                    Decimal(str(out["mae"])), out["bars"])
                written += 1
    if written:
        logger.info("Labelled %d decision outcomes", written)
    return written
