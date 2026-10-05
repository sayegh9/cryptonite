"use client";

import React, { useState, useEffect } from "react";
import { RefreshCw, CheckSquare, Square, ArrowRight, AlertTriangle } from "lucide-react";
import { fmtSignedPct, isLegacyMetrics, metricNum, metricsVersion, versionsOf, type VersionedMetrics } from "@/lib/backtestMetrics";

const VersionBadge = ({ metrics }: { metrics?: VersionedMetrics | null }) => {
  const v = metricsVersion(metrics);
  return isLegacyMetrics(metrics) ? (
    <span className="px-1.5 py-0.5 rounded border border-amber-700 bg-amber-950/50 text-amber-300 text-2xs font-bold"
      title={v === null ? "Computed before mark-to-market equity" : `Metrics v${v}: before profile fees and round-trip trades`}>
      legacy metrics{v === null ? "" : ` (v${v})`}
    </span>
  ) : (
    <span className="text-2xs text-zinc-500">v{v}</span>
  );
};

interface RunItem {
  id: string;
  strategy_slug: string;
  strategy_name?: string;
  execution_profile: string;
  timeframe: string;
  status: string;
  parameters: Record<string, any>;
  metrics?: VersionedMetrics & {
    total_return?: number;
    cagr?: number | null;
    sharpe_ratio?: number | null;
    sortino_ratio?: number | null;
    max_drawdown?: number;
    profit_factor?: number | null;
    win_rate?: number;
    total_trades?: number;
    winning_trades?: number;
    losing_trades?: number;
    avg_trade_pnl?: number;
    max_consecutive_losses?: number;
  } | null;
}

interface Props {
  onSelectRun: (runId: string) => void;
}

export const MultiRunComparator: React.FC<Props> = ({ onSelectRun }) => {
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [comparisonData, setComparisonData] = useState<RunItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  const fetchRecentRuns = async () => {
    try {
      const res = await fetch("/api/v1/backtests?limit=20&status=COMPLETED");
      if (res.ok) {
        const data = await res.json();
        setRuns(data);
        // Pre-select top 3 completed runs if none selected
        if (selectedIds.length === 0 && data.length > 0) {
          setSelectedIds(data.slice(0, 3).map((r: any) => r.id));
        }
      }
    } catch (e) {
      console.error("Failed to load backtests for comparison:", e);
    }
  };

  useEffect(() => {
    fetchRecentRuns();
  }, []);

  useEffect(() => {
    if (selectedIds.length === 0) {
      setComparisonData([]);
      return;
    }
    const fetchComparison = async () => {
      setLoading(true);
      try {
        const res = await fetch(`/api/v1/backtests/compare?run_ids=${selectedIds.join(",")}`);
        if (res.ok) {
          const data = await res.json();
          setComparisonData(data.runs || []);
        }
      } catch (e) {
        console.error("Comparison fetch failed:", e);
      } finally {
        setLoading(false);
      }
    };
    fetchComparison();
  }, [selectedIds]);

  const toggleSelect = (id: string) => {
    if (selectedIds.includes(id)) {
      setSelectedIds(selectedIds.filter((x) => x !== id));
    } else {
      setSelectedIds([...selectedIds, id]);
    }
  };

  return (
    <div className="flex flex-col h-full bg-zinc-950 border border-zinc-900 rounded font-mono text-2xs p-3 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-zinc-800 pb-2">
        <div>
          <h3 className="font-bold text-zinc-100 text-xs">SCENARIO & RUN COMPARATOR</h3>
          <p className="text-2xs text-zinc-500">
            Compare performance metrics across Conservative, Adverse Slippage and Optimistic (no costs) profiles.
          </p>
        </div>
        <button
          onClick={fetchRecentRuns}
          className="flex items-center space-x-1 px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300"
        >
          <RefreshCw className="w-3 h-3" />
          <span>Refresh</span>
        </button>
      </div>

      {/* Run History Table */}
      <div className="overflow-x-auto border border-zinc-800 rounded mb-4">
        <table className="w-full text-left border-collapse">
          <thead>
            <tr className="bg-zinc-900 border-b border-zinc-800 text-zinc-400">
              <th className="py-2 px-3 w-10"></th>
              <th className="py-2 px-3">Run ID</th>
              <th className="py-2 px-3">Strategy</th>
              <th className="py-2 px-3">Profile</th>
              <th className="py-2 px-3">Status</th>
              <th className="py-2 px-3">Metrics</th>
              <th className="py-2 px-3 text-right">Total Return</th>
              <th className="py-2 px-3 text-center">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {runs.length === 0 ? (
              <tr>
                <td colSpan={8} className="py-4 text-center text-zinc-500">No runs found.</td>
              </tr>
            ) : (
              runs.map((r) => {
                const isChecked = selectedIds.includes(r.id);
                return (
                  <tr key={r.id} className="hover:bg-zinc-900/40">
                    <td className="py-2 px-3 text-center">
                      <button onClick={() => toggleSelect(r.id)}>
                        {isChecked ? <CheckSquare className="w-4 h-4 text-cyan-400" /> : <Square className="w-4 h-4 text-zinc-600" />}
                      </button>
                    </td>
                    <td className="py-2 px-3 font-mono text-zinc-300">{r.id.slice(0, 8)}</td>
                    <td className="py-2 px-3 font-bold text-zinc-200">{r.strategy_slug}</td>
                    <td className="py-2 px-3 text-cyan-400">{r.execution_profile}</td>
                    <td className="py-2 px-3">
                      <span className={`px-2 py-0.5 rounded text-2xs font-bold ${r.status === 'COMPLETED' ? 'bg-emerald-950 text-emerald-400' : 'bg-zinc-800 text-zinc-400'}`}>
                        {r.status}
                      </span>
                    </td>
                    <td className="py-2 px-3"><VersionBadge metrics={r.metrics} /></td>
                    <td className="py-2 px-3 text-right">
                      {r.metrics?.total_return !== undefined ? (
                        <span className={r.metrics.total_return >= 0 ? "text-emerald-400" : "text-rose-400"}>
                          {(r.metrics.total_return * 100).toFixed(2)}%
                        </span>
                      ) : "-"}
                    </td>
                    <td className="py-2 px-3 text-center">
                      <button
                        onClick={() => onSelectRun(r.id)}
                        className="text-2xs bg-zinc-800 hover:bg-zinc-700 text-zinc-300 px-2 py-1 rounded transition-colors"
                      >
                        Inspect
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Comparison Matrix Table */}
      <div className="flex-1 overflow-auto border border-zinc-900 rounded">
        {loading ? (
          <div className="p-8 text-center text-zinc-500">Loading scenario comparison...</div>
        ) : comparisonData.length === 0 ? (
          <div className="p-8 text-center text-zinc-500">
            Select at least one completed backtest above to inspect metrics.
          </div>
        ) : (
          <>
          {versionsOf(comparisonData).length > 1 && (
            <div role="status" data-testid="mixed-metrics-warning" className="flex items-start gap-2 m-2 px-3 py-2 rounded border border-amber-800/60 bg-amber-950/30 text-amber-300">
              <AlertTriangle className="w-3.5 h-3.5 flex-none mt-0.5" aria-hidden="true" />
              <span>
                Mixed metric versions ({versionsOf(comparisonData).join(", ")}): runs before v2 measure equity only at fills
                (drawdown looks smaller, Sharpe larger) and runs before v3 paid no fees and counted trades per netting
                position. Compare with care, or re-run the legacy runs.
              </span>
            </div>
          )}
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-zinc-800 bg-zinc-900/60 text-zinc-400">
                <th className="py-2 px-3">Metric</th>
                {comparisonData.map((r) => (
                  <th key={r.id} className="py-2 px-3 text-center border-l border-zinc-900">
                    <div className="font-bold text-zinc-200">{r.strategy_slug}</div>
                    <div className="text-2xs uppercase text-cyan-400 font-semibold">{r.execution_profile}</div>
                    <button
                      onClick={() => onSelectRun(r.id)}
                      className="mt-1 inline-flex items-center space-x-1 text-2xs text-zinc-400 hover:text-cyan-300"
                    >
                      <span>Inspect</span>
                      <ArrowRight className="w-2.5 h-2.5" />
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {/* Total Return */}
              <tr className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 font-semibold text-zinc-300">Total Return</td>
                {comparisonData.map((r) => {
                  const val = r.metrics?.total_return;
                  const isPos = val !== undefined && val >= 0;
                  return (
                    <td key={r.id} className="py-1.5 px-3 text-center font-bold border-l border-zinc-900">
                      {val !== undefined ? (
                        <span className={isPos ? "text-emerald-400" : "text-rose-400"}>
                          {isPos ? "+" : ""}{(val * 100).toFixed(2)}%
                        </span>
                      ) : (
                        "-"
                      )}
                    </td>
                  );
                })}
              </tr>

              {/* CAGR */}
              <tr className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">CAGR (Annualized)</td>
                {comparisonData.map((r) => {
                  const val = r.metrics?.cagr;
                  return (
                    <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900">
                      {val !== null && val !== undefined ? `${(val * 100).toFixed(2)}%` : "N/A (<30d)"}
                    </td>
                  );
                })}
              </tr>

              {/* Sharpe Ratio */}
              <tr className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Sharpe Ratio (0% RFR)</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900 font-semibold">
                    {r.metrics?.sharpe_ratio !== null && r.metrics?.sharpe_ratio !== undefined
                      ? r.metrics.sharpe_ratio.toFixed(2)
                      : "-"}
                  </td>
                ))}
              </tr>

              {/* Sortino Ratio */}
              <tr className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Sortino Ratio</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900">
                    {r.metrics?.sortino_ratio !== null && r.metrics?.sortino_ratio !== undefined
                      ? r.metrics.sortino_ratio.toFixed(2)
                      : "-"}
                  </td>
                ))}
              </tr>

              {/* Max Drawdown */}
              <tr className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Max Drawdown</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center text-rose-400 font-bold border-l border-zinc-900">
                    {r.metrics?.max_drawdown !== undefined
                      ? `-${(r.metrics.max_drawdown * 100).toFixed(2)}%`
                      : "-"}
                  </td>
                ))}
              </tr>

              {/* Profit Factor */}
              <tr className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Profit Factor</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900">
                    {r.metrics?.profit_factor !== null && r.metrics?.profit_factor !== undefined
                      ? r.metrics.profit_factor.toFixed(2)
                      : "N/A (0 losses)"}
                  </td>
                ))}
              </tr>

              {/* Win Rate */}
              <tr className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Win Rate</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900">
                    {r.metrics?.win_rate !== undefined
                      ? `${(r.metrics.win_rate * 100).toFixed(1)}% (${r.metrics.winning_trades}W / ${r.metrics.total_trades}T)`
                      : "-"}
                  </td>
                ))}
              </tr>

              {/* Total Trades */}
              <tr className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Total Trades</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900 font-semibold text-zinc-200">
                    {r.metrics?.total_trades ?? "-"}
                  </td>
                ))}
              </tr>

              {/* Avg Trade PnL */}
              <tr className="border-b border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Avg Trade PnL</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900">
                    {r.metrics?.avg_trade_pnl !== undefined
                      ? `$${r.metrics.avg_trade_pnl.toFixed(2)}`
                      : "-"}
                  </td>
                ))}
              </tr>

              {/* Max Consecutive Losses */}
              <tr className="hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Max Consec Losses</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900">
                    {r.metrics?.max_consecutive_losses ?? "-"}
                  </td>
                ))}
              </tr>

              {/* Fees and turnover (metrics v2) */}
              <tr className="border-t border-zinc-900/60 hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Fees Paid</td>
                {comparisonData.map((r) => {
                  const v = metricNum(r.metrics?.fees_paid);
                  return <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900">{v === null ? "—" : `$${v.toFixed(2)}`}</td>;
                })}
              </tr>
              <tr className="hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Turnover</td>
                {comparisonData.map((r) => {
                  const v = metricNum(r.metrics?.turnover);
                  return <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900">{v === null ? "—" : `${v.toFixed(2)}×`}</td>;
                })}
              </tr>

              {/* Benchmark (buy & hold over the same window) */}
              <tr className="hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Buy &amp; Hold Return</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900">
                    {fmtSignedPct(metricNum(r.metrics?.benchmark?.total_return))}
                  </td>
                ))}
              </tr>
              <tr className="hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Alpha vs Hold</td>
                {comparisonData.map((r) => {
                  const v = metricNum(r.metrics?.benchmark?.alpha_vs_hold);
                  return (
                    <td key={r.id} className={`py-1.5 px-3 text-center border-l border-zinc-900 font-semibold ${v === null ? "" : v >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      {fmtSignedPct(v)}
                    </td>
                  );
                })}
              </tr>
              <tr className="hover:bg-zinc-900/20">
                <td className="py-1.5 px-3 text-zinc-400">Metrics Version</td>
                {comparisonData.map((r) => (
                  <td key={r.id} className="py-1.5 px-3 text-center border-l border-zinc-900"><VersionBadge metrics={r.metrics} /></td>
                ))}
              </tr>
            </tbody>
          </table>
          </>
        )}
      </div>
    </div>
  );
};
