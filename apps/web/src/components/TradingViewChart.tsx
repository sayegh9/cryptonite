"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  createChart,
  ColorType,
  CandlestickSeries,
  HistogramSeries,
  createSeriesMarkers,
  IChartApi,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  Time,
  SeriesMarker,
} from "lightweight-charts";
import { Bar, SignalCandidate } from "@/lib/types";
import { getStreamClient } from "@/lib/ws";

interface Props {
  chartId: string;
  symbol: string;
  timeframe: string;
  isFocused?: boolean;
  onFocus?: () => void;
  onSymbolChange?: (symbol: string) => void;
  onTimeframeChange?: (tf: string) => void;
  onCrosshairSync?: (sourceId: string, time: number | null, price: number | null) => void;
  syncCrosshair?: { sourceId: string; time: number | null; price: number | null } | null;
  targetCandleTime?: number | null;
}

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"];

export const TradingViewChart: React.FC<Props> = ({
  chartId,
  symbol,
  timeframe,
  isFocused = false,
  onFocus,
  onSymbolChange,
  onTimeframeChange,
  onCrosshairSync,
  syncCrosshair,
  targetCandleTime,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [currentPrice, setCurrentPrice] = useState<number | null>(null);
  const [priceChangePct, setPriceChangePct] = useState<number | null>(null);

  // Monotonic sequence tracker for ticks
  const lastSequenceRef = useRef<number>(-1);
  // Current provisional candle for higher timeframes
  const provisionalCandleRef = useRef<Bar | null>(null);
  const barsCacheRef = useRef<Map<number, Bar>>(new Map());

  // Initialize Lightweight Chart
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
      layout: {
        attributionLogo: true, // MANDATORY TradingView attribution logo
        background: { type: ColorType.Solid, color: "#09090b" },
        textColor: "#a1a1aa",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#18181b" },
        horzLines: { color: "#18181b" },
      },
      crosshair: {
        vertLine: {
          color: "#38bdf8",
          width: 1,
          style: 3,
          labelBackgroundColor: "#0284c7",
        },
        horzLine: {
          color: "#38bdf8",
          width: 1,
          style: 3,
          labelBackgroundColor: "#0284c7",
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

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
    });

    const volumeSeries = chart.addSeries(HistogramSeries, {
      color: "#27272a",
      priceFormat: {
        type: "volume",
      },
      priceScaleId: "", // overlay pane
    });

    volumeSeries.priceScale().applyOptions({
      scaleMargins: {
        top: 0.8,
        bottom: 0,
      },
    });

    const markersPlugin = createSeriesMarkers(candleSeries, []);

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    markersPluginRef.current = markersPlugin;

    // Crosshair move handler
    chart.subscribeCrosshairMove((param) => {
      if (!onCrosshairSync) return;
      if (!param || !param.point || param.time === undefined) {
        onCrosshairSync(chartId, null, null);
      } else {
        const p = param.seriesData.get(candleSeries) as any;
        const price = p ? p.close ?? p.value : null;
        onCrosshairSync(chartId, Number(param.time), price);
      }
    });

    // Handle container resizing
    const resizeObserver = new ResizeObserver((entries) => {
      if (!entries || entries.length === 0 || !entries[0].contentRect) return;
      const { width, height } = entries[0].contentRect;
      chart.applyOptions({ width, height });
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      markersPluginRef.current = null;
    };
  }, []);

  // Sync external crosshair from peer charts
  useEffect(() => {
    if (!chartRef.current || !candleSeriesRef.current || !syncCrosshair) return;
    if (syncCrosshair.sourceId === chartId) return;

    if (syncCrosshair.time === null) {
      chartRef.current.clearCrosshairPosition();
    } else {
      const targetTime = syncCrosshair.time as unknown as Time;
      const price = syncCrosshair.price || 0;
      try {
        chartRef.current.setCrosshairPosition(price, targetTime, candleSeriesRef.current);
      } catch {
        // Time might be outside this chart's scale, ignore
      }
    }
  }, [syncCrosshair, chartId]);

  // Navigate to target candidate candle
  useEffect(() => {
    if (!targetCandleTime || !chartRef.current) return;
    try {
      chartRef.current.timeScale().scrollToPosition(0, false);
      const timeScale = chartRef.current.timeScale();
      timeScale.setVisibleRange({
        from: (targetCandleTime - 1800) as unknown as Time,
        to: (targetCandleTime + 1800) as unknown as Time,
      });
    } catch {
      // ignore out of range
    }
  }, [targetCandleTime]);

  // Fetch historical bars & signal markers
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    lastSequenceRef.current = -1;
    provisionalCandleRef.current = null;
    barsCacheRef.current.clear();

    const fetchHistorical = async () => {
      try {
        const res = await fetch(`/api/v1/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=1000`);
        if (!res.ok) throw new Error(`Failed to load bars: ${res.statusText}`);
        const data = await res.json();
        if (cancelled) return;

        const rawBars = Array.isArray(data) ? data : (data.bars || []);

        const candles = rawBars.map((b: any) => ({
          time: b.time as Time,
          open: Number(b.open),
          high: Number(b.high),
          low: Number(b.low),
          close: Number(b.close),
        }));

        const volumes = rawBars.map((b: any) => ({
          time: b.time as Time,
          value: Number(b.volume),
          color: Number(b.close) >= Number(b.open) ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)",
        }));

        rawBars.forEach((b: any) => {
          barsCacheRef.current.set(b.time, b);
        });

        if (candleSeriesRef.current && volumeSeriesRef.current) {
          candleSeriesRef.current.setData(candles);
          volumeSeriesRef.current.setData(volumes);
          chartRef.current?.timeScale().fitContent();

          if (candles.length > 0) {
            const last = candles[candles.length - 1];
            const first = candles[0];
            setCurrentPrice(last.close);
            const change = ((last.close - first.open) / first.open) * 100;
            setPriceChangePct(change);
          }
        }

        // Fetch signals for markers
        const instId = symbol.includes(".") ? symbol : `${symbol.toUpperCase()}.BINANCE`;
        const sigRes = await fetch(`/api/v1/signals?instrument_id=${encodeURIComponent(instId)}&limit=100`);
        if (sigRes.ok && !cancelled) {
          const sigData = await sigRes.json();
          const rawSignals = Array.isArray(sigData) ? sigData : (sigData.signals || []);
          const markers: SeriesMarker<Time>[] = rawSignals.map((s: SignalCandidate) => ({
            time: Math.floor(s.bar_open_ns / 1_000_000_000) as unknown as Time,
            position: "belowBar",
            color: "#10b981",
            shape: "arrowUp",
          }));
          markersPluginRef.current?.setMarkers(markers);
        }

        setLoading(false);
      } catch (err: any) {
        if (!cancelled) {
          setError(err.message || "Failed to load market data");
          setLoading(false);
        }
      }
    };

    fetchHistorical();

    return () => {
      cancelled = true;
    };
  }, [symbol, timeframe]);

  // Live WebSocket Tick Subscription & Ordering
  useEffect(() => {
    const instId = symbol.includes(".") ? symbol : `${symbol.toUpperCase()}.BINANCE`;
    const client = getStreamClient();

    const unsubscribe = client.subscribe((msg: any) => {
      if (!candleSeriesRef.current || !volumeSeriesRef.current) return;

      // Handle live open kline ticks
      if (msg.type === "open_kline") {
        if (msg.instrument_id !== instId) return;

        // Monotonic sequence enforcement: drop stale/out-of-order ticks
        if (msg.update_sequence <= lastSequenceRef.current) {
          return;
        }
        lastSequenceRef.current = msg.update_sequence;

        setCurrentPrice(msg.close);

        const openTimeSec = Math.floor(msg.bar_open_ns / 1_000_000_000);

        if (timeframe === "1m") {
          // Direct 1m tick update
          candleSeriesRef.current.update({
            time: openTimeSec as unknown as Time,
            open: msg.open,
            high: msg.high,
            low: msg.low,
            close: msg.close,
          });
          volumeSeriesRef.current.update({
            time: openTimeSec as unknown as Time,
            value: msg.volume,
            color: msg.close >= msg.open ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)",
          });
        } else {
          // In-memory provisional aggregation for higher timeframes
          let bucketSeconds = 60;
          if (timeframe === "5m") bucketSeconds = 300;
          else if (timeframe === "15m") bucketSeconds = 900;
          else if (timeframe === "1h") bucketSeconds = 3600;
          else if (timeframe === "4h") bucketSeconds = 14400;
          else if (timeframe === "1d") bucketSeconds = 86400;

          const bucketTimeSec = Math.floor(openTimeSec / bucketSeconds) * bucketSeconds;
          let prov = provisionalCandleRef.current;

          if (!prov || prov.time !== bucketTimeSec) {
            prov = {
              time: bucketTimeSec,
              open: msg.open,
              high: msg.high,
              low: msg.low,
              close: msg.close,
              volume: msg.volume,
              bar_open_ns: bucketTimeSec * 1_000_000_000,
              bar_close_inclusive_ns: (bucketTimeSec + bucketSeconds) * 1_000_000_000 - 1_000_000,
              interval_end_exclusive_ns: (bucketTimeSec + bucketSeconds) * 1_000_000_000,
            };
          } else {
            prov.high = Math.max(prov.high, msg.high);
            prov.low = Math.min(prov.low, msg.low);
            prov.close = msg.close;
            prov.volume += msg.volume;
          }
          provisionalCandleRef.current = prov;

          candleSeriesRef.current.update({
            time: bucketTimeSec as unknown as Time,
            open: prov.open,
            high: prov.high,
            low: prov.low,
            close: prov.close,
          });
        }
      }

      // Handle durable closed bar committed events
      if (msg.type === "closed_bar_committed") {
        if (msg.instrument_id !== instId || msg.timeframe !== timeframe) return;

        const openTimeSec = Math.floor(msg.bar_open_ns / 1_000_000_000);
        candleSeriesRef.current.update({
          time: openTimeSec as unknown as Time,
          open: msg.open,
          high: msg.high,
          low: msg.low,
          close: msg.close,
        });
        volumeSeriesRef.current.update({
          time: openTimeSec as unknown as Time,
          value: msg.volume,
          color: msg.close >= msg.open ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)",
        });
      }

      // Handle live signal candidates
      if (msg.type === "signal_candidate") {
        if (msg.instrument_id === instId && msg.timeframe === timeframe) {
          const currentMarkers = markersPluginRef.current?.markers() || [];
          const newMarker: SeriesMarker<Time> = {
            time: Math.floor(msg.bar_open_ns / 1_000_000_000) as unknown as Time,
            position: "belowBar",
            color: "#10b981",
            shape: "arrowUp",
          };
          markersPluginRef.current?.setMarkers([...currentMarkers, newMarker]);
        }
      }
    });

    return () => {
      unsubscribe();
    };
  }, [symbol, timeframe]);

  return (
    <div
      onClick={onFocus}
      className={`relative flex flex-col h-full w-full bg-zinc-950 border transition-all rounded-md overflow-hidden ${
        isFocused ? "border-cyan-500/80 shadow-lg shadow-cyan-950/20" : "border-zinc-800 hover:border-zinc-700"
      }`}
    >
      {/* Chart Control Header */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-zinc-900/90 border-b border-zinc-800 z-10 select-none">
        <div className="flex items-center space-x-2">
          <span className="font-mono font-bold text-xs text-zinc-200 tracking-wider">
            {symbol.toUpperCase()}
          </span>
          {currentPrice !== null && (
            <span className="font-mono text-xs font-semibold text-zinc-100">
              ${currentPrice.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </span>
          )}
          {priceChangePct !== null && (
            <span
              className={`font-mono text-[10px] px-1.5 py-0.5 rounded font-medium ${
                priceChangePct >= 0 ? "bg-emerald-950/80 text-emerald-400 border border-emerald-800/40" : "bg-red-950/80 text-red-400 border border-red-800/40"
              }`}
            >
              {priceChangePct >= 0 ? "+" : ""}
              {priceChangePct.toFixed(2)}%
            </span>
          )}
        </div>

        {/* Timeframe selector tabs */}
        <div className="flex items-center space-x-1 bg-zinc-950/80 p-0.5 rounded border border-zinc-800/80">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={(e) => {
                e.stopPropagation();
                onTimeframeChange?.(tf);
              }}
              className={`px-2 py-0.5 text-[11px] font-medium rounded transition-colors ${
                timeframe === tf
                  ? "bg-cyan-500 text-zinc-950 font-bold shadow-sm"
                  : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60"
              }`}
            >
              {tf}
            </button>
          ))}
        </div>
      </div>

      {/* Chart Canvas Area */}
      <div className="relative flex-1 w-full h-full min-h-0">
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-zinc-950/60 z-20 backdrop-blur-xs">
            <span className="text-xs font-mono text-zinc-400 animate-pulse">Loading market data...</span>
          </div>
        )}
        {error && (
          <div className="absolute inset-0 flex items-center justify-center bg-zinc-950/80 z-20">
            <span className="text-xs font-mono text-red-400 bg-red-950/40 px-3 py-1.5 border border-red-800/60 rounded">
              {error}
            </span>
          </div>
        )}
        <div ref={containerRef} className="w-full h-full" />
      </div>

      {/* Visible Attribution Link Requirement */}
      <div className="px-3 py-1 bg-zinc-950 border-t border-zinc-900 flex items-center justify-between z-10">
        <a
          href="https://www.tradingview.com/lightweight-charts/"
          target="_blank"
          rel="noopener noreferrer"
          className="text-[10px] text-zinc-500 hover:text-cyan-400 transition-colors"
        >
          Market charts powered by TradingView Lightweight Charts™ (Apache-2.0)
        </a>
        <div className="flex items-center space-x-1.5">
          <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
          <span className="text-[10px] font-mono text-zinc-500">LIVE FEED</span>
        </div>
      </div>
    </div>
  );
};
