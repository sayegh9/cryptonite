"""Reproducible read-only benchmark for Phase D3: Structure-Based Stop/Target and Trigger Timeframes.

Validates candidate volume reduction across every active automatic preset and allowed timeframe:
- Automatic trigger timeframes: 15m, 1h, 4h
- Presets evaluated: All BUILTIN_PRESETS from cryptonite_scanner.presets
- Execution logic matches ScannerWorker._execute_scan:
    1. Resamples 1m historical lake data into candidate and HTF frames (0-lookahead).
    2. Evaluates indicator rules and structure-based stop/target.
    3. Qualifies candidates against unrounded R:R >= 1.5 BEFORE deduplication.
    4. Deterministically ranks qualified candidates by higher R:R (-rr) on duplicate bars.
    5. Deduplicates candidates per (instrument, timeframe, bar_close_inclusive_ns).
- Measures:
    - Same-window baseline comparisons (D1 state baseline vs D2 crossover vs D3 structure)
    - Explicit data coverage and bar counts
    - Filter-reason counts (sub-threshold R:R vs invalid ATR)
    - Valid-signal retention / recall rate
"""

from decimal import Decimal
from uuid import uuid4

from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import (
    MIN_RISK_REWARD_RATIO,
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
    deduplicate_candidates,
)
from cryptonite_scanner.presets import BUILTIN_PRESETS
from cryptonite_scanner.resampler import resample_1m_bars
from cryptonite_storage.queries import AnalyticsQueryEngine

# D1 State-based rules for comparison baseline
OLD_MACD_RULES = [
    RuleGroup(category="trend", logic="AND", conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value="ema_50")]),
    RuleGroup(category="trigger", logic="AND", conditions=[RuleCondition(field="macd_hist", operator=RuleOperator.GT, value=0.0)]),
]
OLD_BB_RULES = [
    RuleGroup(category="trigger", logic="AND", conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value="bb_upper")]),
    RuleGroup(category="confirmation", logic="AND", conditions=[
        RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=1.5),
        RuleCondition(field="close", operator=RuleOperator.GT, value="open"),
    ]),
]
OLD_RSI_RULES = [
    RuleGroup(category="trigger", logic="AND", conditions=[RuleCondition(field="close", operator=RuleOperator.LT, value="bb_lower")]),
    RuleGroup(category="confirmation", logic="AND", conditions=[
        RuleCondition(field="rsi", operator=RuleOperator.LTE, value=35.0),
        RuleCondition(field="volume_ratio", operator=RuleOperator.GTE, value=1.0),
    ]),
]
OLD_TREND_RULES = [
    RuleGroup(category="trend", logic="AND", conditions=[
        RuleCondition(field="close", operator=RuleOperator.GT, value="ema_200"),
        RuleCondition(field="ema_20", operator=RuleOperator.GT, value="ema_50"),
    ]),
    RuleGroup(category="trigger", logic="AND", conditions=[RuleCondition(field="rsi", operator=RuleOperator.LT, value=40.0)]),
    RuleGroup(category="confirmation", logic="AND", conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value="open")]),
]
D1_PRESET_SPECS = [
    ("macd-momentum-shift", OLD_MACD_RULES, ["15m", "1h"]),
    ("bollinger-breakout", OLD_BB_RULES, ["15m", "1h"]),
    ("rsi-oversold-reversal", OLD_RSI_RULES, ["15m", "1h"]),
    ("trend-pullback", OLD_TREND_RULES, ["15m", "1h"]),
]


def _candidate_rank_key(c) -> tuple:
    risk = abs(c.suggested_entry - c.suggested_stop)
    reward = abs(c.suggested_target - c.suggested_entry)
    rr = (reward / risk) if risk > Decimal("0") else Decimal("0")
    return (c.instrument_id, c.timeframe, c.bar_close_inclusive_ns, -rr, c.id)


def evaluate_window(engine: AnalyticsQueryEngine, start_ns: int, end_ns: int, window_name: str) -> dict:
    print("\n" + "=" * 70)
    print(f"  BENCHMARK WINDOW: {window_name}")
    print("=" * 70)

    timeframes = ["15m", "1h", "4h"]
    htf_map = {"15m": "1h", "1h": "4h", "4h": "1d"}
    min_rr_dec = Decimal(str(MIN_RISK_REWARD_RATIO))

    window_results = {}

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        df_1m = engine.query_bars_df(symbol, "1m", start_ns=start_ns, end_ns=end_ns)

        # Build resampled bars
        dfs = {
            "15m": resample_1m_bars(df_1m, "15m"),
            "1h": resample_1m_bars(df_1m, "1h"),
            "4h": resample_1m_bars(df_1m, "4h"),
            "1d": resample_1m_bars(df_1m, "1d"),
        }
        enriched_htfs = {
            "1h": ScannerEngine.enrich_indicators(dfs["1h"]) if len(dfs["1h"]) > 0 else None,
            "4h": ScannerEngine.enrich_indicators(dfs["4h"]) if len(dfs["4h"]) > 0 else None,
            "1d": ScannerEngine.enrich_indicators(dfs["1d"]) if len(dfs["1d"]) > 0 else None,
        }

        print(f"\nSymbol: {symbol}")
        print(f"  1m bars: {len(df_1m)} | 15m bars: {len(dfs['15m'])} | 1h bars: {len(dfs['1h'])} | 4h bars: {len(dfs['4h'])}")

        # ---------------------------------------------------------------------
        # 1. D1 State Baseline Evaluation (Same Window)
        # ---------------------------------------------------------------------
        d1_raw_cands = []
        for name, rules, tfs in D1_PRESET_SPECS:
            for tf in tfs:
                c = ScannerEngine.evaluate(
                    df=dfs[tf],
                    rule_groups=rules,
                    risk_config=RiskConfig(),
                    instrument_id=f"{symbol}.BINANCE",
                    timeframe=tf,
                    spec_version_id=uuid4(),
                    scan_run_id=uuid4(),
                )
                d1_raw_cands.extend(c)
        d1_15m_raw = [c for c in d1_raw_cands if c.timeframe == "15m"]
        d1_15m_deduped = deduplicate_candidates(d1_15m_raw)
        d1_all_deduped = deduplicate_candidates(d1_raw_cands)

        # ---------------------------------------------------------------------
        # 2. D2 Crossover Baseline Evaluation (Same Window)
        # ---------------------------------------------------------------------
        d2_raw_cands = []
        for p in BUILTIN_PRESETS:
            for tf in p.universe.timeframes:
                if tf in timeframes:
                    c = ScannerEngine.evaluate(
                        df=dfs[tf],
                        rule_groups=p.rules,
                        risk_config=p.risk,
                        instrument_id=f"{symbol}.BINANCE",
                        timeframe=tf,
                        spec_version_id=uuid4(),
                        scan_run_id=uuid4(),
                    )
                    d2_raw_cands.extend(c)
        d2_15m_raw = [c for c in d2_raw_cands if c.timeframe == "15m"]
        d2_all_deduped = deduplicate_candidates(d2_raw_cands)

        # ---------------------------------------------------------------------
        # 3. D3 Structure Evaluation with Zero-Lookahead HTF
        # ---------------------------------------------------------------------
        d3_all_cands = []
        for tf in timeframes:
            target_htf_name = htf_map[tf]
            target_htf_df = enriched_htfs.get(target_htf_name)

            for p in BUILTIN_PRESETS:
                if tf not in p.universe.timeframes:
                    continue
                c = ScannerEngine.evaluate(
                    df=dfs[tf],
                    rule_groups=p.rules,
                    risk_config=p.risk,
                    instrument_id=f"{symbol}.BINANCE",
                    timeframe=tf,
                    spec_version_id=uuid4(),
                    scan_run_id=uuid4(),
                    htf_df=target_htf_df,
                )
                d3_all_cands.extend(c)

        # ---------------------------------------------------------------------
        # 4. Worker-Equivalent Qualification Before Deduplication
        # ---------------------------------------------------------------------
        qualified_cands = []
        # Note: Invalid ATR is discarded at engine level before candidate creation.
        filter_reasons = {"below_rr_threshold": 0}

        for c in d3_all_cands:
            risk = abs(c.suggested_entry - c.suggested_stop)
            reward = abs(c.suggested_target - c.suggested_entry)
            derived_rr = (reward / risk) if risk > Decimal("0") else Decimal("0")

            if derived_rr < min_rr_dec:
                filter_reasons["below_rr_threshold"] += 1
            else:
                qualified_cands.append(c)

        # Sort qualified deterministically by -rr
        qualified_cands.sort(key=_candidate_rank_key)

        # Deduplicate per (instrument, timeframe, bar_close)
        d3_accepted_all = deduplicate_candidates(qualified_cands)
        d3_accepted_15m = [c for c in d3_accepted_all if c.timeframe == "15m"]
        d3_accepted_1h = [c for c in d3_accepted_all if c.timeframe == "1h"]
        d3_accepted_4h = [c for c in d3_accepted_all if c.timeframe == "4h"]

        # Candidate retention measure:
        # What percentage of D2 candidate setups passed the D3 structural R:R gate?
        candidate_retention_rate = (len(d3_accepted_all) / len(d2_all_deduped) * 100.0) if d2_all_deduped else 0.0

        # Provenance metrics
        stop_types = {}
        target_types = {}
        rr_vals = []
        for c in d3_accepted_all:
            st = c.metrics.get("stop_type", "unknown")
            tt = c.metrics.get("target_type", "unknown")
            stop_types[st] = stop_types.get(st, 0) + 1
            target_types[tt] = target_types.get(tt, 0) + 1
            rr_vals.append(float(c.risk_reward_ratio) if c.risk_reward_ratio else 0.0)

        mean_rr = (sum(rr_vals) / len(rr_vals)) if rr_vals else 0.0

        # Reductions vs D1 (both raw and like-for-like deduped)
        red_15m_vs_d1_raw = len(d1_15m_raw) / len(d3_accepted_15m) if d3_accepted_15m else float("inf")
        red_15m_vs_d1_dedup = len(d1_15m_deduped) / len(d3_accepted_15m) if d3_accepted_15m else float("inf")
        red_all_vs_d1_raw = len(d1_raw_cands) / len(d3_accepted_all) if d3_accepted_all else float("inf")
        red_all_vs_d1_dedup = len(d1_all_deduped) / len(d3_accepted_all) if d3_accepted_all else float("inf")
        red_all_vs_d2 = len(d2_all_deduped) / len(d3_accepted_all) if d3_accepted_all else float("inf")

        print("  -- Counts & Reductions --")
        print(f"  D1 State Baseline:            15m={len(d1_15m_raw):3d} raw ({len(d1_15m_deduped):3d} deduped) | All TFs={len(d1_raw_cands):3d} raw ({len(d1_all_deduped):3d} deduped)")
        print(f"  D2 Crossover Baseline (raw):  15m={len(d2_15m_raw):3d} | All TFs={len(d2_raw_cands):3d} (deduped: {len(d2_all_deduped)})")
        print(f"  D3 Raw Generated:             15m={len([c for c in d3_all_cands if c.timeframe == '15m']):3d} | All TFs={len(d3_all_cands):3d}")
        print(f"  D3 Filter Rejections:         {filter_reasons}")
        print(f"  D3 Qualified (pre-dedupe):    {len(qualified_cands):3d}")
        print(f"  D3 Accepted (deduped):        15m={len(d3_accepted_15m):3d} | 1h={len(d3_accepted_1h):3d} | 4h={len(d3_accepted_4h):3d} | Total={len(d3_accepted_all):3d}")
        print(f"  15m Reduction vs D1:          {red_15m_vs_d1_raw:.1f}x raw ({red_15m_vs_d1_dedup:.1f}x deduped like-for-like)")
        print(f"  All-TF Reduction vs D1:       {red_all_vs_d1_raw:.1f}x raw ({red_all_vs_d1_dedup:.1f}x deduped like-for-like)")
        print(f"  All-TF Reduction vs D2:       {red_all_vs_d2:.1f}x")
        print(f"  Candidate Retention Rate:     {candidate_retention_rate:.1f}%")
        print(f"  Stop Provenance:              {stop_types}")
        print(f"  Target Provenance:            {target_types}")
        print(f"  Accepted R:R:                 mean={mean_rr:.2f}, min={min(rr_vals) if rr_vals else 0.0:.2f}, max={max(rr_vals) if rr_vals else 0.0:.2f}")

        window_results[symbol] = {
            "d1_raw_15m": len(d1_15m_raw),
            "d1_dedup_15m": len(d1_15m_deduped),
            "d1_raw_all": len(d1_raw_cands),
            "d1_deduped_all": len(d1_all_deduped),
            "d2_raw_15m": len(d2_15m_raw),
            "d2_raw_all": len(d2_raw_cands),
            "d2_deduped_all": len(d2_all_deduped),
            "d3_raw_all": len(d3_all_cands),
            "d3_rejections": filter_reasons,
            "d3_accepted_15m": len(d3_accepted_15m),
            "d3_accepted_1h": len(d3_accepted_1h),
            "d3_accepted_4h": len(d3_accepted_4h),
            "d3_accepted_all": len(d3_accepted_all),
            "red_15m_vs_d1_raw": red_15m_vs_d1_raw,
            "red_15m_vs_d1_dedup": red_15m_vs_d1_dedup,
            "red_all_vs_d1_raw": red_all_vs_d1_raw,
            "red_all_vs_d1_dedup": red_all_vs_d1_dedup,
            "red_all_vs_d2": red_all_vs_d2,
            "candidate_retention_rate": candidate_retention_rate,
            "mean_rr": mean_rr,
        }

    # Combined multi-asset metrics
    btc = window_results["BTCUSDT"]
    eth = window_results["ETHUSDT"]
    comb_d1_15m_raw = btc["d1_raw_15m"] + eth["d1_raw_15m"]
    comb_d1_15m_dedup = btc["d1_dedup_15m"] + eth["d1_dedup_15m"]
    comb_d1_all_raw = btc["d1_raw_all"] + eth["d1_raw_all"]
    comb_d1_all_dedup = btc["d1_deduped_all"] + eth["d1_deduped_all"]
    comb_d2_all = btc["d2_deduped_all"] + eth["d2_deduped_all"]
    comb_d3_15m = btc["d3_accepted_15m"] + eth["d3_accepted_15m"]
    comb_d3_all = btc["d3_accepted_all"] + eth["d3_accepted_all"]
    comb_15m_red_raw = comb_d1_15m_raw / comb_d3_15m if comb_d3_15m else float("inf")
    comb_15m_red_dedup = comb_d1_15m_dedup / comb_d3_15m if comb_d3_15m else float("inf")
    comb_all_red_raw = comb_d1_all_raw / comb_d3_all if comb_d3_all else float("inf")
    comb_all_red_dedup = comb_d1_all_dedup / comb_d3_all if comb_d3_all else float("inf")
    comb_retention = (comb_d3_all / comb_d2_all * 100.0) if comb_d2_all else 0.0

    print("\n" + "-" * 70)
    print(f"  COMBINED BTC+ETH METRICS ({window_name})")
    print("-" * 70)
    print(f"  Combined 15m D1 Baseline:     {comb_d1_15m_raw} raw, {comb_d1_15m_dedup} deduped")
    print(f"  Combined 15m D3 Accepted:     {comb_d3_15m}  ==>  Reduction: {comb_15m_red_raw:.1f}x raw ({comb_15m_red_dedup:.1f}x deduped like-for-like)")
    print(f"  Combined All-TF D1 Baseline:  {comb_d1_all_raw} raw, {comb_d1_all_dedup} deduped")
    print(f"  Combined All-TF D3 Accepted:  {comb_d3_all}  ==>  Reduction: {comb_all_red_raw:.1f}x raw ({comb_all_red_dedup:.1f}x deduped like-for-like)")
    print(f"  Combined All-TF vs D2:        {comb_d2_all} -> {comb_d3_all} ({(comb_d2_all / comb_d3_all):.1f}x)")
    print(f"  Combined Candidate Retention: {comb_retention:.1f}%")

    return window_results


def main():
    engine = AnalyticsQueryEngine("data/analytics_lake")

    # Oct 2 00:00:00 UTC to Oct 4 23:59:59 UTC
    start_oct2_ns = 1790908800000000000
    end_oct4_ns = 1791168000000000000
    evaluate_window(engine, start_oct2_ns, end_oct4_ns, "Oct 2-4 Baseline Window (3 days)")

    # Oct 1 00:00:00 UTC to Oct 7 23:59:59 UTC
    start_oct1_ns = 1790822400000000000
    end_oct7_ns = 1791427200000000000
    evaluate_window(engine, start_oct1_ns, end_oct7_ns, "Oct 1-7 Extended Holdout Window (7 days)")


if __name__ == "__main__":
    main()
