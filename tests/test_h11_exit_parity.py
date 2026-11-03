"""H11: one exit simulator for labels and replays, matching the live exit monitor.

``simulate_live_exit`` (``cryptonite_schemas.exit_rules``) is checked against a tick-by-tick emulation of
``exit_monitor.monitor_once``: each 1m bar is walked as dense ticks open -> adverse extreme -> favourable extreme
-> close, and every tick goes through the exit monitor's own ``evaluate_exit`` and ``max_hold_action`` with the best
price tracked as ``best_price_since_open`` does. Both must close at the same bar and price (within one tick).
"""

import asyncio
import datetime
import sys
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import asyncpg
import polars as pl
import pytest
from cryptonite_scanner.costs import cost_in_r, round_trip_cost_fraction
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_schemas.exit_rules import DEFAULT_THRESHOLDS, LiveExitSettings, simulate_live_exit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
import candidate_outcomes as co  # noqa: E402

TICKS = 40
PROFILE = LiveExitSettings(tp_r=2.0, tp1_scale_pct=50.0)  # every production profile on 2026-11-03


def walk(*prices: float, per_bar: int = 1) -> list[dict]:
    """1m bars along straight lines between ``prices``: ``per_bar`` bars per unit step of 0.1."""
    path = [prices[0]]
    for a, b in zip(prices, prices[1:], strict=False):
        n = max(1, round(abs(b - a) / 0.1) * per_bar)
        path += [a + (b - a) * i / n for i in range(1, n + 1)]
    return [{"open": o, "high": max(o, c), "low": min(o, c), "close": c} for o, c in zip(path, path[1:], strict=False)]


def flat(price: float, n: int) -> list[dict]:
    return [{"open": price, "high": price, "low": price, "close": price}] * n


def monitor_replay(bars, side, stop, target, settings, t=DEFAULT_THRESHOLDS):
    """The live monitor, one evaluation per tick: fills as (fraction, price, bar)."""
    entry = float(bars[0]["open"])
    pos = settings.position("ETHUSDT.BINANCE", side, entry, stop, target)
    pos = em.apply_profile_exit_levels(replace(pos, take_profit_price=target))
    pt = em.position_thresholds(pos, t)
    long = side == "LONG"
    best, tp1_done, remaining, fills = entry, False, 1.0, []
    for n, b in enumerate(bars, start=1):
        o, hi, lo, c = b["open"], b["high"], b["low"], b["close"]
        legs = (o, lo, hi, c) if long else (o, hi, lo, c)
        ticks = [o] + [a + (z - a) * i / TICKS for a, z in zip(legs, legs[1:], strict=False) for i in range(1, TICKS + 1)]
        for tick in ticks:
            best = max(best, tick) if long else min(best, tick)
            actions = em.evaluate_exit(replace(pos, current_price=tick), best, False, True, pt, tp1_done=tp1_done)
            if any(a.kind == "CLOSE" for a in actions):
                return fills + [(remaining, tick, n)], next(a.reason for a in actions if a.kind == "CLOSE")
            if any(a.kind == "REDUCE_TP1" for a in actions):
                frac = float(Decimal(em.tp1_reduce_quantity(pos)))
                fills.append((frac, tick, n))
                remaining -= frac
                tp1_done = True
        if em.max_hold_action(pos, now_ts=pos.opened_ts + n * 60) is not None:
            return fills + [(remaining, c, n)], "MAX_HOLD"
    return fills + [(remaining, bars[-1]["close"], len(bars))], "EXPIRED"


def assert_parity(bars, side, stop, target, settings=PROFILE, t=DEFAULT_THRESHOLDS, expect=None):
    sim = simulate_live_exit(bars, side, stop, target, settings=settings, thresholds=t)
    live, reason = monitor_replay(bars, side, stop, target, settings, t)
    tick = max(b["high"] - b["low"] for b in bars) / TICKS + 1e-9
    assert sim.outcome == reason
    assert [(f, n) for f, _, n in sim.fills] == [(f, n) for f, _, n in live]
    for (_, ps, _), (_, pl_, _) in zip(sim.fills, live, strict=True):
        assert abs(ps - pl_) <= tick
    if expect is not None:
        assert sim.outcome == expect
    return sim


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_structural_stop(side):
    s = 1 if side == "LONG" else -1
    sim = assert_parity(walk(100, 100 + 0.3 * s, 100 - 1.6 * s), side, 100 - s, 100 + 1.5 * s,
                        expect="STRUCTURAL_STOP")
    assert sim.fills[0][1] == pytest.approx(100 - s)
    assert sim.gross_r(side, 100 - s) == pytest.approx(-1.0)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_tp1_partial_then_remainder_on_the_structural_stop(side):
    s = 1 if side == "LONG" else -1
    sim = assert_parity(walk(100, 100 + 2.4 * s, 100 - 1.5 * s), side, 100 - s, 100 + 1.5 * s,
                        expect="STRUCTURAL_STOP")
    assert [round(p, 6) for _, p, _ in sim.fills] == [100 + 2 * s, 100 - s]  # TP1 at the profile's 2R
    assert sim.gross_r(side, 100 - s) == pytest.approx(0.5)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_tp1_then_trailing_stop(side):
    s = 1 if side == "LONG" else -1
    sim = assert_parity(walk(100, 100 + 4.0 * s, 100), side, 100 - s, 100 + 1.5 * s, expect="TRAILING_STOP")
    best = 100 + 4.0 * s
    assert sim.fills[-1][1] == pytest.approx(best * (1 - s * 0.02))


def test_hard_stop_is_the_backstop_on_a_wide_structural_stop():
    sim = assert_parity(walk(100, 93), "LONG", 90.0, 115.0, expect="HARD_STOP")
    assert sim.fills[0][1] == pytest.approx(95.0)


def test_gap_beyond_the_stop_fills_at_the_open():
    bars = walk(100, 100.5) + [{"open": 98.4, "high": 98.6, "low": 98.0, "close": 98.2}]
    sim = assert_parity(bars, "LONG", 99.0, 101.5, expect="STRUCTURAL_STOP")
    assert sim.fills[0][1] == 98.4


def test_same_bar_stop_and_tp1_is_a_stop():
    bars = [{"open": 100, "high": 100.2, "low": 99.8, "close": 100},
            {"open": 100, "high": 102.5, "low": 98.9, "close": 101}]
    sim = assert_parity(bars, "LONG", 99.0, 101.5, expect="STRUCTURAL_STOP")
    assert sim.tp1_bars is None and len(sim.fills) == 1


def test_profile_max_hold_and_trail_values():
    settings = LiveExitSettings(tp_r=2.0, tp1_scale_pct=25.0, trail_activate_pct=1.0, trail_pct=0.5, max_hold_hours=2)
    assert_parity(walk(100, 100.4) + flat(100.4, 200), "LONG", 99.0, 101.5, settings, expect="MAX_HOLD")
    sim = assert_parity(walk(100, 102.2, 101.0), "LONG", 99.0, 101.5, settings, expect="TRAILING_STOP")
    assert sim.fills[0][0] == 0.25 and sim.fills[0][1] == pytest.approx(101.0 + 1.0)
    assert sim.fills[-1][1] == pytest.approx(102.2 * 0.995)


def test_no_profile_tp_r_scales_out_at_the_entry_target():
    sim = assert_parity(walk(100, 101.7, 100.4) + flat(100.4, 10), "LONG", 99.0, 101.5, LiveExitSettings(),
                        expect="EXPIRED")
    assert sim.fills[0][:2] == (0.5, pytest.approx(101.5))


def test_stored_global_thresholds_apply():
    t = replace(DEFAULT_THRESHOLDS, stop_loss_pct=1.0)
    assert_parity(walk(100, 98.5), "LONG", 97.0, 104.0, t=t, expect="HARD_STOP")


# The three labelled production candidates of 2026-11-01 (ETH 15m, ETH 15m, BTC 15m):
# fill, preset stop, preset target, net_r_preset, net_r_profile as stored.
PRODUCTION = [
    (2271.62, 2261.02, 2287.52, 0.8553, -2.3285),
    (2289.25, 2275.17, 2310.37, 1.0122, -2.5915),
    (78485.43, 78149.34, 78989.59, -1.7006, -4.4575),
]


@pytest.mark.parametrize("entry,stop,target,preset,profile", PRODUCTION)
def test_production_geometry_live_label_sits_by_the_preset_label(entry, stop, target, preset, profile):
    """Target cases: the price reaches the preset target and the profile's 2R TP1, then gives back to +1R and the
    24h hold runs out. Stop case: the preset stop is hit. The old profile label lost several R on the same
    geometry because it ignored the structural stop; the live label is within a fraction of an R of the preset."""
    risk = entry - stop
    hit = target > entry and preset > 0
    if hit:
        legs = [entry, entry + 2.2 * risk, entry + 1.0 * risk]
    else:
        legs = [entry, entry + 0.5 * risk, stop - 0.5 * risk]
    step = risk / 10
    path = [legs[0]]
    for a, b in zip(legs, legs[1:], strict=False):
        n = max(1, round(abs(b - a) / step))
        path += [a + (b - a) * i / n for i in range(1, n + 1)]
    bars = [{"open": o, "high": max(o, c), "low": min(o, c), "close": c} for o, c in zip(path, path[1:], strict=False)]
    bars += flat(path[-1], 1440 - len(bars))
    net = co.net_labels(bars, "LONG", stop, target, bar_hours=1 / 60)
    live = co.live_label(bars, "LONG", stop, target, 1 / 60, PROFILE)
    if hit:
        assert net["net_r_preset"] == pytest.approx(preset, abs=0.01)
        # 50% at 2R + 50% expired at +1R = 1.5R gross, the preset's own 1.5R target
        assert live["net_r_live"] == pytest.approx(net["net_r_preset"], abs=0.05)
    else:
        assert live["live_outcome"] == "STRUCTURAL_STOP"
        assert live["net_r_live"] == pytest.approx(net["net_r_preset"], abs=0.01)
    assert abs(live["net_r_live"] - preset) < 0.1
    assert live["net_r_live"] - profile > 2.0  # the old 5%-stop label is several R below


def test_live_label_costs_weight_the_hold_by_open_quantity():
    bars = walk(100, 102.1, 100.0)  # TP1 at 2R, then the remainder expires flat
    live = co.live_label(bars, "LONG", 99.0, 101.5, 1 / 60, PROFILE)
    sim = simulate_live_exit(bars, "LONG", 99.0, 101.5, settings=PROFILE)
    held_h = sim.held_bars() / 60
    assert sim.held_bars() < len(bars)
    assert live["net_r_live"] == pytest.approx(sim.gross_r("LONG", 99.0) - cost_in_r(100.0, 99.0, round_trip_cost_fraction("spot", held_h)))
    assert live["live_exit_model"] == "live-v1 tp2r/50% trail3/2 hard5 hold-"


def test_live_label_needs_a_stop_on_the_loss_side_of_the_fill():
    bars = [{"open": 98.5, "high": 99, "low": 98, "close": 98.5}]
    assert co.live_label(bars, "LONG", 99.0, 101.5, 1 / 60)["net_r_live"] is None
    assert co.live_label([], "LONG", 99.0, 101.5, 1 / 60)["live_outcome"] == "NO_DATA"


def test_settings_follow_the_coin_not_the_market():
    by_base = {"ETH": PROFILE}
    assert co._settings_for(by_base, "ETHUSDT-PERP.BINANCE") is PROFILE
    assert co._settings_for(by_base, "BTCUSDT.BINANCE") == LiveExitSettings()


class _Lake:
    def __init__(self, bars: pl.DataFrame):
        self.bars = bars

    def query_bars_df(self, symbol, timeframe, start_ns, end_ns):
        return self.bars


def test_backfill_is_idempotent_and_leaves_the_057_columns():
    from migrated_db import migrated_database

    async def scenario(url: str):
        conn = await asyncpg.connect(url)
        spec, version, run, cand = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        start = 1_793_574_900_000_000_000
        await conn.execute("INSERT INTO scan_specs (id, slug, name) VALUES ($1, 'h11', 'h11')", spec)
        await conn.execute("INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules) "
                           "VALUES ($1, $2, 1, 'h11', '[]', '[]')", version, spec)
        await conn.execute(
            "INSERT INTO scan_runs (id, spec_version_id, spec_hash, scanner_version, indicator_version, "
            "input_watermark_ns, source_partitions, source_partition_checksums, trigger_reason) "
            "VALUES ($1, $2, 'h11', 't', 't', 0, '[]', '{}', 'COMMITTED_BAR_EVENT')", run, version)
        await conn.execute(
            "INSERT INTO signal_candidates (id, scan_run_id, spec_version_id, instrument_id, timeframe, bar_open_ns, "
            "bar_close_inclusive_ns, interval_end_exclusive_ns, matched_rules, metrics, suggested_entry, "
            "suggested_stop, suggested_target) VALUES ($1, $2, $3, 'ETHUSDT.BINANCE', '15m', $4, $4, $4, '[]', '{}', "
            "100, 99, 101.5)", cand, run, version, start)
        await conn.execute(
            "INSERT INTO candidate_outcomes (candidate_id, direction, entry, stop, target, outcome, realized_r, "
            "bars_to_exit, max_hold_hours, entry_next_open, net_r_preset, net_r_profile, cost_model) "
            "VALUES ($1, 'LONG', 100, 99, 101.5, 'STOP', -1, 3, 24, 100, -1.3, -5.3, 'spot-v1')", cand)
        bars = walk(100, 98.5) + flat(98.5, 1430)  # a full 24 h window (MIN_COVERAGE)
        lake = _Lake(pl.DataFrame([{**b, "ts_event_ns": start + (i + 1) * 60_000_000_000} for i, b in enumerate(bars)]))
        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        try:
            dry = await co.backfill_live_labels(pool, lake, dry_run=True)
            assert [u[2] for u in dry] == ["STRUCTURAL_STOP"]
            assert await conn.fetchval("SELECT live_exit_model FROM candidate_outcomes") is None
            assert len(await co.backfill_live_labels(pool, lake)) == 1
            assert await co.backfill_live_labels(pool, lake) == []
            row = await conn.fetchrow("SELECT * FROM candidate_outcomes WHERE candidate_id = $1", cand)
            assert (row["net_r_preset"], row["net_r_profile"], row["cost_model"]) == (
                Decimal("-1.3"), Decimal("-5.3"), "spot-v1")
            assert row["live_outcome"] == "STRUCTURAL_STOP" and row["live_bars"] == 10
            assert float(row["net_r_live"]) == pytest.approx(-1 - cost_in_r(100, 99, round_trip_cost_fraction("spot", 10 / 60)))
            assert row["live_exit_model"].startswith("live-v1 tp-r/50%")
            # a partial window is not labelled while the lake may still be repaired, then it is NO_DATA
            await conn.execute("UPDATE candidate_outcomes SET live_exit_model = NULL, live_outcome = NULL, "
                               "net_r_live = NULL, live_bars = NULL")
            short = _Lake(lake.bars.head(100))
            assert await co.backfill_live_labels(pool, short, now=datetime.datetime(2026, 11, 3, tzinfo=datetime.UTC)) == []
            late = await co.backfill_live_labels(pool, short, now=datetime.datetime(2026, 11, 20, tzinfo=datetime.UTC))
            assert [(u[1], u[2]) for u in late] == [(None, "NO_DATA")]
        finally:
            await pool.close()
            await conn.close()

    with migrated_database("cryptonite_h11") as url:
        asyncio.run(scenario(url))


def test_brief_labels_the_old_replay_and_shows_the_live_parity_figure():
    from cryptonite_scanner import brief
    from test_market_brief import frame

    cand = {"timeframe": "1h", "spec_slug": "rsi-oversold-reversal", "suggested_entry": "149.8",
            "suggested_stop": "148.8", "suggested_target": "152.8", "matched_rules": []}
    frames = {"1h": frame(), "4h": frame()}
    data = brief.brief_data("BTCUSDT.BINANCE", frames, candidate=cand)
    live = brief._LIVE_REPLAY[("BTCUSDT", "rsi-oversold-reversal")]
    assert data["replay"]["exit_model"] == "5pct-stop"
    assert data["replay"]["live"]["avg_r"] == pytest.approx(live["avg_r"])
    line = next(x for x in brief.build_brief("BTCUSDT.BINANCE", frames, candidate=cand).splitlines()
                if x.startswith("Direction:"))
    assert "5%-stop model, not live exits" in line
    assert f"live-parity exits: {live['trades']} trades" in line and f"avg {live['avg_r']:+.2f}R" in line


def test_a_gap_through_both_stops_keeps_the_monitors_reason():
    bars = walk(100, 100.5) + [{"open": 94.0, "high": 94.2, "low": 93.5, "close": 94.0}]
    sim = assert_parity(bars, "LONG", 96.0, 104.0, expect="HARD_STOP")  # evaluate_exit's reason at the gapped open
    assert sim.fills[0][1] == 94.0


def test_a_full_tp1_scale_out_ends_the_trade_there():
    settings = LiveExitSettings(tp_r=2.0, tp1_scale_pct=100.0)
    bars = walk(100, 102.4, 98.0)
    sim = simulate_live_exit(bars, "LONG", 99.0, 101.5, settings=settings)
    assert sim.outcome == "TP1_FULL" and len(sim.fills) == 1 and sim.fills[0][:2] == (1.0, pytest.approx(102.0))
    assert sim.bars_to_exit == sim.tp1_bars


def test_a_market_card_shows_the_profile_r_rule_not_the_card_target():
    from cryptonite_schemas.exit_rules import enforced_tp1

    tp1 = enforced_tp1("LONG", None, 99.0, 101.5, 2, 50)
    assert (tp1.price, tp1.basis) == (None, "PROFILE_R")
    assert tp1.text(101.5) == ("TP1 (50% at 2R): set from the fill and the structural stop when the order fills; "
                               "card target 101.5 is informational")
