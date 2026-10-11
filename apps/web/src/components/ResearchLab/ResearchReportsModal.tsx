"use client";

import { useEffect, useState } from "react";
import { XCircle, RefreshCw } from "lucide-react";

interface ResearchReport {
  id: string;
  idea: string;
  status: string;
  reason: string;
  strategy_slug: string | null;
  strategy_name: string | null;
  in_sample: Record<string, any>;
  out_of_sample: Record<string, any>;
  cost_usd: number;
  created_at: string;
  dsr: number | null;
  holdout: Record<string, any> | null;
  robustness: Record<string, any> | null;
}

const WINDOWS: { hours: number; label: string }[] = [
  { hours: 24, label: "Last 24 hours" },
  { hours: 168, label: "Last 7 days" },
  { hours: 720, label: "Last 30 days" },
];

const pct = (v: unknown) => (typeof v === "number" ? `${(v * 100).toFixed(2)}%` : "n/a");
const num = (v: unknown) => (typeof v === "number" ? v.toFixed(2) : "n/a");

const STATUS_STYLE: Record<string, string> = {
  PROMOTE: "bg-emerald-950 text-emerald-300 border-emerald-700",
  REJECT: "bg-rose-950 text-rose-300 border-rose-800",
  FAILED: "bg-zinc-900 text-zinc-300 border-zinc-700",
};

export function ResearchReportsModal({ onClose }: { onClose: () => void }) {
  const [hours, setHours] = useState(168);
  const [reports, setReports] = useState<ResearchReport[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/v1/research/reports?hours=${hours}`, { credentials: "include" });
      if (!res.ok) {
        setError(`Could not load reports (HTTP ${res.status})`);
        return;
      }
      const data = await res.json();
      setReports(data.reports ?? []);
    } catch (e: any) {
      setError("Could not reach the API: " + (e?.message ?? "unknown error"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [hours]);

  return (
    <div className="fixed inset-0 bg-black/75 backdrop-blur-sm z-50 flex items-center justify-center p-4 font-mono" onClick={onClose}>
      <div
        className="w-full max-w-4xl bg-zinc-950 border border-zinc-800 rounded-xl shadow-2xl overflow-hidden flex flex-col h-[85vh]"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Research reports"
      >
        <div className="p-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/60">
          <div>
            <h2 className="font-bold text-white text-sm">RESEARCH REPORTS</h2>
            <p className="text-2xs text-zinc-400">Each queued idea is drafted, optimized and reviewed. Nothing here is traded.</p>
          </div>
          <div className="flex items-center space-x-2">
            <select
              value={hours}
              aria-label="Report window"
              onChange={(e) => setHours(Number(e.target.value))}
              className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-xs text-zinc-200"
            >
              {WINDOWS.map((w) => (
                <option key={w.hours} value={w.hours}>
                  {w.label}
                </option>
              ))}
            </select>
            <button onClick={load} className="p-1.5 bg-zinc-900 hover:bg-zinc-800 text-zinc-400 rounded border border-zinc-800" title="Refresh">
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
            </button>
            <button onClick={onClose} className="text-zinc-500 hover:text-white p-1 rounded hover:bg-zinc-900" aria-label="Close reports">
              <XCircle className="w-5 h-5" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-2">
          {error && <div role="alert" className="text-rose-300 text-xs">{error}</div>}
          {!error && !loading && reports.length === 0 && (
            <div className="text-xs text-zinc-500">No research reports in this window. The research worker writes one per queued idea.</div>
          )}
          {reports.map((r) => {
            const open = openId === r.id;
            return (
              <div key={r.id} className="border border-zinc-800 rounded-lg bg-zinc-900/40">
                <button
                  onClick={() => setOpenId(open ? null : r.id)}
                  className="w-full text-left p-3 flex items-start justify-between gap-3"
                  aria-expanded={open}
                >
                  <div className="min-w-0">
                    <div className="text-xs font-bold text-zinc-100 truncate">{r.idea}</div>
                    <div className="text-2xs text-zinc-500 mt-0.5">
                      {new Date(r.created_at).toLocaleString()} {r.strategy_name ? `· ${r.strategy_name}` : ""}
                    </div>
                  </div>
                  <span className={`px-2 py-0.5 rounded border text-2xs font-bold flex-none ${STATUS_STYLE[r.status] ?? STATUS_STYLE.FAILED}`}>
                    {r.status}
                  </span>
                </button>
                {open && (
                  <div className="px-3 pb-3 space-y-2 text-2xs text-zinc-300">
                    <div className="text-zinc-400">{r.reason || "No reason recorded."}</div>
                    <div className="grid grid-cols-2 gap-2">
                      <div>In-sample return: {pct(r.in_sample?.total_return)} · Sharpe {num(r.in_sample?.sharpe_ratio)}</div>
                      <div>Out-of-sample return: {pct(r.out_of_sample?.total_return)} · Sharpe {num(r.out_of_sample?.sharpe_ratio)}</div>
                      <div>Deflated Sharpe: {num(r.dsr)}</div>
                      <div>Worst parameter retention: {pct(r.robustness?.worst_retention)}</div>
                      <div>Holdout return: {r.holdout ? pct(r.holdout.total_return) : "not scored"}</div>
                      <div>Research cost: ${num(r.cost_usd)}</div>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
