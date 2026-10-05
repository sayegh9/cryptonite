"use client";

import React, { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { fetchApi } from "@/lib/api";
import { metricNum } from "@/lib/backtestMetrics";

/**
 * Live vs backtest (F3, reporting only): each running paper strategy instance since deploy against the
 * latest completed backtest of the same strategy version (GET /api/v1/research/live-vs-backtest).
 * Unknown values render as "—".
 */
interface Side {
  trades?: number | string | null;
  win_rate?: number | string | null;
  mean_return_per_trade?: number | string | null;
  fees?: number | string | null;
  slippage_bps?: number | string | null;
}

interface InstanceRow {
  instance_id: string;
  strategy_name?: string | null;
  instrument_id?: string | null;
  deployed_at?: string | null;
  live?: Side | null;
  backtest?: (Side & { run_id?: string | null; execution_profile?: string | null }) | null;
}

interface Report {
  as_of?: string | null;
  available?: boolean;
  instances?: InstanceRow[] | null;
  without_backtest?: { instance_id: string; strategy_name?: string | null }[] | null;
}

const pct = (v: unknown, digits = 1) => {
  const n = metricNum(v);
  return n === null ? "—" : `${(n * 100).toFixed(digits)}%`;
};
const num = (v: unknown, digits = 2) => {
  const n = metricNum(v);
  return n === null ? "—" : n.toFixed(digits);
};
const int = (v: unknown) => {
  const n = metricNum(v);
  return n === null ? "—" : String(Math.round(n));
};

const ROWS: { label: string; render: (s: Side | null | undefined) => string }[] = [
  { label: "Trades", render: (s) => int(s?.trades) },
  { label: "Win rate", render: (s) => pct(s?.win_rate) },
  { label: "Mean return / trade", render: (s) => pct(s?.mean_return_per_trade, 2) },
  { label: "Fees (USDT)", render: (s) => num(s?.fees) },
  { label: "Slippage vs limit (bps)", render: (s) => num(s?.slippage_bps) },
];

export const LiveVsBacktestPanel: React.FC = () => {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setReport(await fetchApi("/research/live-vs-backtest"));
      setError(null);
    } catch (e: any) {
      setError(e?.message || "Live-vs-backtest report unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const instances = report?.instances ?? [];
  const without = report?.without_backtest ?? [];

  return (
    <section aria-label="Live vs backtest" className="h-full overflow-y-auto font-mono text-xs space-y-3" data-testid="live-vs-backtest">
      <div className="flex items-center justify-between">
        <h2 className="text-2xs font-bold uppercase tracking-wider text-zinc-400">
          Live vs backtest · running paper strategy instances (reporting only)
        </h2>
        <button
          type="button"
          onClick={() => void load()}
          className="flex items-center space-x-1 px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-2xs"
        >
          <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} />
          <span>Refresh</span>
        </button>
      </div>

      {error ? (
        <p role="alert" className="text-2xs text-rose-300">{error}</p>
      ) : report === null ? (
        <p className="text-2xs text-zinc-500">Loading…</p>
      ) : report.available === false ? (
        <p className="text-2xs text-zinc-500">Report not available.</p>
      ) : instances.length === 0 ? (
        <p className="text-2xs text-zinc-500" data-testid="live-vs-backtest-empty">
          {without.length === 0
            ? "No running strategy instances."
            : "No running strategy instance has a completed backtest of its strategy version yet."}
        </p>
      ) : (
        instances.map((r) => (
          <div key={r.instance_id} className="rounded border border-white/5 bg-zinc-900/60 p-3" data-testid={`lvb-${r.instance_id}`}>
            <div className="mb-2 text-2xs text-zinc-400">
              <strong className="text-zinc-200">{r.instance_id}</strong> · {r.strategy_name ?? "—"} · {r.instrument_id ?? "—"} · deployed{" "}
              {r.deployed_at ? r.deployed_at.slice(0, 10) : "—"} · backtest {r.backtest?.run_id ? r.backtest.run_id.slice(0, 8) : "—"}
              {r.backtest?.execution_profile ? ` (${r.backtest.execution_profile})` : ""}
            </div>
            <table className="w-full text-left tabular-nums" aria-label={`Live vs backtest for ${r.instance_id}`}>
              <thead className="text-2xs text-zinc-500">
                <tr>
                  <th className="py-1 font-normal">Metric</th>
                  <th className="py-1 font-normal">Live since deploy</th>
                  <th className="py-1 font-normal">Backtest</th>
                </tr>
              </thead>
              <tbody className="text-zinc-200">
                {ROWS.map((row) => (
                  <tr key={row.label} className="border-t border-zinc-800">
                    <td className="py-1 text-zinc-400">{row.label}</td>
                    <td className="py-1">{row.render(r.live)}</td>
                    <td className="py-1">{row.render(r.backtest)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}
      {instances.length > 0 && without.length > 0 && (
        <p className="text-2xs text-zinc-500">
          Without a completed backtest of their version: {without.map((w) => w.instance_id).join(", ")}
        </p>
      )}
    </section>
  );
};
