import type { Time } from "lightweight-charts";

/** Pure bar helpers for the chart (no chart-library runtime imports, so they unit-test in node). */

export type ChartStyle = "candlestick" | "bar" | "line" | "area" | "baseline";

export const CHART_STYLES: { id: ChartStyle; label: string }[] = [
  { id: "candlestick", label: "Candles" },
  { id: "bar", label: "Bars" },
  { id: "line", label: "Line" },
  { id: "area", label: "Area" },
  { id: "baseline", label: "Baseline" },
];

export const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"];

const BUCKET_SECONDS: Record<string, number> = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 };

/** Bar length in seconds for a timeframe (1m when unknown). */
export const bucketSecondsOf = (timeframe: string) => BUCKET_SECONDS[timeframe] ?? 60;

export const isOhlcStyle = (style: ChartStyle) => style === "candlestick" || style === "bar";

export const UP_VOLUME = "rgba(16, 185, 129, 0.25)";
export const DOWN_VOLUME = "rgba(239, 68, 68, 0.25)";

/** Bars (API rows) as series data for a chart style: OHLC for candles/bars, close values otherwise. */
export function formatSeriesData(rawBars: any[], style: ChartStyle) {
  if (isOhlcStyle(style)) {
    return rawBars.map((b: any) => ({
      time: b.time as Time,
      open: Number(b.open),
      high: Number(b.high),
      low: Number(b.low),
      close: Number(b.close),
    }));
  }
  return rawBars.map((b: any) => ({ time: b.time as Time, value: Number(b.close) }));
}

export interface ProvisionalBar {
  time: number; // bar open, unix seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/**
 * Fold a 1m open-kline tick into the provisional bar of `timeframe`: 1m replaces the bar, higher
 * timeframes aggregate ticks into their bucket (a new bucket starts a new bar).
 */
export function nextProvisional(
  prev: ProvisionalBar | null,
  tick: { bar_open_ns: number; open: unknown; high: unknown; low: unknown; close: unknown; volume: unknown },
  timeframe: string,
): ProvisionalBar {
  const openSec = Math.floor(tick.bar_open_ns / 1_000_000_000);
  const bar = { open: Number(tick.open), high: Number(tick.high), low: Number(tick.low), close: Number(tick.close), volume: Number(tick.volume) };
  if (timeframe === "1m") return { time: openSec, ...bar };
  const size = bucketSecondsOf(timeframe);
  const bucket = Math.floor(openSec / size) * size;
  if (!prev || prev.time !== bucket) return { time: bucket, ...bar };
  return {
    ...prev,
    high: Math.max(prev.high, bar.high),
    low: Math.min(prev.low, bar.low),
    close: bar.close,
    volume: prev.volume + bar.volume,
  };
}

/** One live point for the main series in the current style. */
export function seriesPoint(time: number, bar: { open: number; high: number; low: number; close: number }, style: ChartStyle) {
  return isOhlcStyle(style)
    ? { time: time as unknown as Time, open: bar.open, high: bar.high, low: bar.low, close: bar.close }
    : { time: time as unknown as Time, value: bar.close };
}
