/** Display helpers for the Setups inbox. */
export function formatAge(seconds: number): string {
  if (seconds < 90) return `${seconds}s`;
  if (seconds < 5_400) return `${Math.round(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  return `${h}h ${Math.round((seconds % 3600) / 60)}m`;
}

export function formatPrice(value: string | null): string {
  if (value === null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
  return n.toLocaleString("en-US", { maximumFractionDigits: n >= 100 ? 2 : 6 });
}

export const symbolOf = (instrumentId: string) => instrumentId.split(".")[0];

const REASONS: Record<string, string> = {
  NO_OWNER: "No profile owns this coin",
  PRESET_NOT_RUN: "The owner does not run this preset",
  TIMEFRAME_NOT_RUN: "The owner does not scan this timeframe",
};

export function reasonText(reason: string | null): string {
  if (!reason) return "Eligible";
  if (REASONS[reason]) return REASONS[reason];
  if (reason.startsWith("PROFILE_")) return `The owning profile is ${reason.slice(8).toLowerCase()}`;
  return reason;
}
