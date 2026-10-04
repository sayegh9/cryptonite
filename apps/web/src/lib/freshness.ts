/**
 * Shared freshness rules: a value is only data if we know when it was observed.
 *
 * - live:    observed within `maxAgeMs`
 * - delayed: older than `maxAgeMs`, up to `DELAYED_FACTOR × maxAgeMs`
 * - stale:   older than that
 * - unknown: no value, or no (valid) observation time
 */
export type FreshState = "live" | "delayed" | "stale" | "unknown";

export const DELAYED_FACTOR = 3;

/** Placeholder rendered wherever a value is unknown. */
export const UNKNOWN_TEXT = "—";

export type AsOf = number | string | Date | null | undefined;

/** Epoch milliseconds of `asOf`, or null when absent/invalid. Numbers below 1e11 are taken as seconds. */
export function asOfMs(asOf: AsOf): number | null {
  if (asOf === null || asOf === undefined || asOf === "") return null;
  let ms: number;
  if (asOf instanceof Date) ms = asOf.getTime();
  else if (typeof asOf === "number") ms = asOf < 1e11 ? asOf * 1000 : asOf;
  else ms = Date.parse(asOf);
  return Number.isFinite(ms) && ms > 0 ? ms : null;
}

export function freshness(asOf: AsOf, maxAgeMs: number, now: number = Date.now()): FreshState {
  const ms = asOfMs(asOf);
  if (ms === null) return "unknown";
  const age = Math.max(0, now - ms);
  if (age <= maxAgeMs) return "live";
  if (age <= maxAgeMs * DELAYED_FACTOR) return "delayed";
  return "stale";
}

/** True when `v` is a usable number (not null/undefined/NaN/Infinity). */
export function isKnownNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/** Positive finite number or null: for quantities like equity where 0/negative means "not loaded". */
export function knownPositive(v: unknown): number | null {
  const n = typeof v === "string" ? Number(v) : v;
  return isKnownNumber(n) && n > 0 ? n : null;
}

/** Finite number from a number or decimal string, else null. */
export function metricOrNull(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function ageLabel(asOf: AsOf, now: number = Date.now()): string {
  const ms = asOfMs(asOf);
  if (ms === null) return "unknown";
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.round(m / 60)}h ago`;
}
