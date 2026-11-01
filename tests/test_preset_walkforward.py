"""Walk-forward split, selection and statistics (AI Phase 2, Track A2/A3)."""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import preset_walkforward as wf  # noqa: E402

DAY = wf.DAY_NS
HOUR = wf.HOUR_NS


def trade(signal_ns, hold_ns, net, *, coin="BTCUSDT", preset="p", regime="CHOP"):
    return {"preset": preset, "coin": coin, "timeframe": "1h", "direction": "LONG", "regime": regime,
            "signal_ns": signal_ns, "net_r_preset": net, "net_r_profile": net,
            "exit_ns_preset": signal_ns + hold_ns, "exit_ns_profile": signal_ns + hold_ns}


def synthetic(n=400, seed=1):
    rng = random.Random(seed)
    return [trade(rng.randrange(0, 90 * DAY), rng.randrange(HOUR, 72 * HOUR), rng.uniform(-2, 2),
                  coin=rng.choice(["BTCUSDT", "ETHUSDT"]), preset=rng.choice(["a", "b"]),
                  regime=rng.choice(["CHOP", "TREND_UP"])) for _ in range(n)]


def test_split_never_leaks():
    trades = synthetic()
    cutoff = wf.window_cutoff(0, 90 * DAY)
    assert cutoff == 60 * DAY
    train, test = wf.split_chronological(trades, cutoff)
    assert train and test
    assert all(wf.trade_end_ns(t) < cutoff for t in train)
    assert all(t["signal_ns"] >= cutoff for t in test)
    assert max(wf.trade_end_ns(t) for t in train) < min(t["signal_ns"] for t in test)
    assert not {id(t) for t in train} & {id(t) for t in test}
    straddler = trade(cutoff - HOUR, 2 * HOUR, 1.0)
    train2, test2 = wf.split_chronological([straddler], cutoff)
    assert train2 == [] and test2 == []


def test_selection_depends_on_train_window_only():
    trades = synthetic()
    cutoff = wf.window_cutoff(0, 90 * DAY)
    base = wf.walkforward(trades, cutoff, n_boot=50)
    shuffled = [dict(t) for t in trades]
    for t in shuffled:
        if t["signal_ns"] >= cutoff:
            t["net_r_preset"] = t["net_r_profile"] = -t["net_r_preset"] * 10
    again = wf.walkforward(shuffled, cutoff, n_boot=50)
    pick = [(v["preset"], v["regime"], v["exit_policy"], v["chosen"], v["train"]["mean_net_r"])
            for v in base["variants"]]
    assert pick == [(v["preset"], v["regime"], v["exit_policy"], v["chosen"], v["train"]["mean_net_r"])
                    for v in again["variants"]]
    assert base["variants_tried"] == len(base["variants"]) == 2 * 2 * 2  # preset × regime × policy
    assert base["train_trades"] + base["test_trades"] + base["purged_straddling_trades"] == len(trades)


def test_flags_presets_whose_test_interval_includes_zero():
    winners = [trade(d * DAY, HOUR, 1.0 + 0.01 * d, preset="good") for d in range(60, 90)]
    noisy = [trade(d * DAY, HOUR, 1.0 if d % 2 else -1.0, preset="flat") for d in range(60, 90)]
    out = wf.walkforward(winners + noisy, 60 * DAY, n_boot=200)
    flagged = {f["preset"] for f in out["flagged_presets"]}
    assert flagged == {"flat"}


def test_overlap_adjusted_count_one_position_per_coin():
    trades = [trade(0, 10 * HOUR, 1.0), trade(HOUR, HOUR, 1.0), trade(10 * HOUR, HOUR, 1.0),
              trade(HOUR, HOUR, 1.0, coin="ETHUSDT")]
    assert wf.overlap_adjusted_count(trades, "preset") == 3


def test_day_cluster_bootstrap():
    assert wf.day_cluster_bootstrap([trade(0, HOUR, 1.0), trade(HOUR, HOUR, 2.0)], "net_r_preset") is None
    trades = [trade(d * DAY, HOUR, 0.5 + 0.1 * (d % 3)) for d in range(20)]
    lo, hi = wf.day_cluster_bootstrap(trades, "net_r_preset", n_boot=300, seed=3)
    assert 0.5 <= lo <= hi <= 0.7
    assert wf.day_cluster_bootstrap(trades, "net_r_preset", n_boot=300, seed=3) == (lo, hi)


def test_regime_at_uses_last_closed_1h_bar():
    ends = [HOUR, 2 * HOUR, 3 * HOUR]
    tags = ["CHOP", None, "TREND_UP"]
    assert wf.regime_at(ends, tags, HOUR - 1) == "UNKNOWN"
    assert wf.regime_at(ends, tags, HOUR) == "CHOP"
    assert wf.regime_at(ends, tags, 2 * HOUR + 5) == "UNKNOWN"
    assert wf.regime_at(ends, tags, 10 * HOUR) == "TREND_UP"


def test_replay_document_keeps_schema_and_states_assumptions():
    trades = [trade(d * DAY, HOUR, 1.0, preset="a") for d in range(0, 90, 3)]
    window = {"train_end": "x"}
    doc = wf.replay_document(trades, [("a", "BTCUSDT"), ("a", "ETHUSDT")],
                             {("a", "BTCUSDT"): (0, 90 * DAY)}, 60 * DAY, window)
    assert doc["schema_version"] == 1
    assert doc["assumptions"]["cost_model"] == "spot-v1 for LONG, perp-v1 for SHORT"
    assert doc["assumptions"]["window"] == window
    btc, eth = doc["results"]
    assert set(btc) == {"preset", "coin", "trades", "win_rate", "avg_r", "median_r", "max_drawdown_r",
                        "first_bar", "last_bar"}
    assert btc["trades"] == 10 and btc["avg_r"] == 1.0
    assert eth["trades"] == 0 and eth["first_bar"] is None
