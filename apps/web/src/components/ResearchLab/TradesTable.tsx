"use client";

import React, { useState, useEffect } from "react";
import { BacktestTrade } from "@/lib/types";
import { ChevronLeft, ChevronRight, Eye, RefreshCw } from "lucide-react";

interface Props {
  runId: string;
  onSelectTrade: (trade: BacktestTrade) => void;
  selectedTrade?: BacktestTrade | null;
}

export const TradesTable: React.FC<Props> = ({
  runId,
  onSelectTrade,
  selectedTrade,
}) => {
  const [trades, setTrades] = useState<BacktestTrade[]>([]);
  const [page, setPage] = useState<number>(1);
  const [pageSize] = useState<number>(20);
  const [total, setTotal] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const fetchTrades = async (p: number, ps: number) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/v1/backtests/${runId}/trades?page=${p}&page_size=${ps}`);
      if (!res.ok) {
        throw new Error(`Failed to load trades (HTTP ${res.status})`);
      }
      const data = await res.json();
      setTrades(data.trades || []);
      setTotal(data.total || 0);
      setPage(data.page || p);
    } catch (err: any) {
      setError(err.message || "Failed to load trades");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (runId) {
      fetchTrades(1, pageSize);
    }
  }, [runId, pageSize]);

  const totalPages = Math.ceil(total / pageSize) || 1;

  const handlePrev = () => {
    if (page > 1) {
      fetchTrades(page - 1, pageSize);
    }
  };

  const handleNext = () => {
    if (page < totalPages) {
      fetchTrades(page + 1, pageSize);
    }
  };

  const formatTs = (tsNs: number) => {
    if (!tsNs) return "-";
    const d = new Date(tsNs / 1e6);
    return d.toISOString().replace("T", " ").replace("Z", "").slice(0, 19);
  };

  return (
    <div className="flex flex-col h-full bg-zinc-950 border border-zinc-900 rounded font-mono text-2xs">
      {/* Table Header Controls */}
      <div className="flex items-center justify-between px-3 py-2 bg-zinc-900/50 border-b border-zinc-800">
        <div className="flex items-center space-x-2">
          <span className="font-semibold text-zinc-200">EXECUTED TRADES</span>
          <span className="text-zinc-500">({total} total)</span>
          {loading && <RefreshCw className="w-3 h-3 text-cyan-400 animate-spin" />}
        </div>

        {/* Pagination bar */}
        <div className="flex items-center space-x-2">
          <span className="text-zinc-400">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={handlePrev}
            disabled={page <= 1 || loading}
            className="p-1 rounded bg-zinc-800 hover:bg-zinc-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <ChevronLeft className="w-3.5 h-3.5 text-zinc-300" />
          </button>
          <button
            onClick={handleNext}
            disabled={page >= totalPages || loading}
            className="p-1 rounded bg-zinc-800 hover:bg-zinc-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <ChevronRight className="w-3.5 h-3.5 text-zinc-300" />
          </button>
        </div>
      </div>

      {/* Table Body */}
      <div className="flex-1 overflow-auto">
        {error ? (
          <div className="p-4 text-rose-400 text-center">{error}</div>
        ) : trades.length === 0 && !loading ? (
          <div className="p-8 text-zinc-500 text-center">No trades recorded during this evaluation period.</div>
        ) : (
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-zinc-800/80 bg-zinc-900/30 text-zinc-400 sticky top-0 bg-zinc-950 z-10">
                <th className="py-1.5 px-3">Position ID</th>
                <th className="py-1.5 px-2">Side</th>
                <th className="py-1.5 px-2">Entry Time</th>
                <th className="py-1.5 px-2">Exit Time</th>
                <th className="py-1.5 px-2 text-right">Entry Px</th>
                <th className="py-1.5 px-2 text-right">Exit Px</th>
                <th className="py-1.5 px-2 text-right">Size</th>
                <th className="py-1.5 px-2 text-right">PnL (USDT)</th>
                <th className="py-1.5 px-2 text-right">Return</th>
                <th className="py-1.5 px-3 text-center">Action</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => {
                const isSelected = selectedTrade?.position_id === t.position_id;
                const isProfitable = t.realized_pnl >= 0;
                const isLong = t.side.toUpperCase().includes("LONG") || t.side.toUpperCase().includes("BUY");

                return (
                  <tr
                    key={t.position_id}
                    className={`border-b border-zinc-900 hover:bg-zinc-900/40 transition-colors ${
                      isSelected ? "bg-cyan-950/40 border-l-2 border-l-cyan-400" : ""
                    }`}
                  >
                    <td className="py-1.5 px-3 font-semibold text-zinc-300" title={t.position_id}>
                      {t.position_id.length > 12 ? `${t.position_id.slice(0, 8)}...` : t.position_id}
                    </td>
                    <td className="py-1.5 px-2">
                      <span
                        className={`px-1.5 py-0.5 rounded text-2xs font-bold ${
                          isLong
                            ? "bg-emerald-950/80 text-emerald-400 border border-emerald-800/40"
                            : "bg-rose-950/80 text-rose-400 border border-rose-800/40"
                        }`}
                      >
                        {t.side}
                      </span>
                    </td>
                    <td className="py-1.5 px-2 text-zinc-400">{formatTs(t.ts_opened_ns)}</td>
                    <td className="py-1.5 px-2 text-zinc-400">{formatTs(t.ts_closed_ns)}</td>
                    <td className="py-1.5 px-2 text-right text-zinc-200">${t.entry_price.toFixed(2)}</td>
                    <td className="py-1.5 px-2 text-right text-zinc-200">
                      {t.exit_price > 0 ? `$${t.exit_price.toFixed(2)}` : "-"}
                    </td>
                    <td className="py-1.5 px-2 text-right text-zinc-400">{t.quantity.toFixed(4)}</td>
                    <td
                      className={`py-1.5 px-2 text-right font-bold ${
                        isProfitable ? "text-emerald-400" : "text-rose-400"
                      }`}
                    >
                      {isProfitable ? "+" : ""}${t.realized_pnl.toFixed(2)}
                    </td>
                    <td
                      className={`py-1.5 px-2 text-right font-semibold ${
                        isProfitable ? "text-emerald-400" : "text-rose-400"
                      }`}
                    >
                      {isProfitable ? "+" : ""}{(t.realized_return * 100).toFixed(2)}%
                    </td>
                    <td className="py-1.5 px-3 text-center">
                      <button
                        onClick={() => onSelectTrade(t)}
                        className="inline-flex items-center space-x-1 px-2 py-0.5 rounded bg-zinc-800 hover:bg-cyan-900/60 hover:text-cyan-300 text-zinc-400 transition-colors"
                        title="View trade marker on equity chart"
                      >
                        <Eye className="w-3 h-3" />
                        <span>Chart</span>
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};
