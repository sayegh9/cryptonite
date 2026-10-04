"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  createChart,
  ColorType,
  CandlestickSeries,
  BarSeries,
  AreaSeries,
  BaselineSeries,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
  IChartApi,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  Time,
  SeriesMarker,
} from "lightweight-charts";
import { Bar, SignalCandidate } from "@/lib/types";
import { getStreamClient } from "@/lib/ws";
import { useDebateStore } from "@/lib/stores/useDebateStore";
import { SMA, EMA, WMA, WEMA, RSI, ATR, ROC, WilliamsR, TRIX, CCI, ForceIndex, MFI, SD, AverageGain, AverageLoss } from "technicalindicators";
import { Settings2, Plus, X, BrainCircuit } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

import { crosshairEmitter } from "@/lib/crosshairEmitter";
import { TickerPriceCell } from "@/components/terminal/TickerPriceCell";

export interface ChartIndicator {
  id: string;
  type: "SMA" | "EMA" | "WMA" | "WEMA" | "RSI" | "ATR" | "ROC" | "WilliamsR" | "TRIX" | "CCI" | "ForceIndex" | "MFI" | "SD" | "AverageGain" | "AverageLoss";
  period: number;
  color: string;
  pane: "main" | "bottom";
}
const DEFAULT_COLORS = ["#3b82f6", "#eab308", "#ec4899", "#8b5cf6", "#10b981"];

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

export type ChartStyle = "candlestick" | "bar" | "line" | "area" | "baseline";

const CHART_STYLES: { id: ChartStyle; label: string }[] = [
  { id: "candlestick", label: "Candles" },
  { id: "bar", label: "Bars" },
  { id: "line", label: "Line" },
  { id: "area", label: "Area" },
  { id: "baseline", label: "Baseline" },
];

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"];

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

  const [indicators, setIndicators] = useState<ChartIndicator[]>([]);
  const [showIndDialog, setShowIndDialog] = useState(false);
  const [newIndType, setNewIndType] = useState<"SMA"|"EMA"|"RSI">("SMA");
  const [newIndPeriod, setNewIndPeriod] = useState(14);
  const indicatorSeriesRef = useRef<Record<string, ISeriesApi<"Line">>>({});
  const rawBarsRef = useRef<any[]>([]); // Keep track of pure bars for recalculation

  const [priceChangePct, setPriceChangePct] = useState<number | null>(null);

  // Monotonic sequence tracker for ticks
  const lastSequenceRef = useRef<number>(-1);
  // Current provisional candle for higher timeframes
  const provisionalCandleRef = useRef<Bar | null>(null);
  const barsCacheRef = useRef<Map<number, Bar>>(new Map());

  const [triggeringDebate, setTriggeringDebate] = useState(false);

  const createMainSeries = (chart: IChartApi, style: ChartStyle): ISeriesApi<any> => {
    switch (style) {
      case "bar":
        return chart.addSeries(BarSeries, {
          upColor: "#10b981",
          downColor: "#ef4444",
        });
      case "line":
        return chart.addSeries(LineSeries, {
          color: "#38bdf8",
          lineWidth: 2,
        });
      case "area":
        return chart.addSeries(AreaSeries, {
          topColor: "rgba(56, 189, 248, 0.4)",
          bottomColor: "rgba(56, 189, 248, 0.0)",
          lineColor: "#38bdf8",
          lineWidth: 2,
        });
      case "baseline":
        return chart.addSeries(BaselineSeries, {
          topLineColor: "#10b981",
          topFillColor1: "rgba(16, 185, 129, 0.28)",
          topFillColor2: "rgba(16, 185, 129, 0.05)",
          bottomLineColor: "#ef4444",
          bottomFillColor1: "rgba(239, 68, 68, 0.05)",
          bottomFillColor2: "rgba(239, 68, 68, 0.28)",
          lineWidth: 2,
        });
      case "candlestick":
      default:
        return chart.addSeries(CandlestickSeries, {
          upColor: "#10b981",
          downColor: "#ef4444",
          borderVisible: false,
          wickUpColor: "#10b981",
          wickDownColor: "#ef4444",
        });
    }
  };

  const formatSeriesData = (rawBars: any[], style: ChartStyle) => {
    if (style === "candlestick" || style === "bar") {
      return rawBars.map((b: any) => ({
        time: b.time as Time,
        open: Number(b.open),
        high: Number(b.high),
        low: Number(b.low),
        close: Number(b.close),
      }));
    }
    return rawBars.map((b: any) => ({
      time: b.time as Time,
      value: Number(b.close),
    }));
  };

  const handleStyleChange = (newStyle: ChartStyle) => {
    if (newStyle === chartStyle || !chartRef.current) return;
    setChartStyle(newStyle);
    chartStyleRef.current = newStyle;

    const chart = chartRef.current;
    if (candleSeriesRef.current) {
      chart.removeSeries(candleSeriesRef.current);
    }

    const newSeries = createMainSeries(chart, newStyle);
    candleSeriesRef.current = newSeries;

    if (rawBarsRef.current.length > 0) {
      const data = formatSeriesData(rawBarsRef.current, newStyle);
      newSeries.setData(data as any);
    }

    if (currentMarkersRef.current.length > 0) {
      markersPluginRef.current = createSeriesMarkers(newSeries, currentMarkersRef.current);
    }
  };

  const handleTriggerDebate = async (e: React.MouseEvent) => {
    const addTurn = useDebateStore.getState().addTurn;
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
      if (!res.ok) {
        throw new Error(data.detail || data.message || `Server responded with ${res.status}`);
      }
      // Optimistically add the debate run so it shows in the UI immediately
      addTurn({
        runId: data.id || "pending-" + Date.now(),
        role: "System",
        content: "Gathering intelligence and preparing agent context...",
        timestamp: Date.now(),
      });
      toast.success(`AI Debate Initiated: ${symbol}`, {
        description: "Bull & Bear research agents are analyzing market structure.",
      });
      window.dispatchEvent(new CustomEvent("cryptonite:switch-workspace", { detail: { mode: "debates" } }));
    } catch (err: any) {
      toast.error(`Debate trigger error: ${err.message}`);
    } finally {
      setTriggeringDebate(false);
    }
  };

  const updateIndicators = (bars: any[]) => {
    if (!chartRef.current) return;
    const chart = chartRef.current;
    
    indicators.forEach((ind) => {
      let values: number[] = [];
      const closes = bars.map((b) => Number(b.close));
      
      const highs = bars.map(b => Number(b.high));
      const lows = bars.map(b => Number(b.low));
      const vols = bars.map(b => Number(b.volume));

      if (ind.type === "SMA") values = SMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "EMA") values = EMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "WMA") values = WMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "WEMA") values = WEMA.calculate({ period: ind.period, values: closes });
      else if (ind.type === "RSI") values = RSI.calculate({ period: ind.period, values: closes });
      else if (ind.type === "ATR") values = ATR.calculate({ period: ind.period, high: highs, low: lows, close: closes });
      else if (ind.type === "ROC") values = ROC.calculate({ period: ind.period, values: closes });
      else if (ind.type === "WilliamsR") values = WilliamsR.calculate({ period: ind.period, high: highs, low: lows, close: closes });
      else if (ind.type === "TRIX") values = TRIX.calculate({ period: ind.period, values: closes });
      else if (ind.type === "CCI") values = CCI.calculate({ period: ind.period, high: highs, low: lows, close: closes });
      else if (ind.type === "ForceIndex") values = ForceIndex.calculate({ period: ind.period, close: closes, volume: vols });
      else if (ind.type === "MFI") values = MFI.calculate({ period: ind.period, high: highs, low: lows, close: closes, volume: vols });
      else if (ind.type === "SD") values = SD.calculate({ period: ind.period, values: closes });
      else if (ind.type === "AverageGain") values = AverageGain.calculate({ period: ind.period, values: closes });
      else if (ind.type === "AverageLoss") values = AverageLoss.calculate({ period: ind.period, values: closes });

      const offset = bars.length - values.length;
      if (offset < 0) return;

      const indData = values.map((val, i) => {
        const b = bars[i + offset];
        const t = b.time !== undefined ? b.time : Math.floor((b.bar_open_ns || 0) / 1_000_000_000);
        return {
          time: t as unknown as Time,
          value: val,
        };
      });

      if (!indicatorSeriesRef.current[ind.id]) {
        if (ind.pane === "main") {
          indicatorSeriesRef.current[ind.id] = chart.addSeries(LineSeries, {
            color: ind.color,
            lineWidth: 2,
            crosshairMarkerVisible: false,
            lastValueVisible: false,
            priceLineVisible: false,
          });
        } else {
          indicatorSeriesRef.current[ind.id] = chart.addSeries(LineSeries, {
            color: ind.color,
            lineWidth: 2,
            priceScaleId: "rsi_scale",
            crosshairMarkerVisible: false,
          });
          chart.priceScale("rsi_scale").applyOptions({
            scaleMargins: { top: 0.8, bottom: 0 },
          });
        }
      }
      
      indicatorSeriesRef.current[ind.id].setData(indData);
    });
  };

  useEffect(() => {
    if (rawBarsRef.current.length > 0) {
      updateIndicators(rawBarsRef.current);
    }
  }, [indicators]);


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

    const candleSeries = createMainSeries(chart, chartStyleRef.current);

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
      if (!param || !param.point || param.time === undefined) {
        crosshairEmitter.emit({ sourceId: chartId, time: null, price: null });
      } else {
        const p = param.seriesData.get(candleSeries) as any;
        const price = p ? p.close ?? p.value : null;
        crosshairEmitter.emit({ sourceId: chartId, time: Number(param.time), price });
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
    return crosshairEmitter.subscribe((syncCrosshair) => {
      if (!chartRef.current || !candleSeriesRef.current) return;
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
    });
  }, [chartId]);

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
    setCurrentPrice(null);
    setPriceChangePct(null);
    lastSequenceRef.current = -1;
    provisionalCandleRef.current = null;
    barsCacheRef.current.clear();

    const fetchHistorical = async () => {
      try {
        if (candleSeriesRef.current && volumeSeriesRef.current) {
          candleSeriesRef.current.setData([]);
          volumeSeriesRef.current.setData([]);
        }

        const res = await fetch(`/api/v1/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=1000`);
        if (!res.ok) throw new Error(`Failed to load bars: ${res.statusText}`);
        const data = await res.json();
        if (cancelled) return;

        const rawBars = Array.isArray(data) ? data : (data.bars || []);

        rawBarsRef.current = rawBars;
        const seriesData = formatSeriesData(rawBars, chartStyleRef.current);

        const volumes = rawBars.map((b: any) => ({
          time: b.time as Time,
          value: Number(b.volume),
          color: Number(b.close) >= Number(b.open) ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)",
        }));
        
        updateIndicators(rawBars);

        rawBars.forEach((b: any) => {
          barsCacheRef.current.set(b.time, b);
        });

        if (candleSeriesRef.current && volumeSeriesRef.current) {
          candleSeriesRef.current.setData(seriesData as any);
          volumeSeriesRef.current.setData(volumes);
          chartRef.current?.timeScale().fitContent();

          if (rawBars.length > 0) {
            const last = rawBars[rawBars.length - 1];
            const first = rawBars[0];
            setCurrentPrice(Number(last.close));
            const change = ((Number(last.close) - Number(first.open)) / Number(first.open)) * 100;
            setPriceChangePct(change);
          }

          // Hydrate real-time 24h price & change from market stats
          const cleanSym = symbol.split(".")[0].toUpperCase();
          fetch(`/api/v1/market/prices?symbols=${encodeURIComponent(cleanSym)}`)
            .then((r) => (r.ok ? r.json() : null))
            .then((stats) => {
              if (cancelled || !stats) return;
              const s = Array.isArray(stats) ? stats.find((item: any) => item.symbol === cleanSym) : stats[cleanSym];
              if (s && s.last_price) {
                setCurrentPrice(Number(s.last_price));
                if (s.price_change_pct !== undefined) {
                  setPriceChangePct(Number(s.price_change_pct));
                }
              }
            })
            .catch(() => {});
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

        // Throttle React state updates to ~150ms to prevent heavy re-renders
        const now = typeof performance !== "undefined" ? performance.now() : Date.now();
        if (now - lastPriceUpdateRef.current > 150) {
          lastPriceUpdateRef.current = now;
          setCurrentPrice(Number(msg.close));
        }

        const openTimeSec = Math.floor(msg.bar_open_ns / 1_000_000_000);

        if (timeframe === "1m") {
          // Direct 1m tick update
          const prov = {
            time: openTimeSec,
            open: Number(msg.open),
            high: Number(msg.high),
            low: Number(msg.low),
            close: Number(msg.close),
            volume: Number(msg.volume),
            bar_open_ns: openTimeSec * 1_000_000_000,
            bar_close_inclusive_ns: (openTimeSec + 60) * 1_000_000_000 - 1_000_000,
            interval_end_exclusive_ns: (openTimeSec + 60) * 1_000_000_000,
          };
          provisionalCandleRef.current = prov;

          if (chartStyleRef.current === "candlestick" || chartStyleRef.current === "bar") {
            candleSeriesRef.current.update({
              time: prov.time as unknown as Time,
              open: prov.open,
              high: prov.high,
              low: prov.low,
              close: prov.close,
            });
          } else {
            candleSeriesRef.current.update({
              time: prov.time as unknown as Time,
              value: prov.close,
            });
          }
          volumeSeriesRef.current.update({
            time: prov.time as unknown as Time,
            value: prov.volume,
            color: prov.close >= prov.open ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)",
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
              open: Number(msg.open),
              high: Number(msg.high),
              low: Number(msg.low),
              close: Number(msg.close),
              volume: Number(msg.volume),
              bar_open_ns: bucketTimeSec * 1_000_000_000,
              bar_close_inclusive_ns: (bucketTimeSec + bucketSeconds) * 1_000_000_000 - 1_000_000,
              interval_end_exclusive_ns: (bucketTimeSec + bucketSeconds) * 1_000_000_000,
            };
          } else {
            prov.high = Math.max(prov.high, Number(msg.high));
            prov.low = Math.min(prov.low, Number(msg.low));
            prov.close = Number(msg.close);
            prov.volume += Number(msg.volume);
          }
          provisionalCandleRef.current = prov;

          if (chartStyleRef.current === "candlestick" || chartStyleRef.current === "bar") {
            candleSeriesRef.current.update({
              time: bucketTimeSec as unknown as Time,
              open: prov.open,
              high: prov.high,
              low: prov.low,
              close: prov.close,
            });
          } else {
            candleSeriesRef.current.update({
              time: bucketTimeSec as unknown as Time,
              value: prov.close,
            });
          }
        }
      }

      // Handle durable closed bar committed events
      if (msg.type === "closed_bar_committed") {
        if (msg.instrument_id !== instId || msg.timeframe !== timeframe) return;

        const openTimeSec = Math.floor(msg.bar_open_ns / 1_000_000_000);
        const prov = provisionalCandleRef.current;
        
        if (!prov || prov.time !== openTimeSec) return;

        // Push to raw bars and recalculate indicators if any exist
        const lastBar = rawBarsRef.current[rawBarsRef.current.length - 1];
        if (lastBar && lastBar.time === openTimeSec) {
            rawBarsRef.current[rawBarsRef.current.length - 1] = {
                time: openTimeSec,
                open: prov.open,
                high: prov.high,
                low: prov.low,
                close: prov.close,
                volume: prov.volume,
            };
        } else {
            rawBarsRef.current.push({
                time: openTimeSec,
                open: prov.open,
                high: prov.high,
                low: prov.low,
                close: prov.close,
                volume: prov.volume,
            });
            if (rawBarsRef.current.length > 2000) rawBarsRef.current.shift(); // prevent unbounded memory
        }
        updateIndicators(rawBarsRef.current);
      }

      // Handle live signal candidates
      if (msg.type === "signal_candidate") {
        const sig = msg.data || msg;
        if (sig.instrument_id === instId && sig.timeframe === timeframe) {
          const currentMarkers = markersPluginRef.current?.markers() || [];
          const openNs = sig.bar_open_ns || (sig.time ? sig.time * 1_000_000_000 : Date.now() * 1_000_000);
          const newMarker: SeriesMarker<Time> = {
            time: Math.floor(openNs / 1_000_000_000) as unknown as Time,
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
          <TickerPriceCell price={currentPrice} />
          {priceChangePct !== null && (
            <span
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
          {/* On-Demand AI Debate Trigger */}
          <button
            onClick={handleTriggerDebate}
            disabled={triggeringDebate}
            className="flex items-center space-x-1.5 px-2 py-0.5 text-2xs font-medium rounded text-purple-400 hover:text-purple-200 bg-purple-950/20 hover:bg-purple-950/50 transition-colors border border-purple-800/40 hover:border-purple-600/60 focus-visible:ring-1 focus-visible:ring-purple-500 outline-none"
            title={`Trigger AI Consensus Debate on ${symbol}`}
            aria-label={`Trigger AI Consensus Debate on ${symbol}`}
          >
            <BrainCircuit className="w-3 h-3" />
            <span>{triggeringDebate ? "Queuing..." : "Run Debate"}</span>
          </button>

          {/* Chart Style Selector */}
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

          {/* Indicators Dialog */}
          <Dialog open={showIndDialog} onOpenChange={setShowIndDialog}>
            <DialogTrigger className="flex items-center space-x-1 px-2 py-0.5 text-2xs font-medium rounded text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60 transition-colors border border-transparent hover:border-zinc-700">
              <Settings2 className="w-3 h-3" />
              <span>Indicators</span>
            </DialogTrigger>
            <DialogContent className="bg-zinc-950 border border-zinc-800 text-zinc-200 max-w-sm">
              <DialogHeader>
                <DialogTitle>Chart Indicators</DialogTitle>
              </DialogHeader>
              <div className="space-y-4 pt-2">
                <div className="flex items-center space-x-2">
                  <select 
                    aria-label="Select indicator type"
                    className="bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1.5 text-sm text-zinc-100 focus-visible:ring-2 focus-visible:ring-cyan-500/50 focus:outline-none transition-all"
                    value={newIndType} onChange={e => setNewIndType(e.target.value as any)}
                  >
                    <option value="SMA">SMA</option>
                    <option value="EMA">EMA</option>
                    <option value="WMA">WMA</option>
                    <option value="WEMA">WEMA</option>
                    <option value="RSI">RSI</option>
                    <option value="ATR">ATR</option>
                    <option value="ROC">ROC</option>
                    <option value="WilliamsR">Williams %R</option>
                    <option value="TRIX">TRIX</option>
                    <option value="CCI">CCI</option>
                    <option value="ForceIndex">Force Index</option>
                    <option value="MFI">MFI</option>
                    <option value="SD">Standard Deviation</option>
                    <option value="AverageGain">Avg Gain</option>
                    <option value="AverageLoss">Avg Loss</option>
                  </select>
                  <input 
                    aria-label="Indicator period"
                    type="number" min="2" max="200"
                    className="bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1.5 text-sm text-zinc-100 w-20 focus-visible:ring-2 focus-visible:ring-cyan-500/50 focus:outline-none font-mono tabular-nums transition-all"
                    value={newIndPeriod} onChange={e => setNewIndPeriod(Number(e.target.value))}
                  />
                  <Button 
                    size="sm" variant="secondary" 
                    className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 border-0"
                    onClick={() => {
                      setIndicators([...indicators, {
                        id: crypto.randomUUID(),
                        type: newIndType,
                        period: newIndPeriod,
                        pane: ["SMA", "EMA", "WMA", "WEMA", "SD"].includes(newIndType) ? "main" : "bottom",
                        color: DEFAULT_COLORS[indicators.length % DEFAULT_COLORS.length]
                      }]);
                    }}
                  >
                    <Plus className="w-4 h-4 mr-1" /> Add
                  </Button>
                </div>
                
                <div className="space-y-2 mt-4">
                  {indicators.map(ind => (
                    <div key={ind.id} className="flex items-center justify-between bg-zinc-900/50 border border-zinc-800/50 rounded px-2 py-1.5">
                      <div className="flex items-center space-x-2">
                        <div className="w-3 h-3 rounded-full" style={{ backgroundColor: ind.color }} />
                        <span className="text-xs font-mono">{ind.type} ({ind.period})</span>
                      </div>
                      <button 
                        onClick={() => {
                          if (indicatorSeriesRef.current[ind.id] && chartRef.current) {
                            chartRef.current.removeSeries(indicatorSeriesRef.current[ind.id]);
                            delete indicatorSeriesRef.current[ind.id];
                          }
                          setIndicators(indicators.filter(i => i.id !== ind.id));
                        }}
                        className="text-zinc-500 hover:text-red-400"
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  ))}
                  {indicators.length === 0 && (
                    <div className="text-xs text-zinc-500 text-center py-2">No active indicators</div>
                  )}
                </div>
              </div>
            </DialogContent>
          </Dialog>
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
              className={`px-2 py-0.5 text-2xs font-medium rounded transition-colors ${
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
          className="text-2xs text-zinc-500 hover:text-cyan-400 transition-colors"
        >
          Market charts powered by TradingView Lightweight Charts™ (Apache-2.0)
        </a>
        <div className="flex items-center space-x-1.5">
          <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
          <span className="text-2xs font-mono text-zinc-500">LIVE FEED</span>
        </div>
      </div>
    </div>
  );
};
