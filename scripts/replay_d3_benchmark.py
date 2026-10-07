"""Replay assessment for D3: Structure-Based Stop/Target and Trigger Timeframes.

Compares:
- D1 Baseline: 114 (BTC), 115 (ETH)
- D2 Event Crossover: 12 (BTC), 17 (ETH)
- D3 Structure Stop/Target + HTF Target + R:R >= 1.5 filter
Both on Oct 2–4 baseline window and Oct 1–7 extended holdout window.
"""

from uuid import uuid4

from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import MIN_RISK_REWARD_RATIO, deduplicate_candidates
from cryptonite_scanner.presets import (
    PRESET_BOLLINGER_BREAKOUT,
    PRESET_MACD_MOMENTUM,
    PRESET_RSI_OVERSOLD,
    PRESET_TREND_PULLBACK,
)
from cryptonite_scanner.resampler import resample_1m_bars
from cryptonite_storage.queries import AnalyticsQueryEngine


def run_benchmark(engine, start_ns: int, end_ns: int, window_name: str):
    presets = [
        PRESET_MACD_MOMENTUM,
        PRESET_BOLLINGER_BREAKOUT,
        PRESET_RSI_OVERSOLD,
        PRESET_TREND_PULLBACK,
    ]

    print("\n=======================================================")
    print(f"  BENCHMARK WINDOW: {window_name}")
    print("=======================================================")

    results = {}

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        df_1m = engine.query_bars_df(symbol, "1m", start_ns=start_ns, end_ns=end_ns)
        df_15m = resample_1m_bars(df_1m, "15m")
        df_1h = resample_1m_bars(df_1m, "1h")
        enriched_1h = ScannerEngine.enrich_indicators(df_1h)

        print(f"\nSymbol: {symbol}")
        print(f"  1m bars: {len(df_1m)}, 15m bars: {len(df_15m)}, 1h bars: {len(df_1h)}")

        all_raw_cands = []
        all_d3_cands = []
        spec_stats = {}

        for p in presets:
            cands = ScannerEngine.evaluate(
                df=df_15m,
                rule_groups=p.rules,
                risk_config=p.risk,
                instrument_id=f"{symbol}.BINANCE",
                timeframe="15m",
                spec_version_id=uuid4(),
                scan_run_id=uuid4(),
                htf_df=enriched_1h,
            )
            all_raw_cands.extend(cands)
            passed = [
                c for c in cands
                if (float(c.risk_reward_ratio) if c.risk_reward_ratio else 0.0) >= MIN_RISK_REWARD_RATIO
            ]
            rejected = [
                c for c in cands
                if (float(c.risk_reward_ratio) if c.risk_reward_ratio else 0.0) < MIN_RISK_REWARD_RATIO
            ]
            all_d3_cands.extend(passed)
            spec_stats[p.slug] = {
                "raw": len(cands),
                "passed": len(passed),
                "rejected": len(rejected),
                "candidates": cands,
            }

            print(f"  Preset: {p.slug:<25} | Raw: {len(cands):2d} | Passed (R:R>=1.5): {len(passed):2d} | Rejected (R:R<1.5): {len(rejected):2d}")
            for c in cands:
                rr = float(c.risk_reward_ratio) if c.risk_reward_ratio else 0.0
                st = c.metrics.get("stop_type", "unknown")
                tt = c.metrics.get("target_type", "unknown")
                status = "PASS" if rr >= MIN_RISK_REWARD_RATIO else "REJECT"
                print(f"    [{status}] Close: {c.suggested_entry:10.2f} | Stop: {c.suggested_stop:10.2f} ({st:<14}) | Target: {c.suggested_target:10.2f} ({tt:<16}) | R:R: {rr:.2f}")

        deduped_raw = deduplicate_candidates(all_raw_cands)
        deduped_d3 = deduplicate_candidates(all_d3_cands)

        # Stop & Target type distribution among accepted candidates
        stop_dist = {}
        target_dist = {}
        rr_vals = []
        for c in deduped_d3:
            st = c.metrics.get("stop_type", "unknown")
            tt = c.metrics.get("target_type", "unknown")
            stop_dist[st] = stop_dist.get(st, 0) + 1
            target_dist[tt] = target_dist.get(tt, 0) + 1
            rr_vals.append(float(c.risk_reward_ratio) if c.risk_reward_ratio else 0.0)

        results[symbol] = {
            "deduped_raw": len(deduped_raw),
            "deduped_d3": len(deduped_d3),
            "stop_dist": stop_dist,
            "target_dist": target_dist,
            "rr_vals": rr_vals,
            "spec_stats": spec_stats,
        }

        print(f"\n  -- {symbol} Aggregate --")
        print(f"  Raw D2 Candidates (deduped): {len(deduped_raw)}")
        print(f"  D3 Accepted (R:R>=1.5, deduped): {len(deduped_d3)}")
        print(f"  Stop Type Distribution: {stop_dist}")
        print(f"  Target Type Distribution: {target_dist}")
        avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0.0
        print(f"  Accepted R:R Mean: {avg_rr:.2f}, Min: {min(rr_vals) if rr_vals else 0.0:.2f}, Max: {max(rr_vals) if rr_vals else 0.0:.2f}")

    return results


def main():
    engine = AnalyticsQueryEngine("data/analytics_lake")

    # Baseline Oct 2 00:00 UTC to Oct 4 23:59:59 UTC
    start_oct2_ns = 1790908800000000000
    end_oct4_ns = 1791168000000000000
    run_benchmark(engine, start_oct2_ns, end_oct4_ns, "Oct 2-4 Baseline Window (3 days)")

    # Extended Holdout Oct 1 00:00 UTC to Oct 7 23:59:59 UTC (7 days)
    start_oct1_ns = 1790822400000000000
    end_oct7_ns = 1791427200000000000
    run_benchmark(engine, start_oct1_ns, end_oct7_ns, "Oct 1-7 Extended Holdout (7 days)")


if __name__ == "__main__":
    main()
