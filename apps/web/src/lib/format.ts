/**
 * Shared display formatting for units the server sends raw (G4): quantities (often Decimal strings
 * such as "0.009399821403393336" or "0E-18"), values already in percent units, ratios that can be
 * infinite, and the sample size a calibration chart needs before it is worth reading.
 * Dependency-free so e2e/format.spec.ts can unit-test it without a browser.
 */

/** A number, or null when missing, blank (" " is unknown, not 0) or not numeric. */
const toNum = (v: unknown): number | null => {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return Number.isNaN(v) ? null : v;
  const s = String(v).trim();
  if (s === "") return null;
  const n = Number(s);
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

/** Below this a quantity is zero: Decimal noise such as "0E-18". Independent of any step size. */
const ZERO_EPS = 1e-12;

/**
 * True when a quantity is zero ("0E-18", 0). Missing, blank or non-numeric values are NOT zero:
 * callers must not hide a row they cannot read. Never depends on the step size, so a dust residual
 * below one step still counts as held.
 */
export function isZeroQuantity(v: unknown): boolean {
  const n = toNum(v);
  return n !== null && Math.abs(n) < ZERO_EPS;
}

/** Rounding toward zero (ES2023 Intl): a displayed quantity is never larger than the real one. */
const trunc = (opts: Intl.NumberFormatOptions): Intl.NumberFormatOptions => ({ ...opts, roundingMode: "trunc" });

/** "<0.00001" for a non-zero quantity smaller than the smallest displayable unit (">-0.00001" if negative). */
const belowUnit = (n: number, decimals: number) => {
  const unit = (10 ** -decimals).toLocaleString("en-US", { maximumFractionDigits: decimals });
  return n < 0 ? `>-${unit}` : `<${unit}`;
};

/**
 * A quantity for display, always truncated toward zero so it never overstates the position.
 * - With a step size: fixed to the step's decimals ("0.00939" for 0.0093998 at step 0.00001).
 * - With `maxFractionDigits` (records such as the ledger): up to that many decimals.
 * - Otherwise: 4 significant digits below 1 ("0.0093998" -> "0.009399"), up to 4 decimals from 1 up.
 * A non-zero value below one displayable unit prints "<unit" (e.g. "<0.00001"), never "0".
 * Zero prints "0" (never "0E-18" or "-0"); a missing or non-numeric value prints "—".
 */
export function fmtQuantity(v: unknown, step?: unknown, opts: { maxFractionDigits?: number } = {}): string {
  const n = toNum(v);
  if (n === null || !Number.isFinite(n)) return "—";
  if (isZeroQuantity(n)) return "0";
  const d = stepDecimals(step) ?? opts.maxFractionDigits ?? null;
  if (d !== null) {
    if (Math.abs(n) < 10 ** -d) return belowUnit(n, d);
    const fixed = stepDecimals(step) !== null;
    return n.toLocaleString("en-US", trunc({ minimumFractionDigits: fixed ? d : 0, maximumFractionDigits: d }));
  }
  if (Math.abs(n) < 1) return n.toLocaleString("en-US", trunc({ maximumSignificantDigits: 4 }));
  return n.toLocaleString("en-US", trunc({ maximumFractionDigits: 4 }));
}

/**
 * A value that is already in percent units (the server's `*_pct` fields): "0.1933" -> "0.19%".
 * Non-numbers (e.g. "not measured") pass through unchanged; null or blank prints "—"; never "-0.00%".
 */
export function fmtPercentUnits(v: unknown, digits = 2): string {
  const n = toNum(v);
  if (n === null) return v === null || v === undefined || String(v).trim() === "" ? "—" : String(v);
  if (!Number.isFinite(n)) return String(v);
  const s = n.toFixed(digits);
  return `${Number(s) === 0 ? s.replace(/^-/, "") : s}%`;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * A Sortino ratio. With no closed trades it is not defined, whatever the server computed from equity
 * ("n/a (0 closed trades)"). +inf means no downside yet; -inf or NaN is "n/a (not finite)". Neither is
 * a measurement. Non-numeric strings such as "not measured" pass through; missing or blank is
 * "not measured".
 */
export function fmtSortino(v: unknown, closedTrades?: number | null): string {
  if (closedTrades === 0) return `n/a (${plural(0, "closed trade")})`;
  if (v === null || v === undefined || String(v).trim() === "") return "not measured";
  const s = String(v).trim().toLowerCase();
  if (s === "inf" || s === "+inf" || s === "infinity" || s === "+infinity") return "n/a (no downside yet)";
  if (s === "-inf" || s === "-infinity" || s === "nan") return "n/a (not finite)";
  const n = toNum(v);
  if (n === null) return String(v);
  if (n === Infinity) return "n/a (no downside yet)";
  if (!Number.isFinite(n)) return "n/a (not finite)";
  const out = n.toFixed(2);
  return Number(out) === 0 ? out.replace(/^-/, "") : out;
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

/** Points a reliability chart needs: at least 20, and never fewer than the server's ECE `min_samples`. */
export const calibrationThreshold = (minSamples?: number | null) => Math.max(CALIBRATION_MIN_POINTS, minSamples ?? 0);

export const calibrationReadable = (n: number | null | undefined, minSamples?: number | null) =>
  (n ?? 0) >= calibrationThreshold(minSamples);

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
