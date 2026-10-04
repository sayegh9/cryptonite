"""Institutional quantitative performance metrics calculation for backtest results."""

import bisect
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

# 2: equity marked at every bar close, Sharpe/Sortino on a full UTC daily grid, benchmark, fees and
# turnover (remediation R2.2/R2.3). Results without ``metrics_version`` are legacy (fill-only equity).
METRICS_VERSION = 2


def account_equity_series(
    balances: Iterable[tuple[int, str, float]],
    fills: list[tuple[int, float]],
    *,
    quote_currency: str,
    start_ns: int,
) -> list[tuple[int, float]]:
    """Equity over time from a cash account's balance rows, in the quote currency.

    A spot account reports one balance row per currency per account update (USDT and BTC, say).
    Equity at each update is the quote balance plus every other balance valued at the last fill price
    (balances only change on fills, so that price is the one the trade happened at). Treating each row's
    total as equity mixed BTC quantities into a USDT curve and reported a -100% return. One instrument
    per backtest, so every non-quote balance is its base asset.

    ``balances``: (ts_ns, currency, total) in report order; ``fills``: (ts_ns, price) of filled orders.
    """
    fills = sorted(fills)
    fill_ts = [ts for ts, _ in fills]
    held: dict[str, float] = {}
    series: list[tuple[int, float]] = []
    for ts_ns, currency, total in balances:
        held[currency] = total
        if ts_ns < start_ns:
            continue
        i = bisect.bisect_right(fill_ts, ts_ns)
        price = fills[i - 1][1] if i else (fills[0][1] if fills else 0.0)
        equity = held.get(quote_currency, 0.0) + sum(q * price for c, q in held.items() if c != quote_currency)
        if series and series[-1][0] == ts_ns:
            series[-1] = (ts_ns, equity)  # one point per update: the rows of one timestamp are one state
        else:
            series.append((ts_ns, equity))
    return series


def marked_equity_series(
    balances: Iterable[tuple[int, str, float]],
    fills: list[tuple[int, float]],
    closes: list[tuple[int, float]],
    *,
    quote_currency: str,
    start_ns: int,
    end_ns: int | None = None,
) -> list[tuple[int, float]]:
    """Mark-to-market equity: holdings from the balance rows (constant between fills), valued at every
    bar close of the evaluation window and at every account update.

    ``account_equity_series`` values holdings only at fills, so a trade that falls 15% and recovers
    between two fills shows no drawdown at all. Here each bar close is a point of the curve. At a
    timestamp with both a fill and a bar, the bar close is the mark. Without ``closes`` this is the
    fill-only curve of ``account_equity_series``.
    """
    events: list[tuple[int, int, Any, Any]] = []  # (ts, order, a, b): fills, then closes, then balances
    events += [(ts, 0, px, None) for ts, px in fills]
    events += [(ts, 1, px, None) for ts, px in closes]
    events += [(ts, 2, cur, total) for ts, cur, total in balances]
    events.sort(key=lambda e: (e[0], e[1]))  # stable: balance rows keep their report order

    held: dict[str, float] = {}
    price = 0.0
    series: list[tuple[int, float]] = []
    i = 0
    while i < len(events):
        ts = events[i][0]
        while i < len(events) and events[i][0] == ts:
            _, kind, a, b = events[i]
            if kind == 2:
                held[a] = b
            else:
                price = a
            i += 1
        if ts < start_ns or (end_ns is not None and ts > end_ns):
            continue
        equity = held.get(quote_currency, 0.0) + sum(q * price for c, q in held.items() if c != quote_currency)
        series.append((ts, equity))
    return series


_DAY_NS = 86_400 * 10**9


def daily_grid_returns(equity_curve: list[tuple[int, float]], initial_balance: float) -> list[float]:
    """Daily returns on the full UTC calendar grid from the first to the last point of the curve.

    Each day's equity is its last point, carried forward through days without points (0% return), so
    quiet days are not dropped from the Sharpe/Sortino sample. The first day is measured against
    ``initial_balance``.
    """
    if not equity_curve:
        return []
    by_day: dict[int, float] = {}
    for ts_ns, eq in equity_curve:
        by_day[ts_ns // _DAY_NS] = eq
    first, last = min(by_day), max(by_day)
    returns: list[float] = []
    prev = initial_balance
    curr = initial_balance
    for day in range(first, last + 1):
        curr = by_day.get(day, curr)
        if prev > 0:
            returns.append((curr - prev) / prev)
        prev = curr
    return returns


def sharpe_sortino(daily_returns: list[float]) -> tuple[float | None, float | None]:
    """Annualized (365 d) Sharpe and Sortino of daily returns, 0% risk-free; None when undefined."""
    if len(daily_returns) < 2:
        return None, None
    n = len(daily_returns)
    mean_ret = sum(daily_returns) / n
    std_dev = math.sqrt(sum((r - mean_ret) ** 2 for r in daily_returns) / (n - 1))
    sharpe = (mean_ret / std_dev) * math.sqrt(365.0) if std_dev > 1e-12 else None
    downside_variance = sum(min(r, 0.0) ** 2 for r in daily_returns) / n  # threshold 0
    sortino = (mean_ret / math.sqrt(downside_variance)) * math.sqrt(365.0) if downside_variance > 1e-12 else None
    return sharpe, sortino


def max_drawdown_of(values: Iterable[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for eq in values:
        peak = max(peak, eq)
        if peak > 0:
            worst = max(worst, (peak - eq) / peak)
    return worst


def benchmark_metrics(
    closes: list[tuple[int, float]],
    initial_balance: float,
    strategy_total_return: float,
) -> dict[str, float | None] | None:
    """Buy-and-hold of the instrument over the evaluation window (bought at the first close, no costs)."""
    closes = [(ts, c) for ts, c in sorted(closes) if c > 0]
    if not closes or initial_balance <= 0:
        return None
    c0 = closes[0][1]
    curve = [(ts, initial_balance * c / c0) for ts, c in closes]
    total_return = curve[-1][1] / initial_balance - 1.0
    sharpe, _ = sharpe_sortino(daily_grid_returns(curve, initial_balance))
    return {
        "total_return": round(total_return, 6),
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "max_drawdown": round(max_drawdown_of(eq for _, eq in curve), 4),
        "alpha_vs_hold": round(strategy_total_return - total_return, 6),
    }


def fees_and_turnover(
    orders: Iterable[tuple[float, float, list[tuple[float, str]]]],
    *,
    quote_currency: str,
    initial_balance: float,
) -> tuple[float, float]:
    """(fees paid in the quote currency, turnover as traded notional / initial balance).

    ``orders``: (filled_qty, avg_px, [(commission amount, currency), ...]) per order. A commission
    charged in the base asset is valued at the order's average price.
    """
    fees = 0.0
    notional = 0.0
    for qty, px, commissions in orders:
        notional += qty * px
        for amount, currency in commissions:
            fees += amount if currency == quote_currency else amount * px
    turnover = notional / initial_balance if initial_balance > 0 else 0.0
    return round(fees, 8), round(turnover, 6)


def calculate_performance_metrics(
    equity_curve: list[tuple[int, float]],
    trades: list[dict[str, Any]],
    initial_balance: float = 10000.0,
) -> dict[str, Any]:
    """Calculate institutional-grade financial metrics from the equity curve and trade list.
    
    Conventions:
    - 0% risk-free rate
    - 365-day annualization for cryptocurrency markets
    - Sharpe/Sortino from daily returns on a full UTC calendar grid (``daily_grid_returns``)
    - Max drawdown over every point of ``equity_curve`` (pass ``marked_equity_series`` for mark-to-market)
    - CAGR is None (NULL) if backtest evaluation span is less than 30 days
    - Profit factor is None (NULL) if there are 0 losing trades (prevents infinity)
    - Downsampled equity curve capped at 500 points preserving initial and final values
    - Monthly returns matrix structured as { "YYYY": { "M": return_float } }
    """
    if not equity_curve:
        final_equity = initial_balance
        equity_curve = [(0, initial_balance)]
    else:
        final_equity = equity_curve[-1][1]

    # 1. Total Return
    total_return = (final_equity - initial_balance) / initial_balance if initial_balance > 0 else 0.0

    # 2. Duration and CAGR
    start_ts_ns = equity_curve[0][0]
    end_ts_ns = equity_curve[-1][0]
    duration_s = max(0.0, (end_ts_ns - start_ts_ns) / 1e9)
    days = duration_s / 86400.0

    cagr: float | None = None
    if days >= 30.0 and initial_balance > 0 and final_equity > 0:
        years = days / 365.25
        try:
            cagr = (final_equity / initial_balance) ** (1.0 / years) - 1.0
        except (ValueError, OverflowError):
            cagr = None

    # 3. Maximum Drawdown & Drawdown series
    peak = -math.inf
    drawdowns: list[float] = []
    max_drawdown = 0.0

    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        drawdowns.append(dd)
        if dd > max_drawdown:
            max_drawdown = dd

    # 4. Daily returns for Sharpe and Sortino, on a full UTC daily grid (days without points count as 0%)
    daily_returns = daily_grid_returns(equity_curve, initial_balance)

    sharpe_ratio, sortino_ratio = sharpe_sortino(daily_returns)

    # 5. Trade metrics
    total_trades = len(trades)
    winning_trades = 0
    losing_trades = 0
    gross_profit = 0.0
    gross_loss = 0.0
    total_pnl = 0.0
    max_consecutive_losses = 0
    current_consecutive_losses = 0

    for t in trades:
        pnl = float(t.get("pnl", 0.0))
        total_pnl += pnl
        if pnl > 0:
            winning_trades += 1
            gross_profit += pnl
            current_consecutive_losses = 0
        elif pnl < 0:
            losing_trades += 1
            gross_loss += abs(pnl)
            current_consecutive_losses += 1
            if current_consecutive_losses > max_consecutive_losses:
                max_consecutive_losses = current_consecutive_losses
        else:
            # Breakeven
            current_consecutive_losses = 0

    win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
    avg_trade_pnl = total_pnl / total_trades if total_trades > 0 else 0.0

    # Profit factor: NULL if 0 losses
    profit_factor: float | None = None
    if losing_trades > 0 and gross_loss > 0:
        profit_factor = gross_profit / gross_loss

    # 6. Downsampled Equity Curve (max 500 points)
    downsampled_equity: list[dict[str, Any]] = []
    n_points = len(equity_curve)
    if n_points <= 500:
        for (ts_ns, eq), dd in zip(equity_curve, drawdowns, strict=False):
            downsampled_equity.append({
                "ts": int(ts_ns // 1e6),  # Unix ms for chart UI
                "equity": round(eq, 2),
                "drawdown": round(dd, 4),
            })
    else:
        indices = [int(i * (n_points - 1) / 499) for i in range(500)]
        # Deduplicate indices while preserving order
        seen = set()
        unique_indices = []
        for idx in indices:
            if idx not in seen:
                unique_indices.append(idx)
                seen.add(idx)
        for idx in unique_indices:
            ts_ns, eq = equity_curve[idx]
            dd = drawdowns[idx]
            downsampled_equity.append({
                "ts": int(ts_ns // 1e6),
                "equity": round(eq, 2),
                "drawdown": round(dd, 4),
            })

    # 7. Monthly Returns Heatmap Matrix
    monthly_returns: dict[str, dict[str, float]] = {}
    # Track month-start and month-end equities
    month_equities: dict[str, dict[str, list[float]]] = {}
    for ts_ns, eq in equity_curve:
        dt = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC)
        yr = str(dt.year)
        mo = str(dt.month)
        if yr not in month_equities:
            month_equities[yr] = {}
        if mo not in month_equities[yr]:
            month_equities[yr][mo] = []
        month_equities[yr][mo].append(eq)

    prev_month_close = initial_balance
    for yr in sorted(month_equities.keys(), key=int):
        monthly_returns[yr] = {}
        for mo in sorted(month_equities[yr].keys(), key=int):
            eq_series = month_equities[yr][mo]
            curr_month_close = eq_series[-1]
            if prev_month_close > 0:
                ret = (curr_month_close - prev_month_close) / prev_month_close
            else:
                ret = 0.0
            monthly_returns[yr][mo] = round(ret, 4)
            prev_month_close = curr_month_close

    return {
        "total_return": round(total_return, 6),
        "cagr": round(cagr, 6) if cagr is not None else None,
        "sharpe_ratio": round(sharpe_ratio, 4) if sharpe_ratio is not None else None,
        "sortino_ratio": round(sortino_ratio, 4) if sortino_ratio is not None else None,
        "max_drawdown": round(max_drawdown, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "win_rate": round(win_rate, 4),
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "avg_trade_pnl": round(avg_trade_pnl, 4),
        "max_consecutive_losses": max_consecutive_losses,
        "downsampled_equity": downsampled_equity,
        "monthly_returns": monthly_returns,
    }
