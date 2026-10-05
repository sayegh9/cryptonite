"use client";

import React from "react";
import { BacktestMetrics } from "@/lib/types";
import { AlertCircle, TrendingUp, TrendingDown } from "lucide-react";
import { fmtSignedPct, isLegacyMetrics, metricNum, metricsVersion } from "@/lib/backtestMetrics";

interface Props {
  metrics: BacktestMetrics;
  executionProfile: string;
}

export const TearsheetCards: React.FC<Props> = ({ metrics, executionProfile }) => {
  const isPositive = metrics.total_return >= 0;
  const version = metricsVersion(metrics);
  const bench = metrics.benchmark ?? null;
  const fees = metricNum(metrics.fees_paid);
  const turnover = metricNum(metrics.turnover);
  const benchReturn = metricNum(bench?.total_return);
  const benchSharpe = metricNum(bench?.sharpe_ratio);
  const benchDd = metricNum(bench?.max_drawdown);
  const alpha = metricNum(bench?.alpha_vs_hold);

  return (
    <div className="flex flex-col space-y-3 w-full font-mono">
      <div className="flex flex-wrap items-center gap-2 text-2xs" data-testid="metrics-version">
        {isLegacyMetrics(metrics) ? (
          <span
            className="px-2 py-0.5 rounded border border-amber-700 bg-amber-950/50 text-amber-300 font-bold"
            title={version === null
              ? "Computed before mark-to-market equity: drawdown can be understated and Sharpe overstated. Re-run to compare with current runs."
              : `Metrics v${version}: computed before the profile's fees were charged and before trades were counted as round trips. Re-run to compare with current runs.`}
          >
            legacy metrics{version === null ? "" : ` (v${version})`}
          </span>
        ) : (
          <span className="px-2 py-0.5 rounded border border-zinc-700 bg-zinc-900 text-zinc-400">metrics v{version}</span>
        )}
        {executionProfile === "optimistic" && (
          <span className="px-2 py-0.5 rounded border border-rose-800 bg-rose-950/40 text-rose-300 font-bold">
            optimistic profile: no costs – not for decisions
          </span>
        )}
      </div>

      <div className="flex items-center justify-between">
        {/* Institutional Timing Contract Disclosure Banner */}
        <div className="flex-1 flex items-start space-x-2.5 bg-cyan-950/40 border border-cyan-800/60 rounded px-3 py-2 text-2xs text-cyan-300 mr-4">
          <AlertCircle className="w-4 h-4 text-cyan-400 flex-none mt-0.5" />
          <div>
            <span className="font-semibold text-cyan-200">Execution Timing Contract: </span>
            <span>
              Under zero latency, market orders submitted in <code className="bg-cyan-900/60 px-1 py-0.5 rounded text-cyan-100">on_bar</code> execute against the simulated L1 book state at bar close, settling against closing prices rather than next bar open.
            </span>
            <span className="ml-2 text-zinc-400">
              Profile: <strong className="text-zinc-200 uppercase">{executionProfile}</strong>
            </span>
          </div>
        </div>
        
        <button
          onClick={() => {
            const prompt = `Analyze this backtest performance:\nSharpe: ${metrics.sharpe_ratio?.toFixed(2)}\nMax Drawdown: -${(metrics.max_drawdown * 100).toFixed(2)}%\nWin Rate: ${(metrics.win_rate * 100).toFixed(1)}%\n\nWhat's causing the drawdown and how can I improve the strategy?`;
            window.dispatchEvent(new CustomEvent("cryptonite:open-copilot", { 
              detail: { mode: "explain", prompt, autoSend: true } 
            }));
          }}
          className="flex-none flex items-center space-x-2 px-3 py-2 bg-zinc-800 hover:bg-zinc-700 text-cyan-400 border border-white/5 rounded transition-colors text-xs font-bold"
        >
          <span>🤖</span>
          <span>Diagnose with Copilot</span>
        </button>
      </div>

      {/* Grid of Key Performance Indicators */}
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2">
        {/* Total Return */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Total Return</span>
          <div className="flex items-baseline space-x-1.5 mt-1">
            <span className={`text-lg font-black ${isPositive ? "text-emerald-400" : "text-rose-400"}`}>
              {isPositive ? "+" : ""}{(metrics.total_return * 100).toFixed(2)}%
            </span>
            {isPositive ? (
              <TrendingUp className="w-3.5 h-3.5 text-emerald-400" />
            ) : (
              <TrendingDown className="w-3.5 h-3.5 text-rose-400" />
            )}
          </div>
        </div>

        {/* CAGR */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">CAGR (Annualized)</span>
          <div className="mt-1">
            {metrics.cagr !== null && metrics.cagr !== undefined ? (
              <span className={`text-lg font-black ${metrics.cagr >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                {metrics.cagr >= 0 ? "+" : ""}{(metrics.cagr * 100).toFixed(2)}%
              </span>
            ) : (
              <span className="text-sm font-semibold text-zinc-500">N/A (&lt;30d)</span>
            )}
          </div>
        </div>

        {/* Sharpe Ratio */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Sharpe (0% RFR)</span>
          <div className="mt-1">
            {metrics.sharpe_ratio !== null && metrics.sharpe_ratio !== undefined ? (
              <span className={`text-lg font-black ${metrics.sharpe_ratio >= 1.0 ? "text-emerald-400" : metrics.sharpe_ratio > 0 ? "text-cyan-400" : "text-rose-400"}`}>
                {metrics.sharpe_ratio.toFixed(2)}
              </span>
            ) : (
              <span className="text-sm font-semibold text-zinc-500">N/A</span>
            )}
          </div>
        </div>

        {/* Sortino Ratio */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Sortino Ratio</span>
          <div className="mt-1">
            {metrics.sortino_ratio !== null && metrics.sortino_ratio !== undefined ? (
              <span className={`text-lg font-black ${metrics.sortino_ratio >= 1.0 ? "text-emerald-400" : "text-zinc-200"}`}>
                {metrics.sortino_ratio.toFixed(2)}
              </span>
            ) : (
              <span className="text-sm font-semibold text-zinc-500">N/A</span>
            )}
          </div>
        </div>

        {/* Max Drawdown */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Max Drawdown</span>
          <div className="flex items-baseline space-x-1.5 mt-1">
            <span className="text-lg font-black text-rose-400">
              -{(metrics.max_drawdown * 100).toFixed(2)}%
            </span>
          </div>
        </div>

        {/* Profit Factor */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Profit Factor</span>
          <div className="mt-1">
            {metrics.profit_factor !== null && metrics.profit_factor !== undefined ? (
              <span className={`text-lg font-black ${metrics.profit_factor >= 1.5 ? "text-emerald-400" : metrics.profit_factor >= 1.0 ? "text-cyan-400" : "text-rose-400"}`}>
                {metrics.profit_factor.toFixed(2)}
              </span>
            ) : (
              <span className="text-sm font-semibold text-zinc-500">N/A (0 losses)</span>
            )}
          </div>
        </div>

        {/* Win Rate */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Win Rate</span>
          <div className="flex items-baseline space-x-1 mt-1">
            <span className={`text-lg font-black ${metrics.win_rate >= 0.5 ? "text-emerald-400" : "text-amber-400"}`}>
              {(metrics.win_rate * 100).toFixed(1)}%
            </span>
            <span className="text-2xs text-zinc-500">
              ({metrics.winning_trades}/{metrics.total_trades})
            </span>
          </div>
        </div>

        {/* Total Trades */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Trades Count</span>
          <div className="flex items-baseline space-x-2 mt-1">
            <span className="text-lg font-black text-zinc-100">{metrics.total_trades}</span>
            <span className="text-2xs text-emerald-400 font-semibold">{metrics.winning_trades}W</span>
            <span className="text-2xs text-rose-400 font-semibold">{metrics.losing_trades}L</span>
          </div>
        </div>

        {/* Avg Trade PnL */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Avg Trade PnL</span>
          <div className="mt-1">
            <span className={`text-lg font-black ${metrics.avg_trade_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
              {metrics.avg_trade_pnl >= 0 ? "+" : ""}${metrics.avg_trade_pnl.toFixed(2)}
            </span>
          </div>
        </div>

        {/* Max Consecutive Losses */}
        <div className="bg-zinc-900 border border-white/5 rounded p-2.5 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Max Consec Losses</span>
          <div className="mt-1">
            <span className={`text-lg font-black ${metrics.max_consecutive_losses > 3 ? "text-amber-400" : "text-zinc-200"}`}>
              {metrics.max_consecutive_losses}
            </span>
          </div>
        </div>

        {/* Fees paid */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider">Fees Paid</span>
          <span className="mt-1 text-lg font-black text-zinc-200 tabular-nums" data-testid="metric-fees">
            {fees === null ? "—" : `$${fees.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
          </span>
        </div>

        {/* Turnover */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-2xs text-zinc-400 uppercase tracking-wider" title="Traded notional divided by starting equity">Turnover</span>
          <span className="mt-1 text-lg font-black text-zinc-200 tabular-nums" data-testid="metric-turnover">
            {turnover === null ? "—" : `${turnover.toFixed(2)}×`}
          </span>
        </div>
      </div>

      {/* Buy-and-hold benchmark over the same window */}
      <div className="bg-zinc-900/60 border border-white/5 rounded p-3" data-testid="benchmark-block">
        <div className="text-2xs text-zinc-400 uppercase tracking-wider mb-2">Benchmark: buy &amp; hold, same instrument and window</div>
        {bench === null ? (
          <p className="text-2xs text-zinc-500">
            {version === null ? "Not computed for legacy runs: re-run to see the benchmark." : "Not reported for this run."}
          </p>
        ) : (
          <dl className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs tabular-nums">
            <div><dt className="text-zinc-500 text-2xs">Hold return</dt><dd className="text-zinc-200 font-bold">{fmtSignedPct(benchReturn)}</dd></div>
            <div><dt className="text-zinc-500 text-2xs">Hold Sharpe</dt><dd className="text-zinc-200 font-bold">{benchSharpe === null ? "—" : benchSharpe.toFixed(2)}</dd></div>
            <div><dt className="text-zinc-500 text-2xs">Hold max DD</dt><dd className="text-rose-400 font-bold">{benchDd === null ? "—" : `-${(benchDd * 100).toFixed(2)}%`}</dd></div>
            <div>
              <dt className="text-zinc-500 text-2xs">Alpha vs hold</dt>
              <dd className={`font-bold ${alpha === null ? "text-zinc-400" : alpha >= 0 ? "text-emerald-400" : "text-rose-400"}`} data-testid="metric-alpha">{fmtSignedPct(alpha)}</dd>
            </div>
          </dl>
        )}
      </div>
    </div>
  );
};
