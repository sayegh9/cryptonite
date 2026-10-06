"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  createChart,
  ColorType,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
  IChartApi,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  Time,
  SeriesMarker,
} from "lightweight-charts";
import { SignalCandidate } from "@/lib/types";
import { getStreamClient } from "@/lib/ws";
import { BrainCircuit } from "lucide-react";
import { toast } from "sonner";

import { crosshairEmitter } from "@/lib/crosshairEmitter";
import { dispatchSwitchWorkspace } from "@/lib/navigation";
import { TickerPriceCell } from "@/components/terminal/TickerPriceCell";
import {
  CHART_STYLES,
  DOWN_VOLUME,
  TIMEFRAMES,
  UP_VOLUME,
  closedBarFromEvent,
  formatSeriesData,
  nextProvisional,
  seriesPoint,
  type ChartStyle,
  type ProvisionalBar,
} from "@/components/chart/bars";
import { createMainSeries } from "@/components/chart/series";
import { indicatorSeries, type ChartIndicator } from "@/components/chart/indicators";
import { IndicatorsDialog } from "@/components/chart/IndicatorsDialog";

export type { ChartStyle } from "@/components/chart/bars";
export type { ChartIndicator } from "@/components/chart/indicators";

interface Props {
  chartId: string;
  symbol: string;
  timeframe: string;
  isFocused?: boolean;
  onFocus?: () => void;
  onSymbolChange?: (symbol: string) => void;
  onTimeframeChange?: (tf: string) => void;
  targetCandleTime?: number | null;
}

/** A tick older than this means the live feed is not flowing for this chart. */
const LIVE_TICK_MAX_AGE_MS = 10_000;

export function TradingViewChart({
  chartId,
  symbol,
  timeframe,
  isFocused = false,
  onFocus,
  onTimeframeChange,
  targetCandleTime,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<any> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const currentMarkersRef = useRef<SeriesMarker<Time>[]>([]);
  const lastPriceUpdateRef = useRef<number>(0);

  const [chartStyle, setChartStyle] = useState<ChartStyle>("candlestick");
  const chartStyleRef = useRef<ChartStyle>(chartStyle);
  chartStyleRef.current = chartStyle;

  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [currentPrice, setCurrentPrice] = useState<number | null>(null);
  /** 24h change from /market/prices; unknown (null) until that call answers */
  const [priceChangePct, setPriceChangePct] = useState<number | null>(null);
  const [lastTickAt, setLastTickAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const [indicators, setIndicators] = useState<ChartIndicator[]>([]);
  const indicatorSeriesRef = useRef<Record<string, ISeriesApi<"Line">>>({});
  const rawBarsRef = useRef<any[]>([]); // pure bars for indicator recalculation

  // Monotonic sequence tracker for ticks
  const lastSequenceRef = useRef<number>(-1);
  // Current provisional candle (aggregated for higher timeframes)
  const provisionalCandleRef = useRef<ProvisionalBar | null>(null);
  // The provisional candle just replaced by the next bar's first tick (its commit may still be in flight)
  const previousProvisionalRef = useRef<ProvisionalBar | null>(null);

  const [triggeringDebate, setTriggeringDebate] = useState(false);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 2000);
    return () => clearInterval(t);
  }, []);
  const feedLive = lastTickAt !== null && now - lastTickAt < LIVE_TICK_MAX_AGE_MS;

  const handleStyleChange = (newStyle: ChartStyle) => {
    if (newStyle === chartStyle || !chartRef.current) return;
    setChartStyle(newStyle);
    chartStyleRef.current = newStyle;

    const chart = chartRef.current;
    if (candleSeriesRef.current) chart.removeSeries(candleSeriesRef.current);
    const newSeries = createMainSeries(chart, newStyle);
    candleSeriesRef.current = newSeries;
    if (rawBarsRef.current.length > 0) newSeries.setData(formatSeriesData(rawBarsRef.current, newStyle) as any);
    if (currentMarkersRef.current.length > 0) {
      markersPluginRef.current = createSeriesMarkers(newSeries, currentMarkersRef.current);
    }
  };

  // Queues a real debate; the run appears in the AI Brain inbox once the worker reports it
  const handleTriggerDebate = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setTriggeringDebate(true);
    try {
      const res = await fetch("/agent-api/debates/trigger", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": typeof window !== "undefined" ? localStorage.getItem("csrf_token") || "" : "",
        },
        body: JSON.stringify({ symbol, timeframe }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.message || `Server responded with ${res.status}`);
      toast.success(`Debate queued: ${symbol}`, { description: "Its card appears in the AI Brain inbox when the worker reports it." });
      dispatchSwitchWorkspace({ mode: "debates", view: "debates" });
    } catch (err: any) {
      toast.error(`Debate trigger error: ${err.message}`);
    } finally {
      setTriggeringDebate(false);
    }
  };

  const updateIndicators = (bars: any[], list: ChartIndicator[] = indicators) => {
    const chart = chartRef.current;
    if (!chart) return;
    for (const ind of list) {
      const data = indicatorSeries(ind, bars);
      if (!data) continue;
      if (!indicatorSeriesRef.current[ind.id]) {
        indicatorSeriesRef.current[ind.id] = chart.addSeries(LineSeries, ind.pane === "main"
          ? { color: ind.color, lineWidth: 2, crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false }
          : { color: ind.color, lineWidth: 2, priceScaleId: "rsi_scale", crosshairMarkerVisible: false });
        if (ind.pane !== "main") chart.priceScale("rsi_scale").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
      }
      indicatorSeriesRef.current[ind.id].setData(data);
    }
  };

  useEffect(() => {
    if (rawBarsRef.current.length > 0) updateIndicators(rawBarsRef.current);
  }, [indicators]);

  const removeIndicator = (id: string) => {
    if (indicatorSeriesRef.current[id] && chartRef.current) {
      chartRef.current.removeSeries(indicatorSeriesRef.current[id]);
      delete indicatorSeriesRef.current[id];
    }
    setIndicators((list) => list.filter((i) => i.id !== id));
  };

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
      grid: { vertLines: { color: "#18181b" }, horzLines: { color: "#18181b" } },
      crosshair: {
        vertLine: { color: "#38bdf8", width: 1, style: 3, labelBackgroundColor: "#0284c7" },
        horzLine: { color: "#38bdf8", width: 1, style: 3, labelBackgroundColor: "#0284c7" },
      },
      rightPriceScale: { borderColor: "#27272a", autoScale: true },
      timeScale: { borderColor: "#27272a", timeVisible: true, secondsVisible: false },
    });

    const candleSeries = createMainSeries(chart, chartStyleRef.current);
    const volumeSeries = chart.addSeries(HistogramSeries, {
      color: "#27272a",
      priceFormat: { type: "volume" },
      priceScaleId: "", // overlay pane
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    const markersPlugin = createSeriesMarkers(candleSeries, []);

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    markersPluginRef.current = markersPlugin;

    chart.subscribeCrosshairMove((param) => {
      if (!param || !param.point || param.time === undefined) {
        crosshairEmitter.emit({ sourceId: chartId, time: null, price: null });
      } else {
        const p = param.seriesData.get(candleSeries) as any;
        const price = p ? p.close ?? p.value : null;
        crosshairEmitter.emit({ sourceId: chartId, time: Number(param.time), price });
      }
    });

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
    return crosshairEmitter.subscribe((syncCrosshair) => {
      if (!chartRef.current || !candleSeriesRef.current) return;
      if (syncCrosshair.sourceId === chartId) return;
      if (syncCrosshair.time === null) {
        chartRef.current.clearCrosshairPosition();
        return;
      }
      try {
        chartRef.current.setCrosshairPosition(syncCrosshair.price || 0, syncCrosshair.time as unknown as Time, candleSeriesRef.current);
      } catch {
        // Time might be outside this chart's scale, ignore
      }
    });
  }, [chartId]);

  // Navigate to target candidate candle
  useEffect(() => {
    if (!targetCandleTime || !chartRef.current) return;
    try {
      chartRef.current.timeScale().scrollToPosition(0, false);
      chartRef.current.timeScale().setVisibleRange({
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
    setCurrentPrice(null);
    setPriceChangePct(null);
    setLastTickAt(null);
    lastSequenceRef.current = -1;
    provisionalCandleRef.current = null;
    previousProvisionalRef.current = null;

    const fetchHistorical = async () => {
      try {
        candleSeriesRef.current?.setData([]);
        volumeSeriesRef.current?.setData([]);

        const res = await fetch(`/api/v1/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=1000`);
        if (!res.ok) throw new Error(`Failed to load bars: ${res.statusText}`);
        const data = await res.json();
        if (cancelled) return;

        const rawBars = Array.isArray(data) ? data : data.bars || [];
        rawBarsRef.current = rawBars;
        updateIndicators(rawBars);

        if (candleSeriesRef.current && volumeSeriesRef.current) {
          candleSeriesRef.current.setData(formatSeriesData(rawBars, chartStyleRef.current) as any);
          volumeSeriesRef.current.setData(rawBars.map((b: any) => ({
            time: b.time as Time,
            value: Number(b.volume),
            color: Number(b.close) >= Number(b.open) ? UP_VOLUME : DOWN_VOLUME,
          })));
          chartRef.current?.timeScale().fitContent();

          // Last close of the loaded bars; the 24h change comes only from market stats (no proxy value)
          if (rawBars.length > 0) setCurrentPrice(Number(rawBars[rawBars.length - 1].close));

          const cleanSym = symbol.split(".")[0].toUpperCase();
          fetch(`/api/v1/market/prices?symbols=${encodeURIComponent(cleanSym)}`)
            .then((r) => (r.ok ? r.json() : null))
            .then((stats) => {
              if (cancelled || !stats) return;
              const s = Array.isArray(stats) ? stats.find((item: any) => item.symbol === cleanSym) : stats[cleanSym];
              if (s && s.last_price) {
                setCurrentPrice(Number(s.last_price));
                if (s.price_change_pct !== undefined && s.price_change_pct !== null) setPriceChangePct(Number(s.price_change_pct));
              }
            })
            .catch(() => {});
        }

        // Signal markers
        const instId = symbol.includes(".") ? symbol : `${symbol.toUpperCase()}.BINANCE`;
        const sigRes = await fetch(`/api/v1/signals?instrument_id=${encodeURIComponent(instId)}&limit=100`);
        if (sigRes.ok && !cancelled) {
          const sigData = await sigRes.json();
          const rawSignals = Array.isArray(sigData) ? sigData : sigData.signals || [];
          const markers: SeriesMarker<Time>[] = rawSignals.map((s: SignalCandidate) => ({
            time: Math.floor(s.bar_open_ns / 1_000_000_000) as unknown as Time,
            position: "belowBar",
            color: "#10b981",
            shape: "arrowUp",
          }));
          currentMarkersRef.current = markers;
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

  // Live WebSocket ticks, closed bars and signals
  useEffect(() => {
    const instId = symbol.includes(".") ? symbol : `${symbol.toUpperCase()}.BINANCE`;
    const client = getStreamClient();

    const unsubscribe = client.subscribe((msg: any) => {
      if (!candleSeriesRef.current || !volumeSeriesRef.current) return;

      if (msg.type === "open_kline") {
        if (msg.instrument_id !== instId) return;
        // Monotonic sequence enforcement: drop stale/out-of-order ticks
        if (msg.update_sequence <= lastSequenceRef.current) return;
        lastSequenceRef.current = msg.update_sequence;

        // Throttle React state updates to ~150ms
        const t = typeof performance !== "undefined" ? performance.now() : Date.now();
        if (t - lastPriceUpdateRef.current > 150) {
          lastPriceUpdateRef.current = t;
          setCurrentPrice(Number(msg.close));
          setLastTickAt(Date.now());
        }

        const prev = provisionalCandleRef.current;
        const prov = nextProvisional(prev, msg, timeframe);
        if (prev && prev.time !== prov.time) previousProvisionalRef.current = prev;
        provisionalCandleRef.current = prov;
        candleSeriesRef.current.update(seriesPoint(prov.time, prov, chartStyleRef.current));
        if (timeframe === "1m") {
          volumeSeriesRef.current.update({
            time: prov.time as unknown as Time,
            value: prov.volume,
            color: prov.close >= prov.open ? UP_VOLUME : DOWN_VOLUME,
          });
        }
      }

      // Durable closed bar: commit the provisional bar and recalculate indicators
      if (msg.type === "closed_bar_committed") {
        if (msg.instrument_id !== instId || msg.timeframe !== timeframe) return;
        const bar = closedBarFromEvent(msg, provisionalCandleRef.current, previousProvisionalRef.current);
        if (!bar) return;

        const bars = rawBarsRef.current;
        const lastTime = bars.length ? bars[bars.length - 1].time : -Infinity;
        if (bar.time < lastTime) return; // older than the history already drawn
        // Draw the committed values; the provisional candle may already have moved on to the next bar
        const historical = (provisionalCandleRef.current?.time ?? -Infinity) > bar.time;
        try {
          candleSeriesRef.current.update(seriesPoint(bar.time, bar, chartStyleRef.current), historical);
          if (timeframe === "1m") {
            volumeSeriesRef.current.update({
              time: bar.time as unknown as Time,
              value: bar.volume,
              color: bar.close >= bar.open ? UP_VOLUME : DOWN_VOLUME,
            }, historical);
          }
        } catch {
          // a historical point the series never had: the indicators below still get the committed bar
        }
        // Only a batch's last bar is applied: a multi-bar recovery batch newer than the drawn history
        // leaves the bars before it missing from rawBarsRef (follow-up: refetch history when bar_count > 1).
        if (bar.time === lastTime) bars[bars.length - 1] = bar;
        else {
          bars.push(bar);
          if (bars.length > 2000) bars.shift(); // prevent unbounded memory
        }
        updateIndicators(bars);
      }

      if (msg.type === "signal_candidate") {
        const sig = msg.data || msg;
        if (sig.instrument_id === instId && sig.timeframe === timeframe && sig.bar_open_ns) {
          const currentMarkers = markersPluginRef.current?.markers() || [];
          markersPluginRef.current?.setMarkers([
            ...currentMarkers,
            { time: Math.floor(sig.bar_open_ns / 1_000_000_000) as unknown as Time, position: "belowBar", color: "#10b981", shape: "arrowUp" },
          ]);
        }
      }
    });

    return () => unsubscribe();
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
          <span className="font-mono font-bold text-xs text-zinc-200 tracking-wider">{symbol.toUpperCase()}</span>
          <TickerPriceCell price={currentPrice} />
          {priceChangePct !== null && (
            <span
              title="24h change"
              className={`font-mono text-2xs px-1.5 py-0.5 rounded font-medium ${
                priceChangePct >= 0 ? "bg-emerald-950/80 text-emerald-400 border border-emerald-800/40" : "bg-red-950/80 text-red-400 border border-red-800/40"
              }`}
            >
              {priceChangePct >= 0 ? "+" : ""}
              {priceChangePct.toFixed(2)}%
            </span>
          )}
        </div>

        <div className="flex items-center space-x-2">
          <button
            onClick={handleTriggerDebate}
            disabled={triggeringDebate}
            className="flex items-center space-x-1.5 px-2 py-0.5 text-2xs font-medium rounded text-purple-400 hover:text-purple-200 bg-purple-950/20 hover:bg-purple-950/50 transition-colors border border-purple-800/40 hover:border-purple-600/60 focus-visible:ring-1 focus-visible:ring-purple-500 outline-none"
            title={`Trigger AI Consensus Debate on ${symbol}`}
            aria-label={`Trigger AI Consensus Debate on ${symbol}`}
          >
            <BrainCircuit className="w-3 h-3" aria-hidden="true" />
            <span>{triggeringDebate ? "Queuing..." : "Run Debate"}</span>
          </button>

          <select
            value={chartStyle}
            onChange={(e) => {
              e.stopPropagation();
              handleStyleChange(e.target.value as ChartStyle);
            }}
            className="bg-zinc-900 border border-zinc-800 hover:border-zinc-700 text-zinc-300 rounded px-2 py-0.5 text-2xs font-medium focus:outline-none focus:ring-1 focus:ring-cyan-500 cursor-pointer transition-colors"
            title="Select Chart Style"
            aria-label="Select Chart Style"
          >
            {CHART_STYLES.map((style) => (
              <option key={style.id} value={style.id} className="bg-zinc-900 text-zinc-200">
                {style.label}
              </option>
            ))}
          </select>

          <IndicatorsDialog
            indicators={indicators}
            onAdd={(ind) => setIndicators((list) => [...list, ind])}
            onRemove={removeIndicator}
          />
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
              aria-pressed={timeframe === tf}
              className={`px-2 py-0.5 text-2xs font-medium rounded transition-colors ${
                timeframe === tf ? "bg-cyan-500 text-zinc-950 font-bold shadow-sm" : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60"
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
            <span className="text-xs font-mono text-red-400 bg-red-950/40 px-3 py-1.5 border border-red-800/60 rounded">{error}</span>
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
          className="text-2xs text-zinc-500 hover:text-cyan-400 transition-colors"
        >
          Market charts powered by TradingView Lightweight Charts™ (Apache-2.0)
        </a>
        <div className="flex items-center space-x-1.5" role="status" data-testid="chart-feed-status">
          <div className={`w-1.5 h-1.5 rounded-full ${feedLive ? "bg-emerald-500 animate-pulse" : "bg-zinc-500"}`} aria-hidden="true" />
          <span className="text-2xs font-mono text-zinc-500">{feedLive ? "LIVE FEED" : "NO LIVE TICKS"}</span>
        </div>
      </div>
    </div>
  );
}
