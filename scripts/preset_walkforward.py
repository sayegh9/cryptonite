"""Walk-forward of scanner presets on NET R (AI Phase 2, Track A2/A3). Read-only on the analytics lake.

Every preset signal is filled at the open of the bar after the signal and closed twice: by the preset
stop/target (a bar touching both counts as the stop) and by the profile exits (5% hard stop, 2% trail
after +3%, max hold by timeframe). Both are net of cost model v1 (``cryptonite_scanner.costs``: spot for
longs, perp for shorts). Variants are preset × timeframe × direction × 1h regime at signal time × exit
policy. They are chosen on the oldest two thirds of the data window (train trades must exit before the
cutoff) and reported on the newest third (test signals at or after the cutoff) with a day-clustered
bootstrap 90% interval, the trade count and an overlap-adjusted count (one open position per coin).

Modes:
  ``--mode report`` (default) writes the walk-forward report.
  ``--mode replay`` writes ``preset_replay.json`` (schema_version 1, unchanged row schema) from the
  test-window NET results under the profile exits. It is only an input to a later gate; nothing is
  unticked automatically.

Run inside the scanner image with the lake mounted read-only (as for ``scripts/preset_replay.py``):

    mkdir -p replay && docker run --rm --memory 2g \
      -v <analytics_lake>:/lake:ro \
      -v "$PWD/scripts/preset_walkforward.py:/app/preset_walkforward.py:ro" \
      -v "$PWD/scripts/preset_replay.py:/app/preset_replay.py:ro" \
      -v "$PWD/services/debate_worker/candidate_outcomes.py:/app/candidate_outcomes.py:ro" \
      -v "$PWD/replay:/out" \
      "$(docker compose -f infra/docker-compose.yml images -q scanner)" \
      python /app/preset_walkforward.py --lake /lake --coins BTCUSDT,ETHUSDT \
      --since 2026-04-15 --mode replay --out /out/preset_replay.json
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean
from typing import Any

_HERE = Path(__file__).resolve().parent
for _path in (_HERE.parent / "services" / "debate_worker", _HERE):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import polars as pl  # noqa: E402
from candidate_outcomes import (  # noqa: E402
    HOLD_HOURS,
    HOUR_NS,
    PROFILE_STOP_PCT,
    PROFILE_TRAIL_ACTIVATE_PCT,
    PROFILE_TRAIL_PCT,
    net_labels,
)
from cryptonite_scanner.costs import (  # noqa: E402
    COSTS,
    DEFAULT_FUNDING_PER_8H,
    cost_model_name,
)
from cryptonite_scanner.preset_stats import (  # noqa: E402
    ReplayTrade,
    _evaluate_candidates,
    _higher_timeframe,
    _prepare_bars,
    _requires_htf,
    summarize_replay,
)
from cryptonite_scanner.presets import _PROFILE_PRESETS  # noqa: E402
from cryptonite_scanner.regime import regime_series  # noqa: E402

DAY_NS = 86_400_000_000_000
POLICIES = ("preset", "profile")
TRAIN_FRACTION = 2 / 3
MIN_TRAIN_TRADES = 10
BOOTSTRAP_SAMPLES = 2000
REGIME_WARMUP_HOURS = 1000
CI_LEVEL = 0.90


# ---------------------------------------------------------------- pure statistics (tested)


def trade_end_ns(trade: dict[str, Any]) -> int:
    """The last moment a trade is open under either exit policy (its signal time when never filled)."""
    ends = [trade.get(f"exit_ns_{p}") for p in POLICIES]
    return max([e for e in ends if e is not None], default=trade["signal_ns"])


def window_cutoff(first_ns: int, last_ns: int, train_fraction: float = TRAIN_FRACTION) -> int:
    """The chronological split point: the oldest ``train_fraction`` of the data window is training."""
    return first_ns + int((last_ns - first_ns) * train_fraction)


def split_chronological(trades: list[dict[str, Any]], cutoff_ns: int) -> tuple[list[dict], list[dict]]:
    """Train = trades fully closed before the cutoff; test = trades signalled at or after it. Trades that
    straddle the cutoff are dropped from both, so no training outcome depends on test-window prices."""
    train = [t for t in trades if trade_end_ns(t) < cutoff_ns]
    test = [t for t in trades if t["signal_ns"] >= cutoff_ns]
    return train, test


def day_cluster_bootstrap(trades: list[dict[str, Any]], key: str, *, n_boot: int = BOOTSTRAP_SAMPLES,
                          level: float = CI_LEVEL, seed: int = 0) -> tuple[float, float] | None:
    """Percentile interval of the mean of ``key`` resampling whole UTC signal days with replacement.
    ``None`` with fewer than two days (no spread to resample)."""
    days: dict[int, list[float]] = defaultdict(lambda: [0.0, 0])
    for t in trades:
        value = t.get(key)
        if value is None:
            continue
        day = days[t["signal_ns"] // DAY_NS]
        day[0] += value
        day[1] += 1
    clusters = list(days.values())
    k = len(clusters)
    if k < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        total = count = 0.0
        for _ in range(k):
            s, c = clusters[rng.randrange(k)]
            total += s
            count += c
        means.append(total / count)
    means.sort()
    alpha = (1 - level) / 2
    return means[int(alpha * (n_boot - 1))], means[round((1 - alpha) * (n_boot - 1))]


def overlap_adjusted_count(trades: list[dict[str, Any]], policy: str) -> int:
    """Trades left when each coin holds at most one open position: a signal is taken only after the
    previous taken trade on that coin has exited under ``policy``."""
    net_key, exit_key = f"net_r_{policy}", f"exit_ns_{policy}"
    free_at: dict[str, int] = {}
    taken = 0
    for t in sorted((t for t in trades if t.get(net_key) is not None), key=lambda t: (t["signal_ns"], t["coin"])):
        if t["signal_ns"] >= free_at.get(t["coin"], -1):
            taken += 1
            free_at[t["coin"]] = t[exit_key]
    return taken


def summarize(trades: list[dict[str, Any]], policy: str, *, n_boot: int = BOOTSTRAP_SAMPLES,
              seed: int = 0) -> dict[str, Any]:
    """Net R statistics of one exit policy over ``trades``."""
    key = f"net_r_{policy}"
    used = [t for t in trades if t.get(key) is not None]
    values = [t[key] for t in used]
    ci = day_cluster_bootstrap(used, key, n_boot=n_boot, seed=seed)
    return {
        "trades": len(values),
        "overlap_adjusted_trades": overlap_adjusted_count(used, policy),
        "days": len({t["signal_ns"] // DAY_NS for t in used}),
        "mean_net_r": mean(values) if values else None,
        "win_rate": sum(v > 0 for v in values) / len(values) if values else None,
        "ci90": list(ci) if ci else None,
        "ci_includes_zero": ci is None or ci[0] <= 0 <= ci[1],
    }


def variant_key(trade: dict[str, Any]) -> tuple[str, str, str, str]:
    return trade["preset"], trade["timeframe"], trade["direction"], trade["regime"]


def walkforward(trades: list[dict[str, Any]], cutoff_ns: int, *, min_train_trades: int = MIN_TRAIN_TRADES,
                n_boot: int = BOOTSTRAP_SAMPLES, seed: int = 0) -> dict[str, Any]:
    """Choose variants on the train window only (at least ``min_train_trades`` and a positive mean net R),
    then report every tried variant, every preset and the chosen set on the test window."""
    train, test = split_chronological(trades, cutoff_ns)
    train_by: dict[tuple, list] = defaultdict(list)
    test_by: dict[tuple, list] = defaultdict(list)
    for t in train:
        train_by[variant_key(t)].append(t)
    for t in test:
        test_by[variant_key(t)].append(t)
    variants: list[dict[str, Any]] = []
    chosen_test: dict[str, list] = {p: [] for p in POLICIES}
    for key in sorted(train_by):
        for policy in POLICIES:
            fitted = [t for t in train_by[key] if t.get(f"net_r_{policy}") is not None]
            if not fitted:
                continue
            chosen = len(fitted) >= min_train_trades and mean(t[f"net_r_{policy}"] for t in fitted) > 0
            if chosen:
                chosen_test[policy].extend(test_by.get(key, []))
            variants.append({
                "preset": key[0], "timeframe": key[1], "direction": key[2], "regime": key[3],
                "exit_policy": policy, "chosen": chosen,
                "train": summarize(fitted, policy, n_boot=n_boot, seed=seed),
                "test": summarize(test_by.get(key, []), policy, n_boot=n_boot, seed=seed),
            })
    presets = []
    for slug in sorted({t["preset"] for t in trades}):
        for policy in POLICIES:
            stats = summarize([t for t in test if t["preset"] == slug], policy, n_boot=n_boot, seed=seed)
            presets.append({"preset": slug, "exit_policy": policy, "test": stats})
    return {
        "train_trades": len(train),
        "test_trades": len(test),
        "purged_straddling_trades": len(trades) - len(train) - len(test),
        "variants_tried": len(variants),
        "variants_chosen": sum(v["chosen"] for v in variants),
        "selection_rule": f"train trades >= {min_train_trades} and train mean net R > 0",
        "chosen_test": {p: summarize(chosen_test[p], p, n_boot=n_boot, seed=seed) for p in POLICIES},
        "variants": variants,
        "presets": presets,
        "flagged_presets": [{"preset": p["preset"], "exit_policy": p["exit_policy"]}
                            for p in presets if p["test"]["ci_includes_zero"]],
    }


def regime_at(ends_ns: list[int], tags: list[str | None], signal_ns: int) -> str:
    """The regime of the last 1h bar closed at or before ``signal_ns`` (``UNKNOWN`` when none)."""
    i = bisect.bisect_right(ends_ns, signal_ns) - 1
    return (tags[i] or "UNKNOWN") if i >= 0 else "UNKNOWN"


def finish_trade(signal: dict[str, Any], rows: list[dict[str, Any]], bar_hours: float) -> dict[str, Any]:
    """Close one signal over the bars after it (``rows``, oldest first) under both exit policies."""
    net = net_labels(rows, signal["direction"], signal["stop"], signal["target"], bar_hours)
    events = [int(r["ts_event_ns"]) for r in rows]
    return {
        **signal,
        "entry_next_open": net["entry_next_open"],
        "cost_model": net["cost_model"],
        "net_r_preset": net["net_r_preset"],
        "net_r_profile": net["net_r_profile"],
        "exit_ns_preset": events[net["preset_bars"] - 1] if net["preset_bars"] else None,
        "exit_ns_profile": events[net["profile_bars"] - 1] if net["profile_bars"] else None,
    }


def assumptions(window: dict[str, Any]) -> dict[str, Any]:
    """The replay assumptions: cost model v1 (numbers read from ``costs.py``), entry, exits and window."""
    return {
        "cost_model": f"{cost_model_name('spot')} for LONG, {cost_model_name('perp')} for SHORT",
        "market": "spot for LONG, perp for SHORT",
        "fee_per_side": {m: COSTS[m].fee_per_side for m in ("spot", "perp")},
        "slippage_per_side": {m: COSTS[m].slippage_per_side for m in ("spot", "perp")},
        "funding": f"perp: default {DEFAULT_FUNDING_PER_8H} per 8 h against the position",
        "entry": "open of the bar after the signal",
        "exit_policy": "profile (net_r_profile)",
        "profile_stop_loss_pct": PROFILE_STOP_PCT,
        "global_trail_activate_pct": PROFILE_TRAIL_ACTIVATE_PCT,
        "global_trail_pct": PROFILE_TRAIL_PCT,
        "max_hold_hours": HOLD_HOURS,
        "r_unit": "distance from the fill to the preset stop",
        "bars": "signal timeframe; a bar touching both stop and target counts as the stop",
        "metric": "test window only, every signal (no variant selection, overlap not removed)",
        "window": window,
    }


def _iso(ns: int | None) -> str | None:
    return None if ns is None else datetime.fromtimestamp(ns / 1e9, tz=UTC).isoformat()


def replay_document(trades: list[dict[str, Any]], pairs: list[tuple[str, str]],
                    ranges: dict[tuple[str, str], tuple[int, int]], cutoff_ns: int,
                    window: dict[str, Any]) -> dict[str, Any]:
    """``preset_replay.json`` (schema_version 1) from the test-window net results under profile exits."""
    _, test = split_chronological(trades, cutoff_ns)
    results = []
    for slug, coin in pairs:
        rows = [ReplayTrade(t["signal_ns"], t["exit_ns_profile"], t["net_r_profile"]) for t in test
                if t["preset"] == slug and t["coin"] == coin and t["net_r_profile"] is not None]
        first, last = ranges.get((slug, coin), (None, None))
        if last is not None and last < cutoff_ns:
            first = last = None
        elif first is not None:
            first = max(first, cutoff_ns)
        results.append(summarize_replay(slug, coin, rows, first, last))
    return {"schema_version": 1, "assumptions": assumptions(window), "results": results}


# ---------------------------------------------------------------- lake replay (read-only)


def _regime_lookup(reader, coin: str, since_ns: int) -> Callable[[int], str]:
    bars = reader.read_window(coin, "1h", since_ns - REGIME_WARMUP_HOURS * HOUR_NS, 2**62)
    if bars.is_empty():
        return lambda _t: "UNKNOWN"
    prepared = _prepare_bars(bars, "1h").sort("interval_end_exclusive_ns")
    ends = [int(v) for v in prepared["interval_end_exclusive_ns"]]
    tags = regime_series(prepared)
    return lambda t: regime_at(ends, tags, t)


def stream_trades(reader, preset, coin: str, timeframe: str, since_ns: int,
                  regime_of: Callable[[int], str]) -> tuple[list[dict], int | None, int | None, int]:
    """Replay one preset stream batch by batch (as ``preset_stats.replay_batches``), closing each signal
    once its max-hold bars have been seen. Returns trades, first/last bar and the censored signal count."""
    from preset_replay import INDICATOR_CONTEXT_BARS, timeframe_ns

    tf_ns = timeframe_ns(timeframe)
    bar_hours = tf_ns / HOUR_NS
    max_bars = max(1, HOLD_HOURS.get(timeframe, 72) * HOUR_NS // tf_ns)
    side = (preset.risk.direction or "LONG").upper()
    htf_tf = _higher_timeframe(timeframe) if _requires_htf(preset) else None
    initial = reader.read_window(coin, timeframe, since_ns - INDICATOR_CONTEXT_BARS * tf_ns, since_ns - 1,
                                 limit=INDICATOR_CONTEXT_BARS, descending=True)
    context = _prepare_bars(initial, timeframe) if not initial.is_empty() else pl.DataFrame()
    context_end_ns = int(context["ts_event_ns"][-1]) if not context.is_empty() else None
    pending: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    trades: list[dict[str, Any]] = []
    first_ns = last_ns = None
    for batch in reader.iter_batches(coin, timeframe, since_ns):
        prepared_batch = _prepare_bars(batch, timeframe)
        if prepared_batch.is_empty():
            continue
        first_ns = first_ns if first_ns is not None else int(prepared_batch["ts_event_ns"][0])
        last_ns = int(prepared_batch["ts_event_ns"][-1])
        combined = pl.concat([context, prepared_batch]) if not context.is_empty() else prepared_batch
        htf = None
        if htf_tf is not None:
            start = int(combined["bar_open_ns"].min()) - INDICATOR_CONTEXT_BARS * timeframe_ns(htf_tf)
            htf = reader.read_window(coin, htf_tf, start, int(combined["interval_end_exclusive_ns"].max()))
        candidates, prepared = _evaluate_candidates(combined, preset, coin, timeframe, htf, 8)
        index = {int(v): i for i, v in enumerate(prepared["interval_end_exclusive_ns"])}
        signals_at: dict[int, list] = {}
        for c in candidates:
            end_ns = int(c.interval_end_exclusive_ns)
            if context_end_ns is not None and end_ns <= context_end_ns + 1_000_000:
                continue
            i = index.get(end_ns)
            if i is not None:
                signals_at.setdefault(int(prepared[i, "ts_event_ns"]), []).append(c)
        for row in prepared.filter(pl.col("ts_event_ns") > (context_end_ns or -1)).to_dicts():
            event_ns = int(row["ts_event_ns"])
            still = []
            for signal, rows in pending:
                rows.append(row)
                if len(rows) >= max_bars:
                    trades.append(finish_trade(signal, rows, bar_hours))
                else:
                    still.append((signal, rows))
            pending = still
            for c in signals_at.get(event_ns, []):
                signal_ns = int(c.interval_end_exclusive_ns)
                pending.append(({
                    "preset": preset.slug, "coin": coin, "timeframe": timeframe, "direction": side,
                    "regime": regime_of(signal_ns), "signal_ns": signal_ns,
                    "stop": None if c.suggested_stop is None else float(c.suggested_stop),
                    "target": None if c.suggested_target is None else float(c.suggested_target),
                }, []))
            context_end_ns = event_ns
        context = prepared.tail(INDICATOR_CONTEXT_BARS)
    return trades, first_ns, last_ns, len(pending)


def run(lake: Path, coins: list[str], since: date) -> dict[str, Any]:
    """Replay every registered preset on the requested coins; returns trades and data ranges."""
    from preset_replay import LakeReader

    since_ns = int(datetime.combine(since, datetime.min.time(), UTC).timestamp()) * 1_000_000_000
    trades: list[dict[str, Any]] = []
    ranges: dict[tuple[str, str], tuple[int, int]] = {}
    pairs = [(p.slug, coin) for p in _PROFILE_PRESETS.values() for coin in coins]
    censored = 0
    with LakeReader(lake) as reader:
        regimes = {coin: _regime_lookup(reader, coin, since_ns) for coin in coins}
        for preset in _PROFILE_PRESETS.values():
            configured = {i.split(".", maxsplit=1)[0].upper() for i in preset.universe.instrument_ids}
            for coin in coins:
                if coin not in configured:
                    continue
                for timeframe in preset.universe.timeframes:
                    got, first, last, open_left = stream_trades(reader, preset, coin, timeframe, since_ns,
                                                                regimes[coin])
                    trades.extend(got)
                    censored += open_left
                    if first is not None:
                        old = ranges.get((preset.slug, coin))
                        ranges[(preset.slug, coin)] = (min(first, old[0]), max(last, old[1])) if old else (first, last)
    return {"trades": trades, "ranges": ranges, "pairs": pairs, "censored": censored}


def build(data: dict[str, Any], mode: str, train_fraction: float, min_train_trades: int,
          n_boot: int, seed: int) -> dict[str, Any]:
    ranges = data["ranges"]
    if not ranges:
        raise SystemExit("no bars found for the requested coins and dates")
    first_ns = min(r[0] for r in ranges.values())
    last_ns = max(r[1] for r in ranges.values())
    cutoff = window_cutoff(first_ns, last_ns, train_fraction)
    window = {"data_start": _iso(first_ns), "train_end": _iso(cutoff), "test_start": _iso(cutoff),
              "data_end": _iso(last_ns), "train_fraction": round(train_fraction, 4),
              "split": "chronological; train trades exit before the cutoff, test trades signal at or after it"}
    if mode == "replay":
        doc = replay_document(data["trades"], data["pairs"], ranges, cutoff, window)
    else:
        doc = {"schema_version": 1, "assumptions": assumptions(window),
               "censored_signals": data["censored"],
               **walkforward(data["trades"], cutoff, min_train_trades=min_train_trades, n_boot=n_boot, seed=seed)}
    doc["generated_at"] = datetime.now(UTC).isoformat()
    doc["source_command"] = "python " + " ".join(sys.argv)
    return doc


def main() -> None:
    from preset_replay import _validate_coins

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lake", required=True, type=Path, help="Analytics lake root (read-only)")
    parser.add_argument("--coins", required=True, help="Comma-separated spot symbols, e.g. BTCUSDT,ETHUSDT")
    parser.add_argument("--since", required=True, type=date.fromisoformat, help="First UTC date (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, type=Path, help="Output JSON file")
    parser.add_argument("--mode", choices=("report", "replay"), default="report")
    parser.add_argument("--train-fraction", type=float, default=TRAIN_FRACTION)
    parser.add_argument("--min-train-trades", type=int, default=MIN_TRAIN_TRADES)
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP_SAMPLES)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    try:
        coins = _validate_coins(args.coins)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if not 0 < args.train_fraction < 1:
        parser.error("--train-fraction must be between 0 and 1")
    if args.out.resolve().is_relative_to(args.lake.resolve()):
        parser.error("--out must resolve outside --lake; the analytics lake is read-only")
    doc = build(run(args.lake, coins, args.since), args.mode, args.train_fraction, args.min_train_trades,
                args.bootstrap, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
