"""Write the chart/scanner indicator parity fixture (remediation R7.2).

Deterministic bars plus the scanner's own SMA, EMA, RSI, ATR and population standard deviation, so
`apps/web/e2e/indicator_parity.spec.ts` can check the chart's `technicalindicators` output against
the scanner on the same bars. Re-run after changing either side:

    uv run python scripts/build_indicator_parity_fixture.py
"""

import json
import math
import random
from pathlib import Path

import polars as pl
from cryptonite_scanner.indicators import calc_atr, calc_ema, calc_rsi, calc_sma

OUT = Path(__file__).resolve().parents[1] / "apps/web/e2e/fixtures/indicator_parity.json"
N, PERIOD = 300, 14


def bars() -> list[dict]:
    rng, price, out = random.Random(7), 60_000.0, []
    for i in range(N):
        o = price
        c = o * math.exp(rng.gauss(0, 0.004))
        h, low = max(o, c) * (1 + abs(rng.gauss(0, 0.002))), min(o, c) * (1 - abs(rng.gauss(0, 0.002)))
        out.append({"bar_open_ns": (1_700_000_000 + 60 * i) * 1_000_000_000, "open": round(o, 2), "high": round(h, 2),
                    "low": round(low, 2), "close": round(c, 2), "volume": round(rng.uniform(5, 50), 3)})
        price = c
    return out


def main() -> None:
    b = bars()
    df = pl.DataFrame(b).with_columns(
        calc_sma("close", PERIOD).alias("SMA"), calc_ema("close", PERIOD).alias("EMA"),
        calc_rsi("close", PERIOD).alias("RSI"), calc_atr("high", "low", "close", PERIOD).alias("ATR"),
        pl.col("close").rolling_std(window_size=PERIOD, ddof=0).alias("SD"))
    expected = {k: [None if v is None else float(v) for v in df[k].to_list()] for k in ("SMA", "EMA", "RSI", "ATR", "SD")}
    OUT.write_text(json.dumps({"period": PERIOD, "bars": b, "scanner": expected}, indent=None) + "\n")
    print(f"wrote {OUT} ({N} bars)")


if __name__ == "__main__":
    main()
