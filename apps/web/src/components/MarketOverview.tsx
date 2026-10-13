"use client";

import React, { useEffect, useState } from "react";
import { TrendingUp, TrendingDown, RefreshCw } from "lucide-react";
import { MarketOverviewTicker } from "@/lib/types";
import { fetchApi } from "@/lib/api";
import { useSlowFlag } from "@/lib/hooks/useSlowFlag";

/** The overview is a heavy request; past this it is reported as not loaded */
const MARKETS_TIMEOUT_MS = 15_000;

interface Props {
  onSelectSymbol: (symbol: string) => void;
}

export const MarketOverview: React.FC<Props> = ({ onSelectSymbol }) => {
  const [tickers, setTickers] = useState<MarketOverviewTicker[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const slow = useSlowFlag(loading && tickers.length === 0);

  const loadData = async () => {
    try {
      const data = await fetchApi("/market/overview", { timeoutMs: MARKETS_TIMEOUT_MS });
      setTickers(data);
      setError(null);
    } catch (err: any) {
      setError(err?.timeout ? "Market data did not load in time." : err?.message || "Failed to load market data");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 30000);
    return () => clearInterval(interval);
  }, []);

  if (loading && tickers.length === 0) {
    return (
      <div role="status" className="h-full flex flex-col bg-zinc-950 p-4 space-y-6">
        <p className="font-mono text-2xs text-zinc-500">
          {slow ? "Still loading market data… the feed is slower than usual." : "Loading market data…"}
        </p>
        <div className="space-y-6 animate-pulse" aria-hidden="true">
          <div className="h-48 bg-zinc-900 rounded"></div>
          <div className="h-32 bg-zinc-900 rounded"></div>
          <div className="flex-1 bg-zinc-900 rounded"></div>
        </div>
      </div>
    );
  }

  if (error && tickers.length === 0) {
    return (
      <div className="h-full flex flex-col items-center justify-center bg-zinc-950 text-zinc-100 font-mono space-y-4">
        <p className="text-red-400">{error}</p>
        <button
          onClick={() => { setLoading(true); loadData(); }}
          className="flex items-center space-x-2 px-4 py-2 bg-zinc-900 hover:bg-zinc-800 rounded transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
          <span>Retry</span>
        </button>
      </div>
    );
  }

  const sortedByChange = [...tickers].sort((a, b) => b.change_pct - a.change_pct);
  const topGainers = sortedByChange.slice(0, 5);
  const topLosers = sortedByChange.slice(-5).reverse();
  const topVolume = [...tickers].sort((a, b) => b.volume_quote - a.volume_quote).slice(0, 10);

  const getHeatmapColor = (pct: number) => {
    if (pct > 3) return "bg-emerald-700";
    if (pct > 1) return "bg-emerald-900/80";
    if (pct > 0) return "bg-emerald-950/60";
    if (pct > -1) return "bg-red-950/60";
    if (pct > -3) return "bg-red-900/80";
    return "bg-red-700";
  };

  const formatVolume = (vol: number) => {
    if (vol >= 1e9) return `$${(vol / 1e9).toFixed(1)}B`;
    if (vol >= 1e6) return `$${(vol / 1e6).toFixed(1)}M`;
    return `$${vol.toLocaleString()}`;
  };

  return (
    <div className="h-full flex flex-col bg-zinc-950 text-zinc-100 p-4 space-y-6 overflow-y-auto">
      
      {/* Section 1: Top Movers */}
      <div className="space-y-3 shrink-0">
        <h2 className="font-mono text-2xs uppercase tracking-widest text-zinc-500">Top Movers</h2>
        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <h3 className="text-xs font-bold text-emerald-500 mb-2">Gainers</h3>
            {topGainers.map((t) => (
              <div
                key={t.symbol}
                onClick={() => onSelectSymbol(t.symbol)}
                className="flex items-center justify-between p-2 bg-zinc-900/50 rounded border border-white/5 cursor-pointer hover:bg-zinc-800 transition-colors"
              >
                <span className="font-mono text-xs font-bold">{t.symbol.replace("USDT", "")}</span>
                <span className="font-mono text-xs text-emerald-400 flex items-center space-x-1">
                  <TrendingUp className="w-3 h-3" />
                  <span>+{t.change_pct.toFixed(2)}%</span>
                </span>
              </div>
            ))}
          </div>
          <div className="space-y-2">
            <h3 className="text-xs font-bold text-red-500 mb-2">Losers</h3>
            {topLosers.map((t) => (
              <div
                key={t.symbol}
                onClick={() => onSelectSymbol(t.symbol)}
                className="flex items-center justify-between p-2 bg-zinc-900/50 rounded border border-white/5 cursor-pointer hover:bg-zinc-800 transition-colors"
              >
                <span className="font-mono text-xs font-bold">{t.symbol.replace("USDT", "")}</span>
                <span className="font-mono text-xs text-red-400 flex items-center space-x-1">
                  <TrendingDown className="w-3 h-3" />
                  <span>{t.change_pct.toFixed(2)}%</span>
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Section 2: Volume Leaders */}
      <div className="space-y-3 shrink-0">
        <h2 className="font-mono text-2xs uppercase tracking-widest text-zinc-500">Volume Leaders</h2>
        <div className="flex gap-3 overflow-x-auto pb-2 scrollbar-thin scrollbar-thumb-zinc-800">
          {topVolume.map((t) => (
            <div
              key={t.symbol}
              onClick={() => onSelectSymbol(t.symbol)}
              className="flex-shrink-0 w-32 p-3 bg-zinc-900/50 rounded border border-white/5 cursor-pointer hover:bg-zinc-800 transition-colors flex flex-col space-y-1"
            >
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs font-bold">{t.symbol.replace("USDT", "")}</span>
                <span className={`text-2xs font-mono ${t.change_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {t.change_pct > 0 ? "+" : ""}{t.change_pct.toFixed(1)}%
                </span>
              </div>
              <span className="font-mono text-sm">{formatVolume(t.volume_quote)}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Section 3: Market Heatmap */}
      <div className="flex-1 flex flex-col space-y-3 min-h-[300px]">
        <h2 className="font-mono text-2xs uppercase tracking-widest text-zinc-500">Market Heatmap — USDT Pairs</h2>
        <div className="flex-1 grid grid-cols-[repeat(auto-fill,minmax(96px,1fr))] gap-1">
          {tickers.map((t) => (
            <div
              key={t.symbol}
              onClick={() => onSelectSymbol(t.symbol)}
              className={`${getHeatmapColor(t.change_pct)} p-2 flex flex-col justify-center items-center cursor-pointer hover:brightness-110 transition-all rounded-sm`}
            >
              <span className="font-mono text-xs font-bold truncate w-full text-center">{t.symbol.replace("USDT", "")}</span>
              <span className="font-mono text-2xs opacity-90">
                {t.change_pct > 0 ? "+" : ""}{t.change_pct.toFixed(2)}%
              </span>
            </div>
          ))}
        </div>
      </div>

    </div>
  );
};
