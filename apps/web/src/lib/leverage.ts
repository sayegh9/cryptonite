/**
 * Leverage display rules shared by the Brain perp card and the manual perp ticket (leverage plan 4.3, 4.5, 4.9).
 * The numbers here only decide what is shown; the server plans and enforces every limit.
 */

/** The pills offered; the server's recommended leverage is added when it is not one of them */
export const LEVERAGE_PILLS = [1, 2, 3, 5, 7, 10] as const;
/** The account maximum (ADR 0009 D2) */
export const MAX_LEVERAGE = 10;
/** At or above this, approve asks for a second confirmation (leverage plan 4.8) */
export const HIGH_LEVERAGE_CONFIRM = 8;

export type LeverageTier = "low" | "mid" | "high";

/** 1–3x low, 4–7x mid, 8–10x high */
export function leverageTier(leverage: number): LeverageTier {
  if (leverage >= HIGH_LEVERAGE_CONFIRM) return "high";
  if (leverage > 3) return "mid";
  return "low";
}

/** Text, border and tint classes of a tier, from the leverage colour tokens in globals.css (light and dark) */
export const TIER_CLASSES: Record<LeverageTier, string> = {
  low: "text-leverage-low border-leverage-low/70 bg-leverage-low/10",
  mid: "text-leverage-mid border-leverage-mid/70 bg-leverage-mid/10",
  high: "text-leverage-high border-leverage-high/70 bg-leverage-high/15",
};

/** The pills to show: the fixed set, plus the recommended leverage when it is not one of them */
export function pillsWith(recommended: number | null | undefined): number[] {
  const set = new Set<number>(LEVERAGE_PILLS);
  if (recommended && recommended >= 1 && recommended <= MAX_LEVERAGE) set.add(recommended);
  return Array.from(set).sort((a, b) => a - b);
}

/** "$1,750.00" from a server decimal string; "—" when missing */
export function usd(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

/** A server fraction-or-percent string shown with a fixed number of decimals */
export function pct(value: string | number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toFixed(digits)}%` : "—";
}
