#!/usr/bin/env python3
"""Phase 2 simulator: one paper portfolio per entry policy (A rules, B current AI, C narrow veto), read-only.

Joins ``policy_decisions`` to ``candidate_outcomes`` and replays every labelled TAKE in signal order through the
real profile sizing rules: 1% risk of equity (times the policy's size factor), a 10% position limit, the 1%-of-equity
minimum notional for perp and margin (spot has none), one position per coin and at most 3 open. A TAKE whose coin
is held or whose portfolio is full is skipped. Each policy runs twice, once on ``net_r_preset`` and once on
``net_r_profile``. Exit time is ``bars_to_exit`` bars after the signal (else ``max_hold_hours``): the labeller keeps
one exit time per candidate, so the profile-exit run shares the preset run's holding time.

Outputs per policy and exit set: trades, net R, net P&L, max drawdown (R and %), turnover, win rate, the day-clustered
bootstrap 90% interval of mean net R, and the opportunity cost (the net R of the policy's labelled SKIPs).

Usage: DATABASE_URL=... python scripts/policy_sim.py [--since 2026-11-01] [--until 2026-12-31] [--seed 7]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cryptonite_scanner.costs import default_market

START_EQUITY = 10_000.0
RISK_PCT = 0.01
MAX_POSITION_PCT = 0.10
MIN_NOTIONAL_PCT = 0.01  # perp and margin; the risk engine's fixed floor
MAX_OPEN = 3
POLICIES = ("A", "B", "C")
EXIT_SETS = ("net_r_preset", "net_r_profile")
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


@dataclass(frozen=True, slots=True)
class Decision:
    candidate_id: str
    policy: str
    decision: str  # TAKE / SKIP
    side: str | None
    size_factor: float
    instrument_id: str
    entry_at: datetime
    exit_at: datetime
    entry: float
    stop: float
    net_r: dict[str, float | None]


@dataclass(slots=True)
class Result:
    policy: str
    exit_set: str
    trades: int = 0
    skipped_held: int = 0
    skipped_full: int = 0
    skipped_min_notional: int = 0
    net_r: float = 0.0
    net_pnl: float = 0.0
    max_dd_r: float = 0.0
    max_dd_pct: float = 0.0
    turnover: float = 0.0
    wins: int = 0
    ci90: tuple[float, float] | None = None
    skip_count: int = 0
    skip_net_r: float = 0.0
    trade_rs: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None


def size_trade(equity: float, entry: float, stop: float, size_factor: float, market: str) -> tuple[float, float] | None:
    """(notional, risk_usd) under the profile rules, or None when the order is below the minimum notional."""
    stop_pct = abs(entry - stop) / entry if entry > 0 else 0.0
    if stop_pct <= 0 or size_factor <= 0:
        return None
    notional = min(equity * RISK_PCT * size_factor / stop_pct, equity * MAX_POSITION_PCT)
    if market != "spot" and notional < equity * MIN_NOTIONAL_PCT:
        return None
    return notional, notional * stop_pct


def day_clustered_ci(trade_rs: list[tuple[datetime, float]], *, n_boot: int = 2000, seed: int = 7) -> tuple[float, float] | None:
    """90% bootstrap interval of mean net R per trade, resampling whole UTC days."""
    by_day: dict[object, list[float]] = {}
    for at, r in trade_rs:
        by_day.setdefault(at.astimezone(UTC).date(), []).append(r)
    days = list(by_day.values())
    if len(days) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample = [r for _ in days for r in rng.choice(days)]
        means.append(sum(sample) / len(sample))
    means.sort()
    return means[int(0.05 * n_boot)], means[int(0.95 * n_boot) - 1]


def simulate(decisions: list[Decision], policy: str, exit_set: str, *, seed: int = 7) -> Result:
    res = Result(policy, exit_set)
    rows = sorted((d for d in decisions if d.policy == policy), key=lambda d: (d.entry_at, d.candidate_id))
    equity = START_EQUITY
    peak_equity = START_EQUITY
    cum_r = peak_r = 0.0
    open_pos: dict[str, tuple[datetime, float, float]] = {}  # instrument -> (exit_at, pnl, r)

    def close_until(t: datetime) -> None:
        nonlocal equity, peak_equity, cum_r, peak_r
        for inst, (exit_at, pnl, r) in sorted(open_pos.items(), key=lambda kv: kv[1][0]):
            if exit_at <= t:
                del open_pos[inst]
                equity += pnl
                cum_r += r
                peak_equity = max(peak_equity, equity)
                peak_r = max(peak_r, cum_r)
                res.max_dd_pct = max(res.max_dd_pct, (peak_equity - equity) / peak_equity)
                res.max_dd_r = max(res.max_dd_r, peak_r - cum_r)

    for d in rows:
        r = d.net_r.get(exit_set)
        if r is None:
            continue
        if d.decision == "SKIP":
            res.skip_count += 1
            res.skip_net_r += r
            continue
        if d.decision != "TAKE":
            continue
        close_until(d.entry_at)
        if d.instrument_id in open_pos:
            res.skipped_held += 1
            continue
        if len(open_pos) >= MAX_OPEN:
            res.skipped_full += 1
            continue
        sized = size_trade(equity, d.entry, d.stop, d.size_factor, default_market(d.side))
        if sized is None:
            res.skipped_min_notional += 1
            continue
        notional, risk_usd = sized
        r_units = risk_usd / (equity * RISK_PCT)  # a capped or half-size trade risks less than 1R
        pnl = r * risk_usd
        open_pos[d.instrument_id] = (d.exit_at, pnl, r * r_units)
        res.trades += 1
        res.net_r += r * r_units
        res.net_pnl += pnl
        res.turnover += 2 * notional / START_EQUITY
        res.wins += pnl > 0
        res.trade_rs.append((d.entry_at, r * r_units))
    close_until(datetime.max.replace(tzinfo=UTC))
    res.ci90 = day_clustered_ci(res.trade_rs, seed=seed)
    return res


QUERY = """
SELECT pd.candidate_id::text, pd.policy, pd.decision, COALESCE(pd.side, co.direction) AS side,
       COALESCE(pd.size_factor, 1) AS size_factor, sc.instrument_id, sc.timeframe, sc.interval_end_exclusive_ns,
       COALESCE(co.entry_next_open, co.entry, sc.suggested_entry) AS entry, COALESCE(co.stop, sc.suggested_stop) AS stop,
       co.bars_to_exit, co.max_hold_hours, co.net_r_preset, co.net_r_profile
FROM policy_decisions pd
JOIN signal_candidates sc ON sc.id = pd.candidate_id
JOIN candidate_outcomes co ON co.candidate_id = pd.candidate_id
WHERE pd.decision IN ('TAKE', 'SKIP') AND pd.decided_at >= $1 AND pd.decided_at < $2
"""


def to_decision(row) -> Decision:
    entry_at = datetime.fromtimestamp(row["interval_end_exclusive_ns"] / 1e9, tz=UTC)
    if row["bars_to_exit"] is not None:
        hold = timedelta(minutes=TF_MINUTES.get(row["timeframe"], 60) * max(int(row["bars_to_exit"]), 1))
    else:
        hold = timedelta(hours=int(row["max_hold_hours"] or 24))
    f = lambda v: None if v is None else float(v)  # noqa: E731
    return Decision(
        candidate_id=row["candidate_id"], policy=row["policy"], decision=row["decision"], side=row["side"],
        size_factor=float(row["size_factor"]), instrument_id=row["instrument_id"], entry_at=entry_at,
        exit_at=entry_at + hold, entry=float(row["entry"]), stop=float(row["stop"]),
        net_r={"net_r_preset": f(row["net_r_preset"]), "net_r_profile": f(row["net_r_profile"])},
    )


def render(results: list[Result], since: datetime, until: datetime) -> str:
    out = [
        f"# Policy simulation {since:%Y-%m-%d} to {until:%Y-%m-%d}",
        "",
        f"$10,000 per policy; 1% risk, 10% position limit, perp/margin minimum 1% of equity, "
        f"one position per coin, at most {MAX_OPEN} open. Cost model v1.",
        "",
        "| Policy | Exits | Trades | Net R | Mean R | 90% CI (day-clustered) | Net P&L | Max DD R | Max DD % "
        "| Turnover | Win rate | Skipped held/full/min | SKIPs | SKIP net R |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        mean = f"{r.net_r / r.trades:+.3f}" if r.trades else "–"
        ci = f"[{r.ci90[0]:+.3f}, {r.ci90[1]:+.3f}]" if r.ci90 else "–"
        wr = f"{r.win_rate:.0%}" if r.win_rate is not None else "–"
        out.append(
            f"| {r.policy} | {r.exit_set.removeprefix('net_r_')} | {r.trades} | {r.net_r:+.2f} | {mean} | {ci} "
            f"| {r.net_pnl:+,.0f} | {r.max_dd_r:.2f} | {r.max_dd_pct:.1%} | {r.turnover:.1f}x | {wr} "
            f"| {r.skipped_held}/{r.skipped_full}/{r.skipped_min_notional} | {r.skip_count} | {r.skip_net_r:+.2f} |"
        )
    out += ["", "Gate P bar: mean net R > 0 with the 90% CI lower bound above −0.05R, max drawdown < 10R, "
            "policy A ≥ 100 closed trades."]
    return "\n".join(out)


async def load(db_url: str, since: datetime, until: datetime) -> list[Decision]:
    import asyncpg

    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute("SET default_transaction_read_only = on")
        return [to_decision(r) for r in await conn.fetch(QUERY, since, until)]
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--since", type=datetime.fromisoformat, default=datetime(2026, 11, 1, tzinfo=UTC))
    p.add_argument("--until", type=datetime.fromisoformat, default=None)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    a = p.parse_args()
    if not a.db_url:
        print("DATABASE_URL or --db-url is required", file=sys.stderr)
        return 2
    since = a.since if a.since.tzinfo else a.since.replace(tzinfo=UTC)
    until = a.until or datetime.now(UTC)
    until = until if until.tzinfo else until.replace(tzinfo=UTC)
    decisions = asyncio.run(load(a.db_url.replace("postgresql+asyncpg://", "postgresql://"), since, until))
    results = [simulate(decisions, pol, ex, seed=a.seed) for pol in POLICIES for ex in EXIT_SETS]
    print(render(results, since, until))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
