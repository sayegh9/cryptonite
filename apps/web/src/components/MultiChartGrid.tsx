"use client";

import React, { useState } from "react";
import { TradingViewChart } from "./TradingViewChart";
import { LayoutGrid, Square, Columns } from "lucide-react";

export type GridLayout = "1x1" | "1x2" | "2x2";

export interface ChartConfig {
  id: string;
  symbol: string;
  timeframe: string;
}

interface Props {
  activeTarget?: {
    symbol: string;
    timeframe: string;
    candleTime: number;
  } | null;
  onActiveChartSelect?: (symbol: string) => void;
}

export const MultiChartGrid: React.FC<Props> = ({ activeTarget, onActiveChartSelect }) => {
  const [layout, setLayout] = useState<GridLayout>("1x2");
  const [focusedId, setFocusedId] = useState<string>("chart-0");

  const [charts, setCharts] = useState<ChartConfig[]>([
    { id: "chart-0", symbol: "BTCUSDT", timeframe: "1m" },
    { id: "chart-1", symbol: "BTCUSDT", timeframe: "5m" },
    { id: "chart-2", symbol: "ETHUSDT", timeframe: "1m" },
    { id: "chart-3", symbol: "ETHUSDT", timeframe: "1h" },
  ]);

  // When activeTarget changes from Scanner Studio "View on Chart", update focused chart
  React.useEffect(() => {
    if (!activeTarget) return;
    setCharts((prev) =>
      prev.map((c) =>
        c.id === focusedId
          ? { ...c, symbol: activeTarget.symbol, timeframe: activeTarget.timeframe }
          : c
      )
    );
  }, [activeTarget, focusedId]);

  const handleTimeframeChange = (chartId: string, newTf: string) => {
    setCharts((prev) =>
      prev.map((c) => (c.id === chartId ? { ...c, timeframe: newTf } : c))
    );
  };

  const handleSymbolChange = (chartId: string, newSym: string) => {
    setCharts((prev) =>
      prev.map((c) => (c.id === chartId ? { ...c, symbol: newSym } : c))
    );
    onActiveChartSelect?.(newSym);
  };

  const getVisibleCharts = () => {
    switch (layout) {
      case "1x1":
        return charts.slice(0, 1);
      case "1x2":
        return charts.slice(0, 2);
      case "2x2":
        return charts.slice(0, 4);
    }
  };

  const visibleCharts = getVisibleCharts();

  return (
    <div className="flex flex-col h-full w-full bg-zinc-950 p-2">
      {/* Grid Toolbar */}
      <div className="flex items-center justify-between pb-2 mb-2 border-b border-white/5">
        <div className="flex items-center space-x-3">
          <span className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
            Multi-Chart Terminal
          </span>
          <span className="text-2xs text-zinc-500 font-mono">
            Active: {charts.find((c) => c.id === focusedId)?.symbol} ({charts.find((c) => c.id === focusedId)?.timeframe})
          </span>
        </div>

        {/* Layout Switcher Buttons */}
        <div className="flex items-center space-x-1 bg-zinc-900 border border-zinc-800 p-0.5 rounded">
          <button
            onClick={() => setLayout("1x1")}
            title="Single Chart (1x1)"
            className={`p-1 rounded text-xs transition-colors flex items-center space-x-1 ${
              layout === "1x1" ? "bg-cyan-500 text-zinc-950 font-bold" : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            <Square className="w-3.5 h-3.5" />
            <span className="text-2xs">1x1</span>
          </button>
          <button
            onClick={() => setLayout("1x2")}
            title="Split Charts (1x2)"
            className={`p-1 rounded text-xs transition-colors flex items-center space-x-1 ${
              layout === "1x2" ? "bg-cyan-500 text-zinc-950 font-bold" : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            <Columns className="w-3.5 h-3.5" />
            <span className="text-2xs">1x2</span>
          </button>
          <button
            onClick={() => setLayout("2x2")}
            title="Quad Grid (2x2)"
            className={`p-1 rounded text-xs transition-colors flex items-center space-x-1 ${
              layout === "2x2" ? "bg-cyan-500 text-zinc-950 font-bold" : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            <LayoutGrid className="w-3.5 h-3.5" />
            <span className="text-2xs">2x2</span>
          </button>
        </div>
      </div>

      {/* Grid Panes Area */}
      <div
        className={`flex-1 grid gap-2 min-h-0 ${
          layout === "1x1"
            ? "grid-cols-1 grid-rows-1"
            : layout === "1x2"
            ? "grid-cols-1 md:grid-cols-2 grid-rows-1"
            : "grid-cols-1 md:grid-cols-2 grid-rows-2"
        }`}
      >
        {visibleCharts.map((c) => (
          <div key={c.id} className="min-h-[260px] h-full w-full">
            <TradingViewChart
              chartId={c.id}
              symbol={c.symbol}
              timeframe={c.timeframe}
              isFocused={c.id === focusedId}
              onFocus={() => {
                setFocusedId(c.id);
                onActiveChartSelect?.(c.symbol);
              }}
              onSymbolChange={(sym) => handleSymbolChange(c.id, sym)}
              onTimeframeChange={(tf) => handleTimeframeChange(c.id, tf)}
              targetCandleTime={c.id === focusedId ? activeTarget?.candleTime : null}
            />
          </div>
        ))}
      </div>
    </div>
  );
};
