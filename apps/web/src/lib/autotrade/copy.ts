/** Auto-Trade labels and error copy (docs/ux/auto-trade-tab.md). Display only: no trading rule lives here. */
import type { ApiError, ExecutionMode, Market, ProfileStatus } from "./types";

export const MODE_LABEL: Record<ExecutionMode, string> = { ASK: "Ask me", VETO: "Veto window", AUTO: "Fully automatic" };
export const MARKET_LABEL: Record<Market, string> = { SPOT: "Spot", SPOT_MARGIN: "Spot Margin", PERP: "Perpetuals" };
export const STATUS_LABEL: Record<ProfileStatus, string> = {
  ACTIVE: "Running",
  PAUSED: "Paused",
  HALTED: "Halted",
  STOPPING: "Stopping",
  STOPPED: "Stopped",
  ARCHIVED: "Archived",
};

/** Lifecycle actions the API accepts per status (profile_lifecycle). Archive also needs a flat profile when paused or halted. */
export const canStart = (s: ProfileStatus) => s === "STOPPED";
export const canResume = (s: ProfileStatus) => s === "PAUSED" || s === "HALTED" || s === "STOPPING";
export const canPause = (s: ProfileStatus) => s === "ACTIVE";
export const canStop = (s: ProfileStatus) => s === "ACTIVE" || s === "PAUSED" || s === "HALTED";
export const canArchive = (s: ProfileStatus, openPositions: number) =>
  s === "STOPPED" || ((s === "PAUSED" || s === "HALTED") && openPositions === 0);

const SCHEMA_CODES: Record<string, string> = {
  SHORT_NEEDS_MARGIN_OR_PERP: "Short profiles need Spot Margin or Perpetuals. Add one of these markets or choose Long.",
  LEVERAGE_NEEDS_MARGIN_OR_PERP: "Leverage above 1× needs Spot Margin or Perpetuals.",
  DAILY_LOSS_REQUIRED: "Set a daily loss limit before starting.",
  DAILY_LOSS_BELOW_RISK: "Daily loss limit must be at least risk per trade.",
  DAILY_LOSS_ABOVE_DRAWDOWN: "Daily loss limit cannot exceed maximum drawdown.",
  TRAIL_ABOVE_ACTIVATION: "Trail distance cannot exceed its activation threshold.",
  LIVE_NOT_AVAILABLE: "Live trading is not available yet. Choose Paper.",
  UNKNOWN_STRATEGY: "One of the strategies is not available any more. Pick from the list.",
  STRATEGY_DIRECTION_MISMATCH: "A ticked strategy trades a side this direction doesn't allow. Untick it or change the direction.",
  TIMEFRAME_UNSUPPORTED: "A ticked timeframe isn't run by any ticked strategy. Untick it or add a strategy that runs it.",
  NO_STRATEGIES: "Pick at least one strategy and one timeframe it runs on.",
  STRATEGY_NOT_IN_PROFILE: "The setup came from a strategy this profile no longer runs.",
};

/** Plain copy for an API refusal. The API's own text is shown when no mapping applies (it never carries secrets). */
export function errorCopy(err: ApiError, mode?: ExecutionMode): string {
  switch (err.code) {
    case "AUTOTRADE_DISABLED":
      return `The auto-trade switch is off, so this profile can't start in ${mode ? MODE_LABEL[mode] : "this mode"}. Choose Ask me, or start it after turning the switch on in Settings.`;
    case "PROFILE_INVALID":
      return err.message
        .split(",")
        .map((c) => SCHEMA_CODES[c.trim()] ?? c.trim())
        .join(" ");
    case "TOTP_REQUIRED":
    case "TOTP_REUSED":
      return "That code wasn't accepted. Check the current code in your authenticator app and try again.";
    case "TOTP_LOCKED":
      return "Too many wrong codes. Try again in 15 minutes.";
    case "COIN_LOCKED":
      return err.message.replace(/; pause or stop it first$/, ".");
    case "MANDATE_PAUSED":
    case "MANDATE_HALTED":
      return err.message.replace(/; resume or edit it instead$/, ". Resume or edit it instead.");
    default:
      if (err.code && SCHEMA_CODES[err.code]) return SCHEMA_CODES[err.code];
      if (err.message && err.message.split(",").every((c) => SCHEMA_CODES[c.trim()]))
        return err.message.split(",").map((c) => SCHEMA_CODES[c.trim()]).join(" ");
      if (err.status === 401 || err.status === 403) return "Sign in as an operator to manage profiles.";
      return err.message;
  }
}

export const isTotpError = (err: ApiError) => err.code === "TOTP_REQUIRED" || err.code === "TOTP_REUSED";

export const fmtUsd = (v: string | number | null | undefined) =>
  v == null || v === "" ? "Unavailable" : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
export const signedUsd = (v: string | number | null | undefined) => {
  if (v == null || v === "") return "Unavailable";
  const n = Number(v);
  return `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};
export const coinOf = (instrument: string) => instrument.split(".")[0].replace(/-(PERP|MARGIN)$/, "");

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
