"""Build a labelled triage eval set from real market history (blueprint A6.2).

Each case is a market brief rendered by the production code path (lake 1m bars -> ``resample_1m_bars``
-> ``ScannerEngine.enrich_indicators`` -> ``build_brief``) at a sampled historical 15m close, with a
synthetic long candidate (entry = close, stop/target as ATR multiples) and a chosen data age. The label
applies the TRIAGE persona's own hard rules to the same numbers the brief shows:

  worth_debate = false  if reward-to-risk < 1.5, or the data is stale (> 2 bars of 15m), or both shown
                        timeframes trend down (against a long), or chop (15m volume <= 0.8x and
                        price inside the Bollinger bands)
  worth_debate = true   only for a clean setup: reward-to-risk >= 2, fresh data, no shown timeframe
                        trending down, at least one trending up, 15m volume >= 1.0x

Anything in between (reward-to-risk 1.5-2, data 16-30 min old, volume 0.8-1.0x inside the bands, no
timeframe trending up) is ambiguous by the persona's wording and is never emitted. False cases carry
one deciding reason each, so a model's misses point at the rule it misapplies.

    docker compose -f infra/docker-compose.yml run --rm --no-deps test \\
        uv run python scripts/build_triage_eval_set.py --out packages/agents/cryptonite_agents/evals/sets/triage_v3.jsonl

``--source synthetic`` (default) renders the same production brief code from controlled frames, so
every rule gets balanced coverage. ``--source lake`` samples real history instead; it needs a
gap-free 1m lake (backlog LE-1), otherwise the strict resampler rejects most windows.

Deterministic for a given seed (and, for ``lake``, the lake contents).
"""

import argparse
import json
import random
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from cryptonite_scanner.brief import _trend, build_brief, hard_rejects
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.resampler import resample_1m_bars
from cryptonite_storage.queries import AnalyticsQueryEngine

TF_NS = {"15m": 15 * 60 * 10**9, "1h": 3600 * 10**9}
HISTORY_DAYS = 10  # enough 1h bars for EMA-200 on most samples
RULES = ["ema_cross", "rsi_cross", "oversold_bounce", "breakout", "volume_spike"]


def _bb(row: dict) -> str:
    if row.get("bb_upper") is None or row.get("bb_lower") is None:
        return "n/a"
    return "above" if row["close"] > row["bb_upper"] else "below" if row["close"] < row["bb_lower"] else "inside"


def frames_at(df_1m: pl.DataFrame, end_ns: int) -> dict[str, pl.DataFrame] | None:
    window = df_1m.filter((pl.col("ts_event_ns") > end_ns - HISTORY_DAYS * 86400 * 10**9) & (pl.col("ts_event_ns") <= end_ns))
    frames = {}
    for tf in ("15m", "1h"):
        res = resample_1m_bars(window, tf)
        if res.is_empty():
            return None
        res = res.filter(pl.col("bar_close_inclusive_ns") <= end_ns)
        if res.height < 60:
            return None
        frames[tf] = ScannerEngine.enrich_indicators(res)
    last15 = frames["15m"].tail(1).to_dicts()[0]
    if end_ns - last15["bar_close_inclusive_ns"] > 60 * 10**9:  # the sampled close must be a real 15m close
        return None
    return frames


def label(rr: float, age_min: float, t15: str, t1h: str, vol15: float | None, bb15: str) -> tuple[bool | None, str]:
    stale, fresh = age_min > 30, age_min < 15
    # The rule part is the production one (the gate rejects the same cases without a model call, R3.1)
    rules = set(hard_rejects({
        "timeframes": [{"timeframe": "15m", "trend": t15, "volume_ratio": vol15,
                        "bands": bb15 if bb15 in ("inside", "above", "below") else None},
                       {"timeframe": "1h", "trend": t1h}],
        "candidate": {"timeframe": "15m", "reward_to_risk": rr},
    }))
    hard = [r for r, hit in (("reward_to_risk_below_1.5", "REWARD_TO_RISK" in rules), ("stale_data", stale),
                             ("both_timeframes_against", "TIMEFRAMES_AGAINST" in rules),
                             ("chop", "CHOP" in rules)) if hit]
    if hard:
        return False, hard[0] if len(hard) == 1 else "+".join(hard)
    clean = (rr >= 2.0 and fresh and "down" not in (t15, t1h) and "up" in (t15, t1h)
             and vol15 is not None and vol15 >= 1.0)
    return (True, "clean_setup") if clean else (None, "ambiguous")



# --- synthetic source -----------------------------------------------------------------------------

PRICE = {"BTCUSDT": (58_000, 72_000), "ETHUSDT": (2_900, 3_700)}
SCENARIOS = ["clean", "reward_to_risk_below_1.5", "stale_data", "both_timeframes_against", "chop"]


def synth_frame(rng: random.Random, last_close: float, trend: str, vol: float, bands: str, atr_pct: float) -> pl.DataFrame:
    """50 bars whose last row reads as ``trend`` / ``bands`` / ``vol`` through ``timeframe_line``."""
    change = {"up": rng.uniform(1.0, 4.0), "down": -rng.uniform(1.0, 4.0), "mixed": rng.uniform(-0.6, 0.6)}[trend]
    first = last_close / (1 + change / 100)
    closes = [first + (last_close - first) * i / 49 + rng.gauss(0, last_close * atr_pct / 300) for i in range(50)]
    closes[-1] = last_close
    c = last_close
    if trend == "up":
        e20, e50, e200 = c * 0.995, c * 0.988, c * 0.97
        rsi, macd = rng.uniform(56, 70), 1.0
    elif trend == "down":
        e20, e50, e200 = c * 1.005, c * 1.012, c * 1.03
        rsi, macd = rng.uniform(30, 44), -1.0
    else:  # close above the 20 EMA but the 20 below the 50: neither up nor down
        e20, e50, e200 = c * 0.998, c * 1.003, c * 0.99
        rsi, macd = rng.uniform(45, 55), rng.choice([1.0, -1.0])
    half = c * atr_pct / 100 * 2
    upper, lower = (c + half, c - half) if bands == "inside" else (c * 0.995, c * 0.98)
    rows = {"close": closes, "high": [x * 1.002 for x in closes], "low": [x * 0.998 for x in closes],
            "ema_20": [None] * 49 + [e20], "ema_50": [None] * 49 + [e50], "ema_200": [None] * 49 + [e200],
            "rsi": [None] * 49 + [rsi], "macd_hist": [None] * 49 + [macd], "bb_upper": [None] * 49 + [upper],
            "bb_lower": [None] * 49 + [lower], "atr": [None] * 49 + [c * atr_pct / 100],
            "volume_ratio": [None] * 49 + [vol]}
    return pl.DataFrame(rows)


def synth_case(rng: random.Random, scenario: str) -> dict:
    symbol = rng.choice(list(PRICE))
    close = round(rng.uniform(*PRICE[symbol]), 2)
    rr, age = rng.choice([2.0, 2.4, 3.0]), rng.choice([2, 5, 9, 12])
    t15, t1h = rng.choice([("up", "up"), ("up", "mixed"), ("mixed", "up")])
    vol15, bands = round(rng.uniform(1.05, 2.4), 2), rng.choice(["inside", "above"])
    if scenario == "reward_to_risk_below_1.5":
        rr = rng.choice([0.7, 1.0, 1.3])
    elif scenario == "stale_data":
        age = rng.choice([45, 75, 120])
    elif scenario == "both_timeframes_against":
        t15, t1h = "down", "down"
    elif scenario == "chop":
        vol15, bands = round(rng.uniform(0.3, 0.8), 2), "inside"
        t15, t1h = rng.choice([("mixed", "mixed"), ("mixed", "up"), ("up", "mixed")])
    worth, why = label(rr, age, t15, t1h, vol15, "inside" if bands == "inside" else "above")
    assert worth is not None and (why == scenario or (scenario == "clean" and why == "clean_setup")), (scenario, why)
    atr15, atr1h = rng.uniform(0.15, 0.45), rng.uniform(0.35, 0.9)
    frames = {"15m": synth_frame(rng, close, t15, vol15, bands, atr15),
              "1h": synth_frame(rng, close * rng.uniform(0.998, 1.002), t1h, round(rng.uniform(0.6, 1.6), 2),
                                rng.choice(["inside", "above"]), atr1h)}
    stop_mult = rng.choice([1.0, 1.5, 2.0])
    atr = close * atr15 / 100
    stop, target = close - stop_mult * atr, close + rr * stop_mult * atr
    bar_close = datetime(2026, 9, rng.randint(1, 28), rng.randint(0, 23), rng.choice([0, 15, 30, 45]), tzinfo=UTC)
    now = datetime.fromtimestamp(bar_close.timestamp() + age * 60, tz=UTC)
    brief = build_brief(f"{symbol}.BINANCE", frames, last_bar_close_ns=int(bar_close.timestamp() * 1e9), now=now,
                        candidate={"suggested_entry": close, "suggested_stop": stop, "suggested_target": target,
                                   "timeframe": "15m", "matched_rules": [rng.choice(RULES)]})
    return {"prompt": brief, "expect": {"worth_debate": [worth]}, "reason": why,
            "facts": {"rr": rr, "age_min": age, "trend_15m": t15, "trend_1h": t1h, "volume_15m": vol15, "bands_15m": bands}}


def synthetic_cases(rng: random.Random, per_class: int) -> list[dict]:
    cases = [synth_case(rng, "clean") for _ in range(per_class)]
    false_kinds = SCENARIOS[1:]
    cases += [synth_case(rng, false_kinds[i % len(false_kinds)]) for i in range(per_class)]
    rng.shuffle(cases)
    return cases


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--lake", default="data/analytics_lake")
    p.add_argument("--out", required=True)
    p.add_argument("--per-class", type=int, default=30)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--source", choices=["synthetic", "lake"], default="synthetic")
    p.add_argument("--max-states", type=int, default=400, help="lake: sampled 15m closes per symbol")
    p.add_argument("--since-days-ago", type=float, help="lake: only closes newer than this (a later-data holdout)")
    p.add_argument("--until-days-ago", type=float, help="lake: only closes older than this (training data)")
    args = p.parse_args()
    rng = random.Random(args.seed)
    if args.source == "synthetic":
        write(args.out, synthetic_cases(rng, args.per_class))
        return
    engine = AnalyticsQueryEngine(Path(args.lake))

    pool: dict[bool, list[dict]] = {True: [], False: []}
    reasons: Counter = Counter()
    for symbol in ("BTCUSDT", "ETHUSDT"):
        # bar_open_ns (= ts_event_ns - 1m) comes from query_bars_df; the resampler windows on it (B1 / F-C1:
        # sets built before B1 took the close as the open, one minute early, and must be regenerated)
        df = engine.query_bars_df(symbol=symbol, timeframe="1m")
        if df.is_empty():
            continue
        df = df.sort("bar_open_ns")
        lo, hi = int(df["ts_event_ns"].min()) + HISTORY_DAYS * 86400 * 10**9, int(df["ts_event_ns"].max())
        # 1m bars are stamped at their close, so a 15m bar closes exactly on a 15m boundary
        closes = list(range(lo - lo % TF_NS["15m"] + TF_NS["15m"], hi + 1, TF_NS["15m"]))
        now_ns = int(datetime.now(UTC).timestamp() * 1e9)
        if args.since_days_ago is not None:
            closes = [c for c in closes if c >= now_ns - args.since_days_ago * 86400e9]
        if args.until_days_ago is not None:
            closes = [c for c in closes if c < now_ns - args.until_days_ago * 86400e9]
        rng.shuffle(closes)
        for end_ns in closes[:args.max_states]:
            frames = frames_at(df, end_ns)
            if frames is None:
                continue
            r15, r1h = (frames[tf].tail(1).to_dicts()[0] for tf in ("15m", "1h"))
            atr = r15.get("atr")
            if not atr or not r15.get("close"):
                continue
            t15, t1h, vol15, bb15 = _trend(r15), _trend(r1h), r15.get("volume_ratio"), _bb(r15)
            for _ in range(3):  # a few candidate/age variants per market state
                rr = rng.choice([0.7, 1.0, 1.3, 2.0, 2.4, 3.0])
                age = rng.choice([2, 5, 9, 12, 45, 75, 120])
                worth, why = label(rr, age, t15, t1h, vol15, bb15)
                if worth is None:
                    continue
                entry = float(r15["close"])
                stop_mult = rng.choice([1.0, 1.5, 2.0])
                stop, target = entry - stop_mult * atr, entry + rr * stop_mult * atr
                close_ns = int(r15["bar_close_inclusive_ns"])
                now = datetime.fromtimestamp(close_ns / 1e9 + age * 60, tz=UTC)
                brief = build_brief(
                    f"{symbol}.BINANCE", frames, last_bar_close_ns=close_ns, now=now,
                    candidate={"suggested_entry": entry, "suggested_stop": stop, "suggested_target": target,
                               "timeframe": "15m", "matched_rules": [rng.choice(RULES)]},
                )
                pool[worth].append({"prompt": brief, "expect": {"worth_debate": [worth]}, "reason": why,
                                    "facts": {"rr": rr, "age_min": age, "trend_15m": t15, "trend_1h": t1h,
                                              "volume_15m": round(vol15, 2) if vol15 is not None else None,
                                              "bands_15m": bb15}})

    out, n = [], args.per_class
    for worth in (True, False):
        cases = pool[worth]
        rng.shuffle(cases)
        if worth is False:  # spread false cases across deciding reasons
            by_reason: dict[str, list[dict]] = {}
            for c in cases:
                by_reason.setdefault(c["reason"], []).append(c)
            picked, i = [], 0
            while len(picked) < n and any(by_reason.values()):
                key = sorted(by_reason)[i % len(by_reason)]
                if by_reason[key]:
                    picked.append(by_reason[key].pop())
                i += 1
            cases = picked
        for c in cases[:n]:
            reasons[c["reason"]] += 1
            out.append(c)
    rng.shuffle(out)
    print(json.dumps({"pool": {str(k): len(v) for k, v in pool.items()}}))
    write(args.out, out)


def write(path: str, cases: list[dict]) -> None:
    with open(path, "w") as f:
        for i, c in enumerate(cases, 1):
            f.write(json.dumps({"id": f"triage-v3-{i}", "kind": "triage", "persona": "triage", "prompt": c["prompt"],
                                "expect": c["expect"], "label_reason": c["reason"], "facts": c["facts"]}) + "\n")
    print(json.dumps({"written": len(cases), "by_reason": dict(Counter(c["reason"] for c in cases))}, indent=1))


if __name__ == "__main__":
    main()
