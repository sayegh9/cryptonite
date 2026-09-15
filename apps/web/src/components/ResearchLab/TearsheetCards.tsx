"use client";

import React from "react";
import { BacktestMetrics } from "@/lib/types";
import { AlertCircle, TrendingUp, TrendingDown, Percent, Award, ShieldAlert } from "lucide-react";

interface Props {
  metrics: BacktestMetrics;
  executionProfile: string;
}

export const TearsheetCards: React.FC<Props> = ({ metrics, executionProfile }) => {
  const isPositive = metrics.total_return >= 0;

  return (
    <div className="flex flex-col space-y-3 w-full font-mono">
      {/* Institutional Timing Contract Disclosure Banner */}
      <div className="flex items-start space-x-2.5 bg-cyan-950/40 border border-cyan-800/60 rounded px-3 py-2 text-[11px] text-cyan-300">
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

      {/* Grid of Key Performance Indicators */}
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2">
        {/* Total Return */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">Total Return</span>
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
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">CAGR (Annualized)</span>
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
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">Sharpe (0% RFR)</span>
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
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">Sortino Ratio</span>
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
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">Max Drawdown</span>
          <div className="flex items-baseline space-x-1.5 mt-1">
            <span className="text-lg font-black text-rose-400">
              -{(metrics.max_drawdown * 100).toFixed(2)}%
            </span>
          </div>
        </div>

        {/* Profit Factor */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">Profit Factor</span>
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
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">Win Rate</span>
          <div className="flex items-baseline space-x-1 mt-1">
            <span className={`text-lg font-black ${metrics.win_rate >= 0.5 ? "text-emerald-400" : "text-amber-400"}`}>
              {(metrics.win_rate * 100).toFixed(1)}%
            </span>
            <span className="text-[10px] text-zinc-500">
              ({metrics.winning_trades}/{metrics.total_trades})
            </span>
          </div>
        </div>

        {/* Total Trades */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">Trades Count</span>
          <div className="flex items-baseline space-x-2 mt-1">
            <span className="text-lg font-black text-zinc-100">{metrics.total_trades}</span>
            <span className="text-[10px] text-emerald-400 font-semibold">{metrics.winning_trades}W</span>
            <span className="text-[10px] text-rose-400 font-semibold">{metrics.losing_trades}L</span>
          </div>
        </div>

        {/* Avg Trade PnL */}
        <div className="bg-zinc-900 border border-white/5 rounded p-3 flex flex-col justify-between">
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">Avg Trade PnL</span>
          <div className="mt-1">
            <span className={`text-lg font-black ${metrics.avg_trade_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
              {metrics.avg_trade_pnl >= 0 ? "+" : ""}${metrics.avg_trade_pnl.toFixed(2)}
            </span>
          </div>
        </div>

        {/* Max Consecutive Losses */}
        <div className="bg-zinc-900 border border-white/5 rounded p-2.5 flex flex-col justify-between">
          <span className="text-[10px] text-zinc-400 uppercase tracking-wider">Max Consec Losses</span>
          <div className="mt-1">
            <span className={`text-lg font-black ${metrics.max_consecutive_losses > 3 ? "text-amber-400" : "text-zinc-200"}`}>
              {metrics.max_consecutive_losses}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
};
