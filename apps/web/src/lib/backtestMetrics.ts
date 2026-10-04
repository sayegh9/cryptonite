import { metricOrNull } from "@/lib/freshness";

/**
 * Backtest metric versions (R2.2-R2.4). Version 2 marks equity to market at every bar close and
 * adds a buy-and-hold benchmark, fees and turnover. A run without `metrics_version` was computed
 * the old way (equity only at fills) and is "legacy": its drawdown can be understated.
 */
export const CURRENT_METRICS_VERSION = 2;

export interface BenchmarkMetrics {
  total_return?: number | string | null;
  sharpe_ratio?: number | string | null;
  max_drawdown?: number | string | null;
  alpha_vs_hold?: number | string | null;
}

export interface VersionedMetrics {
  metrics_version?: number | string | null;
  benchmark?: BenchmarkMetrics | null;
  fees_paid?: number | string | null;
  turnover?: number | string | null;
}

/** The metrics version, or null for legacy runs (field missing). */
export function metricsVersion(m: VersionedMetrics | null | undefined): number | null {
  const v = m?.metrics_version;
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export const isLegacyMetrics = (m: VersionedMetrics | null | undefined) => metricsVersion(m) === null;

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
