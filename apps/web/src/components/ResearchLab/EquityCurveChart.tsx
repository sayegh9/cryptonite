"use client";

import React, { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  AreaSeries,
  LineSeries,
  createSeriesMarkers,
  IChartApi,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  Time,
  SeriesMarker,
} from "lightweight-charts";
import { DownsampledEquityPoint, BacktestTrade } from "@/lib/types";

interface Props {
  equityPoints: DownsampledEquityPoint[];
  selectedTrade?: BacktestTrade | null;
}

export const EquityCurveChart: React.FC<Props> = ({
  equityPoints,
  selectedTrade,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const equitySeriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const drawdownSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight || 340,
      layout: {
        background: { type: ColorType.Solid, color: "#09090b" },
        textColor: "#a1a1aa",
        fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#18181b" },
        horzLines: { color: "#18181b" },
      },
      crosshair: {
        vertLine: {
          color: "#06b6d4",
          width: 1,
          style: 3,
          labelBackgroundColor: "#0891b2",
        },
        horzLine: {
          color: "#06b6d4",
          width: 1,
          style: 3,
          labelBackgroundColor: "#0891b2",
        },
      },
      rightPriceScale: {
        borderColor: "#27272a",
        autoScale: true,
      },
      timeScale: {
        borderColor: "#27272a",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const equitySeries = chart.addSeries(AreaSeries, {
      topColor: "rgba(16, 185, 129, 0.35)",
      bottomColor: "rgba(16, 185, 129, 0.0)",
      lineColor: "#10b981",
      lineWidth: 2,
      priceFormat: {
        type: "price",
        precision: 2,
        minMove: 0.01,
      },
    });

    const drawdownSeries = chart.addSeries(LineSeries, {
      color: "#f43f5e",
      lineWidth: 2,
      priceScaleId: "drawdown",
      priceFormat: {
        type: "percent",
      },
    });

    chart.priceScale("drawdown").applyOptions({
      scaleMargins: {
        top: 0.75,
        bottom: 0.02,
      },
    });

    const markersPlugin = createSeriesMarkers(equitySeries, []);

    chartRef.current = chart;
    equitySeriesRef.current = equitySeries;
    drawdownSeriesRef.current = drawdownSeries;
    markersPluginRef.current = markersPlugin;

    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect && chartRef.current) {
          chartRef.current.applyOptions({
            width: entry.contentRect.width,
            height: entry.contentRect.height,
          });
        }
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // Update data series
  useEffect(() => {
    if (!chartRef.current || !equitySeriesRef.current || !drawdownSeriesRef.current) return;

    if (!equityPoints || equityPoints.length === 0) {
      equitySeriesRef.current.setData([]);
      drawdownSeriesRef.current.setData([]);
      return;
    }

    // Normalize and sort ascending by time
    const getTimeSec = (p: any): number => {
      if (typeof p.time === "number") return p.time;
      if (typeof p.ts === "number") return p.ts > 1e11 ? Math.floor(p.ts / 1000) : p.ts;
      return 0;
    };
    const getEquity = (p: any): number => {
      if (typeof p.equity === "number") return p.equity;
      if (typeof p.value === "number") return p.value;
      return 0;
    };

    const sorted = [...equityPoints].sort((a, b) => getTimeSec(a) - getTimeSec(b));
    const eqData = sorted.map((p) => ({
      time: getTimeSec(p) as Time,
      value: getEquity(p),
    }));
    const ddData = sorted.map((p) => ({
      time: getTimeSec(p) as Time,
      value: -((p.drawdown ?? 0) * 100), // Negative percentage for drawdown
    }));

    equitySeriesRef.current.setData(eqData);
    drawdownSeriesRef.current.setData(ddData);
    chartRef.current.timeScale().fitContent();
  }, [equityPoints]);

  // Update Trade Markers when selectedTrade changes
  useEffect(() => {
    if (!markersPluginRef.current || !selectedTrade) {
      if (markersPluginRef.current) markersPluginRef.current.setMarkers([]);
      return;
    }

    const markers: SeriesMarker<Time>[] = [];
    const entrySec = Math.floor(selectedTrade.ts_opened_ns / 1e9) as Time;
    const isLong = selectedTrade.side.toUpperCase().includes("LONG") || selectedTrade.side.toUpperCase().includes("BUY");

    markers.push({
      time: entrySec,
      position: isLong ? "belowBar" : "aboveBar",
      color: "#06b6d4",
      shape: isLong ? "arrowUp" : "arrowDown",
      text: `ENTRY ${selectedTrade.side} @ ${selectedTrade.entry_price.toFixed(2)}`,
    });

    if (selectedTrade.ts_closed_ns > 0) {
      const exitSec = Math.floor(selectedTrade.ts_closed_ns / 1e9) as Time;
      const isProfitable = selectedTrade.realized_pnl >= 0;
      markers.push({
        time: exitSec,
        position: isLong ? "aboveBar" : "belowBar",
        color: isProfitable ? "#10b981" : "#ef4444",
        shape: "circle",
        text: `EXIT @ ${selectedTrade.exit_price.toFixed(2)} (${isProfitable ? "+" : ""}${selectedTrade.realized_pnl.toFixed(2)} USDT)`,
      });
    }

    markersPluginRef.current.setMarkers(markers);
  }, [selectedTrade]);

  return (
    <div className="relative w-full h-full flex flex-col bg-zinc-950">
      <div className="flex items-center justify-between px-3 py-1.5 bg-zinc-900/60 border-b border-zinc-800/80 text-2xs font-mono text-zinc-400">
        <div className="flex items-center space-x-3">
          <span className="font-semibold text-zinc-200">EQUITY & DRAWDOWN CURVE</span>
          <span className="flex items-center space-x-1">
            <span className="w-2 h-2 rounded-full bg-emerald-500 inline-block" />
            <span className="text-emerald-400">Portfolio Equity</span>
          </span>
          <span className="flex items-center space-x-1">
            <span className="w-2 h-2 rounded-full bg-rose-500 inline-block" />
            <span className="text-rose-400">Drawdown (%)</span>
          </span>
        </div>
        {selectedTrade && (
          <div className="text-cyan-400 bg-cyan-950/60 border border-cyan-800/50 px-2 py-0.5 rounded text-2xs">
            Viewing Trade: {selectedTrade.side} ({selectedTrade.realized_pnl >= 0 ? "+" : ""}{selectedTrade.realized_pnl.toFixed(2)} USDT)
          </div>
        )}
      </div>
      <div ref={containerRef} className="flex-1 w-full min-h-0" />
    </div>
  );
};
