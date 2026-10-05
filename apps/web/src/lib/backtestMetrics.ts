import { metricOrNull } from "@/lib/freshness";

/**
 * Backtest metric versions (R2.2-R2.4). Version 2 marks equity to market at every bar close and
 * adds a buy-and-hold benchmark, fees and turnover. Version 3 actually charges the profile's fees
 * (v2 runs were fee-free whatever the profile said) and counts trades as round trips. A run below
 * the current version, or without `metrics_version`, is "legacy": re-run it for comparable numbers.
 */
export const CURRENT_METRICS_VERSION = 3;

export interface BenchmarkMetrics {
  total_return?: number | string | null;
  sharpe_ratio?: number | string | null;
  max_drawdown?: number | string | null;
  alpha_vs_hold?: number | string | null;
}

/** Trades per market regime at entry (F1, reporting only). */
export interface RegimeStats {
  trades?: number | string | null;
  win_rate?: number | string | null;
  total_pnl?: number | string | null;
}

export interface VersionedMetrics {
  metrics_version?: number | string | null;
  benchmark?: BenchmarkMetrics | null;
  fees_paid?: number | string | null;
  turnover?: number | string | null;
  /** Keyed by TREND_UP, TREND_DOWN, CHOP, HIGH_VOL, UNKNOWN; absent on runs from before F1. */
  regime_breakdown?: Record<string, RegimeStats> | null;
}

/** Display order of market regimes (F1); unknown keys sort last. */
export const REGIME_ORDER = ["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOL", "UNKNOWN"];

export const REGIME_LABEL: Record<string, string> = {
  TREND_UP: "Trend up",
  TREND_DOWN: "Trend down",
  CHOP: "Chop",
  HIGH_VOL: "High volatility",
  UNKNOWN: "Unknown (too little history)",
};

export function sortRegimes<T>(entries: [string, T][]): [string, T][] {
  const rank = (k: string) => (REGIME_ORDER.includes(k) ? REGIME_ORDER.indexOf(k) : REGIME_ORDER.length);
  return [...entries].sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]));
}

/** The metrics version, or null for legacy runs (field missing). */
export function metricsVersion(m: VersionedMetrics | null | undefined): number | null {
  const v = m?.metrics_version;
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export const isLegacyMetrics = (m: VersionedMetrics | null | undefined) => {
  const v = metricsVersion(m);
  return v === null || v < CURRENT_METRICS_VERSION;
};

/** Distinct versions among runs, "legacy" for runs without one; more than one = mixed. */
export function versionsOf(runs: { metrics?: VersionedMetrics | null }[]): string[] {
  const set = new Set(runs.map((r) => {
    const v = metricsVersion(r.metrics);
    return v === null ? "legacy" : `v${v}`;
  }));
  return Array.from(set).sort();
}

/** Number from a metric field (the API may send decimals as strings), or null when absent. */
export const metricNum = metricOrNull;

export const fmtSignedPct = (v: number | null, digits = 2) =>
  v === null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;
