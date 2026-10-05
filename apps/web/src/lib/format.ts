/**
 * Shared display formatting for units the server sends raw (G4): quantities (often Decimal strings
 * such as "0.009399821403393336" or "0E-18"), values already in percent units, ratios that can be
 * infinite, and the sample size a calibration chart needs before it is worth reading.
 * Dependency-free so e2e/format.spec.ts can unit-test it without a browser.
 */

const toNum = (v: unknown): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(String(v).trim());
  return Number.isNaN(n) ? null : n;
};

/**
 * Decimal places of an instrument step size: "0.00001000" -> 5, "1" -> 0, "1E-8" -> 8, 0.001 -> 3.
 * null when the step is missing, zero or not a number.
 */
export function stepDecimals(step: unknown): number | null {
  const n = toNum(step);
  if (n === null || !Number.isFinite(n) || n <= 0) return null;
  // Normalise through exponent notation so "1E-8" and "0.00000001" agree; cap at 12 places
  const [mantissa, exp] = n.toExponential().split("e");
  const mantissaDecimals = (mantissa.split(".")[1] ?? "").length;
  return Math.min(12, Math.max(0, mantissaDecimals - Number(exp)));
}

/** True when a quantity is zero once rounded to its step (or to 1e-12 without one). "0E-18" is flat. */
export function isZeroQuantity(v: unknown, step?: unknown): boolean {
  const n = toNum(v);
  if (n === null) return false;
  const s = toNum(step);
  const eps = s !== null && Number.isFinite(s) && s > 0 ? s / 2 : 1e-12;
  return Math.abs(n) < eps;
}

/**
 * A quantity for display. With a step size: fixed to the step's decimals ("0.00940" for step 0.00001).
 * Without one: 4 significant digits below 1 ("0.009400" -> "0.0094"), up to 4 decimals from 1 up.
 * Zero after rounding prints "0", never "0E-18" or "-0"; a non-number prints "—".
 */
export function fmtQuantity(v: unknown, step?: unknown): string {
  const n = toNum(v);
  if (n === null || !Number.isFinite(n)) return "—";
  if (isZeroQuantity(n, step)) return "0";
  const d = stepDecimals(step);
  if (d !== null) {
    return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  if (Math.abs(n) < 1) {
    return n.toLocaleString("en-US", { maximumSignificantDigits: 4 });
  }
  return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

/**
 * A value that is already in percent units (the server's `*_pct` fields): "0.1933" -> "0.19%".
 * Non-numbers (e.g. "not measured") pass through unchanged; null prints "—".
 */
export function fmtPercentUnits(v: unknown, digits = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = toNum(v);
  if (n === null || !Number.isFinite(n)) return String(v);
  return `${n.toFixed(digits)}%`;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * A Sortino ratio. With no closed trades it is not defined, whatever the server computed from equity
 * ("n/a (0 closed trades)"); an infinite one means no downside yet, which is not a measurement either.
 * Non-numeric strings such as "not measured" pass through.
 */
export function fmtSortino(v: unknown, closedTrades?: number | null): string {
  if (closedTrades === 0) return `n/a (${plural(0, "closed trade")})`;
  if (v === null || v === undefined || v === "") return "not measured";
  const s = String(v).trim().toLowerCase();
  if (s === "inf" || s === "+inf" || s === "-inf" || s === "infinity" || s === "-infinity") return "n/a (no downside yet)";
  const n = toNum(v);
  if (n === null) return String(v);
  if (!Number.isFinite(n)) return "n/a (no downside yet)";
  return n.toFixed(2);
}

/**
 * The gate's "Sortino vs BTC hold" actual, "<sortino> vs <benchmark>" as the server writes it
 * (`f"{_fmt(m.sortino)} vs {_fmt(m.benchmark_sortino)}"`), with each side through `fmtSortino`.
 * The benchmark is BTC buy-and-hold, so it does not depend on the mandate's closed trades.
 */
export function fmtSortinoVsHold(actual: string, closedTrades?: number | null): string {
  const parts = actual.split(" vs ");
  if (parts.length !== 2) return closedTrades === 0 ? fmtSortino(null, 0) : actual;
  return `${fmtSortino(parts[0], closedTrades)} vs ${fmtSortino(parts[1])}`;
}

/** Fewer labelled points than this and a reliability chart is noise: it is greyed out with a note. */
export const CALIBRATION_MIN_POINTS = 20;

export const calibrationReadable = (n: number | null | undefined) => (n ?? 0) >= CALIBRATION_MIN_POINTS;

/**
 * A promotion-gate check's "Now" value with units (G4). The server writes some actuals raw
 * (`max_drawdown_pct` "0.1933", `sortino_vs_btc_hold` "inf vs not measured"); `metrics` is the gate's
 * metrics object, which carries `closed_trades`.
 */
export function fmtGateActual(name: string, actual: string, metrics?: Record<string, unknown> | null): string {
  switch (name) {
    case "max_drawdown_pct":
    case "veto_rate_pct":
      return fmtPercentUnits(actual);
    case "sortino_vs_btc_hold":
      return fmtSortinoVsHold(actual, toNum(metrics?.closed_trades));
    default:
      return actual;
  }
}
