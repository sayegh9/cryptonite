"""Leverage replay of the closed AI-mandate spot trades (read-only research script).

Replays every CLOSED, non-PERP paper position owned by an ``AI_MANDATE_*`` instance as if it had been
opened on the Binance USD-M perpetual at 1x/3x/5x/10x ISOLATED, with the same entry/exit times and
prices. Writes ``leverage_replay_2026_10.json`` next to this file; the report
``leverage-replay-2026-10.md`` is written from that output.

Read-only by construction:
- Postgres: SELECT statements only, through ``docker exec infra-postgres-1 psql``.
- DuckDB lake (deploy-main/data, spot 1m bars): opened in-memory with read_parquet, memory_limit 1GB.
- Binance public endpoints (no key): /fapi/v1/fundingRate and /fapi/v1/markPriceKlines, cached as JSON
  under --cache. /fapi/v1/leverageBracket needs an API key, so XRP/WLD brackets are assumptions (below).

Run: <repo>/.venv/bin/python -I docs/research/leverage_replay_2026_10.py --cache <dir>
"""

import argparse
import csv
import io
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "packages" / "execution"))
sys.path.insert(0, str(REPO / "packages" / "schemas"))

import duckdb  # noqa: E402
import httpx  # noqa: E402
from cryptonite_execution import margin  # noqa: E402
from cryptonite_execution.derivatives import MarginMode, OrderSide  # noqa: E402

LAKE_1M = Path.home() / "Documents/projects/cryptonite/deploy-main/data/analytics_lake/binance_klines_enriched/binance/spot/1m"
LEVERAGES = (1, 3, 5, 10)
EQUITY = 10_000.0  # every mandate that traded has capital_budget_usd = 10,000
CAP_PCT = 0.25  # GuardedOrderDispatcher concentration invariant
RISK_PCTS = (0.005, 0.01)
PERP_TAKER = 0.0005  # Binance USD-M VIP0 taker; the spot paper engine charges 0.001
HARD_STOP_PCT = 0.05

# Assumed USD-M brackets for symbols margin.REFERENCE_BRACKETS lacks (leverageBracket needs a key).
# Shaped like Binance's published alt-coin tables; only the first one or two tiers are ever reached here.
ASSUMED_BRACKETS = {
    "XRPUSDT": [("0", "10000", 75, "0.005"), ("10000", "50000", 50, "0.0065"), ("50000", "500000", 25, "0.01"),
                ("500000", None, 10, "0.025")],
    "WLDUSDT": [("0", "5000", 50, "0.01"), ("5000", "25000", 25, "0.015"), ("25000", "100000", 20, "0.02"),
                ("100000", None, 10, "0.025")],
}


def psql(sql: str) -> list[dict]:
    assert sql.lstrip().upper().startswith(("SELECT", "WITH")), "read-only: SELECT only"
    out = subprocess.run(
        ["docker", "exec", "infra-postgres-1", "psql", "-U", "cryptonite", "-d", "cryptonite", "-v", "ON_ERROR_STOP=1",
         "-c", f"COPY ({sql}) TO STDOUT WITH CSV HEADER"],
        check=True, capture_output=True, text=True,
    ).stdout
    return list(csv.DictReader(io.StringIO(out)))


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace(" ", "T")).astimezone(UTC)


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


@dataclass
class Leg:
    t: datetime
    price: float
    qty: float


@dataclass
class Trade:
    position_id: str
    instance: str
    symbol: str
    legs: list[Leg]
    exit_t: datetime
    exit_price: float
    exit_qty: float
    spot_realized: float
    spot_fees: float
    exit_reason: str = "?"
    exit_cmd_t: datetime | None = None
    struct_stop: float | None = None
    take_profit: float | None = None
    path: list[tuple[int, float, float, float]] = field(default_factory=list)  # (close_ms, high, low, close)
    mark_path: list[tuple[int, float, float]] = field(default_factory=list)  # (open_ms, high, low)
    funding: list[tuple[int, float, float]] = field(default_factory=list)  # (time_ms, rate, mark)

    @property
    def qty(self) -> float:
        return sum(leg.qty for leg in self.legs)

    @property
    def entry(self) -> float:
        return sum(leg.qty * leg.price for leg in self.legs) / self.qty

    @property
    def cost(self) -> float:
        return sum(leg.qty * leg.price for leg in self.legs)


def load_trades() -> list[Trade]:
    positions = psql(
        "SELECT position_id, instance_id, instrument_id, realized_pnl, opened_at, closed_at FROM paper_positions "
        "WHERE status = 'CLOSED' AND instrument_id NOT LIKE '%PERP%' AND instance_id LIKE 'AI_MANDATE_%' ORDER BY opened_at"
    )
    fills = psql(
        "SELECT instance_id, instrument_id, side, price, quantity, commission, executed_at FROM paper_trades "
        "WHERE instrument_id NOT LIKE '%PERP%' AND instance_id LIKE 'AI_MANDATE_%' ORDER BY executed_at"
    )
    closes = psql(
        "SELECT created_at, processed_at, requester_id, payload->>'reason' AS reason, payload->>'instrument_id' AS instrument_id "
        "FROM execution_commands WHERE command_type = 'CLOSE_POSITION' AND created_at > '2026-10-01'"
    )
    entries = psql(
        "SELECT created_at, target_instance_id, payload->>'instrument_id' AS instrument_id, payload->>'side' AS side, "
        "COALESCE(payload->>'proposed_stop_loss', payload->>'stop_price') AS sl, "
        "COALESCE(payload->>'proposed_take_profit', payload->>'take_profit_price') AS tp FROM execution_commands "
        "WHERE command_type = 'SUBMIT_ORDER' AND status = 'COMPLETED' AND target_instance_id LIKE 'AI_MANDATE_%'"
    )
    trades = []
    for p in positions:
        lo, hi = ts(p["opened_at"]), ts(p["closed_at"])
        mine = [f for f in fills if f["instance_id"] == p["instance_id"] and f["instrument_id"] == p["instrument_id"]
                and lo - timedelta(seconds=1) <= ts(f["executed_at"]) <= hi + timedelta(seconds=1)]
        buys = [f for f in mine if f["side"] == "BUY"]
        sells = [f for f in mine if f["side"] == "SELL"]
        sell_qty = sum(float(f["quantity"]) for f in sells)
        main_sell = max(sells, key=lambda f: float(f["quantity"]))  # ignore the 0.000002 dust sell for timing
        t = Trade(
            position_id=p["position_id"], instance=p["instance_id"], symbol=p["instrument_id"].split(".")[0],
            legs=[Leg(ts(f["executed_at"]), float(f["price"]), float(f["quantity"])) for f in buys],
            exit_t=ts(main_sell["executed_at"]),
            exit_price=sum(float(f["price"]) * float(f["quantity"]) for f in sells) / sell_qty,
            exit_qty=sell_qty, spot_realized=float(p["realized_pnl"]),
            spot_fees=sum(float(f["commission"]) for f in mine),
        )
        for c in closes:
            if c["instrument_id"] == p["instrument_id"] and abs((ts(c["processed_at"]) - t.exit_t).total_seconds()) < 5:
                t.exit_reason = c["reason"] or ("MANUAL_CLOSE" if c["requester_id"] == "web-ui" else c["requester_id"])
                t.exit_cmd_t = ts(c["created_at"])
        if t.exit_reason == "?":
            t.exit_reason = "AI_SELL_CARD"  # 10-13 BTC: a Brain order card SELL, not a CLOSE_POSITION
        first = t.legs[0].t
        for e in entries:
            if (e["target_instance_id"] == p["instance_id"] and e["instrument_id"] == p["instrument_id"] and e["side"] == "BUY"
                    and abs((ts(e["created_at"]) - first).total_seconds()) < 7200 and ts(e["created_at"]) <= first):
                t.struct_stop = float(e["sl"]) if e["sl"] else None
                t.take_profit = float(e["tp"]) if e["tp"] else None
        trades.append(t)
    return trades


def load_paths(trades: list[Trade]) -> None:
    con = duckdb.connect(config={"memory_limit": "1GB", "threads": 2})
    for t in trades:
        start, end = t.legs[0].t, t.exit_t
        days = [(start + timedelta(days=i)).date() for i in range((end.date() - start.date()).days + 1)]
        globs = [str(LAKE_1M / t.symbol / f"date={d}" / "*.parquet") for d in days if (LAKE_1M / t.symbol / f"date={d}").exists()]
        # bar ts_event_ns = bar close; include the entry minute and the exit minute (conservative)
        rows = con.execute(
            f"SELECT DISTINCT ts_event_ns // 1000000 AS t, high::DOUBLE, low::DOUBLE, close::DOUBLE FROM read_parquet({globs}) "
            "WHERE ts_event_ns > ? AND ts_event_ns <= ? ORDER BY t",
            [ms(start) * 1_000_000, (ms(end) + 60_000) * 1_000_000],
        ).fetchall()
        t.path = [(int(a), b, c, d) for a, b, c, d in rows]


def binance_get(http: httpx.Client, cache: Path, path: str, params: dict):
    key = cache / (path.strip("/").replace("/", "_") + "_" + "_".join(f"{k}-{v}" for k, v in sorted(params.items())) + ".json")
    if key.exists():
        return json.loads(key.read_text())
    r = http.get(f"https://fapi.binance.com{path}", params=params, timeout=20)
    r.raise_for_status()
    key.write_text(r.text)
    time.sleep(0.2)
    return r.json()


def load_binance(trades: list[Trade], cache: Path) -> None:
    with httpx.Client() as http:
        for t in trades:
            s, e = ms(t.legs[0].t), ms(t.exit_t)
            fr = binance_get(http, cache, "/fapi/v1/fundingRate", {"symbol": t.symbol, "startTime": s, "endTime": e, "limit": 1000})
            t.funding = [(int(x["fundingTime"]), float(x["fundingRate"]), float(x["markPrice"])) for x in fr]
            cursor = s - s % 60_000
            while cursor <= e:
                kl = binance_get(http, cache, "/fapi/v1/markPriceKlines",
                                 {"symbol": t.symbol, "interval": "1m", "startTime": cursor, "limit": 1500})
                if not kl:
                    break
                t.mark_path += [(int(k[0]), float(k[2]), float(k[3])) for k in kl if int(k[0]) <= e]
                cursor = int(kl[-1][0]) + 60_000


def install_brackets(mmr_scale: Decimal = Decimal(1)) -> None:
    for sym, tiers in ASSUMED_BRACKETS.items():
        margin.REFERENCE_BRACKETS[sym] = margin._build_brackets(
            [(f, c, lev, str(Decimal(m) * mmr_scale)) for f, c, lev, m in tiers])


def liq_price(symbol: str, qty: float, entry: float, wallet: float) -> float | None:
    p = margin.calculate_liquidation_price(f"{symbol}-PERP.BINANCE", OrderSide.BUY, Decimal(str(qty)), Decimal(str(entry)),
                                           Decimal(str(wallet)), MarginMode.ISOLATED)
    return float(p) if p is not None and p > Decimal("1e-6") else None  # 1x long: no positive liquidation price


def replay(t: Trade, notional: float, lev: int, mark: bool = False) -> dict:
    """Open ``notional`` split over the real legs (same proportions), hold to the real exit, isolated margin.

    Returns liquidation level(s), first touch, funding and PnL. Liquidated loss = isolated margin + entry fee
    (the engine caps an isolated loss at the margin; the 0.5% clearance fee comes out of it).
    """
    scale = notional / t.cost
    legs = [(leg.t, leg.price, leg.qty * scale) for leg in t.legs]
    qty = wallet = cost = 0.0
    phases = []  # (from_ms, to_ms, liq)
    for i, (lt, px, q) in enumerate(legs):
        qty += q
        cost += q * px
        wallet += q * px / lev
        nxt = legs[i + 1][0] if i + 1 < len(legs) else t.exit_t
        phases.append((ms(lt), ms(nxt), liq_price(t.symbol, qty, cost / qty, wallet)))
    entry_fee = notional * PERP_TAKER
    liq_at = None
    if mark:
        for start, end, lp in phases:
            hit = next((k for k in t.mark_path if start - 60_000 < k[0] <= end and lp is not None and k[2] <= lp), None)
            if hit:
                liq_at = hit[0]
                break
    else:
        for start, end, lp in phases:
            hit = next((b for b in t.path if start < b[0] <= end + 60_000 and lp is not None and b[2] <= lp), None)
            if hit:
                liq_at = hit[0]
                break
    funding = 0.0
    for f_ms, rate, mk in t.funding:
        if liq_at is not None and f_ms >= liq_at:
            break
        held = sum(q for lt, _, q in legs if ms(lt) < f_ms)
        funding += -held * mk * rate  # positive rate: longs pay
    if liq_at is not None:
        pnl = -wallet - entry_fee
    else:
        pnl = qty * (t.exit_price - cost / qty) - entry_fee - qty * t.exit_price * PERP_TAKER + funding
    return {
        "lev": lev, "notional": notional, "margin": wallet, "liq": phases[-1][2], "liq_first_leg": phases[0][2],
        "liq_dist_pct": (1 - phases[-1][2] / (cost / qty)) * 100 if phases[-1][2] else None,
        "liquidated": liq_at is not None,
        "liq_time": datetime.fromtimestamp(liq_at / 1000, UTC).isoformat() if liq_at else None,
        "funding": funding, "pnl": pnl, "pnl_on_margin_pct": pnl / wallet * 100, "pnl_on_equity_pct": pnl / EQUITY * 100,
    }


def stop_distance(t: Trade) -> tuple[float, str]:
    if t.struct_stop and t.struct_stop < t.entry:
        return 1 - t.struct_stop / t.entry, "structural"
    return HARD_STOP_PCT, "hard 5%"


def price_at(t: Trade, when: datetime) -> float:
    """Last spot 1m close at or before ``when`` (mark kline low/high midpoint if the lake has a gap there)."""
    w = ms(when)
    bar = max((b for b in t.path if b[0] <= w), key=lambda b: b[0], default=None)
    if bar is not None and w - bar[0] <= 120_000:
        return bar[3]
    k = max((k for k in t.mark_path if k[0] <= w), key=lambda k: k[0])
    return (k[1] + k[2]) / 2


# Stops a model proposed that the exit monitor did not enforce at the moment the price crossed them.
INTENDED_STOP_NOTES = {
    "44e1ebfd": "structural stop 1.34 was on the entry command, but STRUCTURAL_STOP shipped on 2026-10-17 (906c550)",
    "a9059a28": "STRUCTURAL_STOP fired 2026-10-17 21:37 but sold only 0.000002 WLD; the position ran to the hard stop",
}


def slippage(t: Trade) -> dict | None:
    """Stop exits: the trigger level (hard/structural) or the price when the close command was raised
    (trailing: the monitor's best-price is not persisted), versus the fill, as % of the entry price."""
    if t.exit_reason not in ("HARD_STOP", "STRUCTURAL_STOP", "TRAILING_STOP"):
        return None
    cmd_t = t.exit_cmd_t or t.exit_t
    trigger = {"HARD_STOP": t.entry * (1 - HARD_STOP_PCT), "STRUCTURAL_STOP": t.struct_stop}.get(t.exit_reason)
    px_cmd = price_at(t, cmd_t)
    out = {
        "trigger": trigger, "px_at_command": px_cmd, "fill": t.exit_price,
        "delay_s": (t.exit_t - cmd_t).total_seconds(),
        "slip_vs_trigger_pct": (t.exit_price - trigger) / t.entry * 100 if trigger else None,
        "slip_vs_command_px_pct": (t.exit_price - px_cmd) / t.entry * 100,
        "move_pct": (t.exit_price / t.entry - 1) * 100,
    }
    note = INTENDED_STOP_NOTES.get(t.position_id[:8])
    if note and t.struct_stop:
        out["intended_stop"] = t.struct_stop
        out["gap_vs_intended_stop_pct"] = (t.exit_price - t.struct_stop) / t.entry * 100
        out["note"] = note
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    install_brackets()
    trades = load_trades()
    load_paths(trades)
    load_binance(trades, args.cache)
    out = []
    for t in trades:
        d, d_src = stop_distance(t)
        mae_bar = min((b[2] for b in t.path), default=t.entry)
        mfe_bar = max((b[1] for b in t.path), default=t.entry)
        mae_mark = min((k[2] for k in t.mark_path), default=t.entry)
        row = {
            "position_id": t.position_id, "instance": t.instance, "symbol": t.symbol,
            "entries": [(leg.t.isoformat(), leg.price, leg.qty) for leg in t.legs], "entry": t.entry, "spot_cost": t.cost,
            "exit_t": t.exit_t.isoformat(), "exit_cmd_t": t.exit_cmd_t.isoformat() if t.exit_cmd_t else None,
            "exit_price": t.exit_price, "exit_reason": t.exit_reason, "move_pct": (t.exit_price / t.entry - 1) * 100,
            "spot_realized": t.spot_realized, "spot_fees": t.spot_fees, "struct_stop": t.struct_stop, "take_profit": t.take_profit,
            "stop_dist_pct": d * 100, "stop_src": d_src, "bars": len(t.path), "mark_bars": len(t.mark_path),
            "expected_bars": int((ms(t.exit_t) - ms(t.legs[0].t)) / 60_000) + 1,
            "mae_pct": (mae_bar / t.entry - 1) * 100, "mfe_pct": (mfe_bar / t.entry - 1) * 100,
            "mae_mark_pct": (mae_mark / t.entry - 1) * 100,
            "funding_events": len(t.funding), "funding_rate_sum": sum(f[1] for f in t.funding),
            "slippage": slippage(t), "sizing": {},
        }
        sizings = {
            "A_cap25_mandate": lambda L: min(t.cost * L, CAP_PCT * EQUITY),
            "A_same_margin_uncapped": lambda L: t.cost * L,
        }
        for rp in RISK_PCTS:
            sizings[f"B_risk_{rp * 100:g}pct"] = lambda L, rp=rp: rp * EQUITY / d
        for name, fn in sizings.items():
            row["sizing"][name] = {str(L): replay(t, fn(L), L) for L in LEVERAGES}
            row["sizing"][name + "_markcheck"] = {str(L): replay(t, fn(L), L, mark=True)["liquidated"] for L in LEVERAGES}
        out.append(row)
    # MMR sensitivity: double the assumed alt MMRs, rule A at 10x
    install_brackets(Decimal(2))
    sens = {}
    for t, row in zip(trades, out):
        if t.symbol in ASSUMED_BRACKETS:
            r = replay(t, min(t.cost * 10, CAP_PCT * EQUITY), 10)
            sens[t.position_id] = {"liq": r["liq"], "liquidated": r["liquidated"]}
    # BTC/ETH funding cross-check against the DB table
    db_funding = psql(
        "SELECT instrument_id, funding_time_ns / 1000000 AS t, funding_rate AS rate FROM funding_rates "
        "WHERE funding_time_ns > 1791072000000000000 AND source = 'BINANCE_REST'"
    )
    xcheck = {}
    for t in trades:
        if t.symbol in ("BTCUSDT", "ETHUSDT"):
            db = {int(r["t"]) // 1000: float(r["rate"]) for r in db_funding if r["instrument_id"].startswith(t.symbol)}
            api = {f[0] // 1000: f[1] for f in t.funding}
            xcheck[t.position_id] = {"api_n": len(api), "db_match": sum(1 for k, v in api.items() if abs(db.get(k, 9) - v) < 1e-9)}
    (HERE / "leverage_replay_2026_10.json").write_text(json.dumps(
        {"generated_at": datetime.now(UTC).isoformat(), "equity": EQUITY, "perp_taker": PERP_TAKER,
         "assumed_brackets": ASSUMED_BRACKETS, "trades": out, "mmr_x2_sensitivity_10x": sens, "funding_db_xcheck": xcheck},
        indent=1, default=str))
    print(f"{len(out)} trades written")


if __name__ == "__main__":
    main()
