import { useEffect, useState } from "react";
import { fetchApi } from "@/lib/api";

/**
 * GET /api/v1/control/enforced-limits: the limits the orchestrator's dispatcher actually runs with,
 * as it publishes them to Redis (`risk:effective_limits`). `paper: null` = not published yet (unknown).
 * Values are decimal strings.
 */
export interface EnforcedPaperLimits {
  max_daily_loss_pct: string;
  min_order_equity_pct: string;
  max_order_equity_pct: string;
  taker_fee_bps: string;
  account_backstop_drawdown_pct: string;
  /** F4 cap on the combined beta-weighted BTC exposure of all mandates (absent from older orchestrators). */
  cross_mandate_exposure_pct?: string;
}

export interface EnforcedLimits {
  as_of: string | null;
  paper: EnforcedPaperLimits | null;
  effective_version?: number;
  enforced_at?: string | null;
}

/** The orchestrator republishes every 60 s; older than this the values are "delayed". */
export const LIMITS_MAX_AGE_MS = 120_000;

export type LimitsState =
  | { status: "loading"; data: null; error: null }
  | { status: "ok"; data: EnforcedLimits; error: null }
  | { status: "error"; data: null; error: string };

export async function fetchEnforcedLimits(): Promise<EnforcedLimits> {
  const raw = await fetchApi("/control/enforced-limits");
  const paper = raw && typeof raw.paper === "object" && raw.paper !== null ? (raw.paper as EnforcedPaperLimits) : null;
  return { as_of: typeof raw?.as_of === "string" ? raw.as_of : null, paper, effective_version: typeof raw?.effective_version === "number" ? raw.effective_version : 0, enforced_at: typeof raw?.enforced_at === "string" ? raw.enforced_at : null };
}

/**
 * True only when the enforced telemetry is trustworthy enough to claim "enforced": paper values
 * published, as_of within LIMITS_MAX_AGE_MS (not in the future), enforced_at a real parseable
 * timestamp that does not postdate the snapshot that reports it. Anything stale, missing, or
 * internally inconsistent is treated as unavailable (fail closed).
 */
export function isEnforcedLimitsValid(state: EnforcedLimits): boolean {
  if (!state || state.paper === null) return false;
  const asOfMs = typeof state.as_of === "string" ? Date.parse(state.as_of) : NaN;
  const enforcedAtMs = typeof state.enforced_at === "string" ? Date.parse(state.enforced_at) : NaN;
  if (!Number.isFinite(asOfMs) || !Number.isFinite(enforcedAtMs)) return false;
  const now = Date.now();
  if (asOfMs > now || now - asOfMs > LIMITS_MAX_AGE_MS) return false;
  if (enforcedAtMs > asOfMs) return false; // enforced_at must not postdate the reporting snapshot
  return true;
}

export function useEnforcedLimits(pollMs = 60_000): LimitsState {
  const [state, setState] = useState<LimitsState>({ status: "loading", data: null, error: null });
  useEffect(() => {
    let active = true;
    const load = () =>
      fetchEnforcedLimits()
        .then((data) => active && setState({ status: "ok", data, error: null }))
        .catch((err: { status?: number; message?: string }) =>
          active && setState({ status: "error", data: null, error: err?.status ? `HTTP ${err.status}` : err?.message || "unreachable" }));
    void load();
    const id = setInterval(load, pollMs);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [pollMs]);
  return state;
}

/** A limit as a fraction ("5" -> 0.05), or null when unknown/unparseable. */
export function pctFraction(v: string | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n / 100 : null;
}

/**
 * F7: GET/POST /api/v1/control/risk-limits. Operator overrides that can only tighten the code defaults;
 * loosening (a higher value, or clearing an override) needs the operator's TOTP. Percent strings; null =
 * no override (the default applies).
 */
export const CONFIGURABLE_LIMITS = ["max_daily_loss_pct", "max_order_equity_pct", "cross_mandate_exposure_pct"] as const;
export type ConfigurableLimit = (typeof CONFIGURABLE_LIMITS)[number];
export type LimitValues = Record<ConfigurableLimit, string | null>;

export interface RiskLimitsConfig {
  environment: string;
  account_id: string;
  defaults: Record<ConfigurableLimit, string>;
  overrides: LimitValues;
  effective: Record<ConfigurableLimit, string>;
  min_order_equity_pct: string;
  updated_by: string | null;
  updated_at: string | null;
  reason: string | null;
  override_version?: number;
}

export interface RiskLimitsUpdate extends LimitValues {
  environment: string;
  account_id: string;
  reason: string;
  totp_code?: string;
}

const csrf = () => (typeof window !== "undefined" ? localStorage.getItem("csrf_token") || "" : "");

export const fetchRiskLimits = (environment = "PAPER", accountId = "PAPER-ACCOUNT-001"): Promise<RiskLimitsConfig> =>
  fetchApi(`/control/risk-limits?environment=${encodeURIComponent(environment)}&account_id=${encodeURIComponent(accountId)}`);

export const updateRiskLimits = (body: RiskLimitsUpdate): Promise<RiskLimitsConfig & { loosened: string[]; tightened: string[]; applies_within_s: number }> =>
  fetchApi("/control/risk-limits", { method: "POST", headers: { "X-CSRF-Token": csrf() }, body: JSON.stringify(body) });

/** "" (cleared) -> the default; otherwise the number typed. NaN when not a number. */
export function proposedValue(input: string, defaultValue: string): number {
  return input.trim() === "" ? Number(defaultValue) : Number(input);
}

/** Mirrors the server's rule: a field loosens when its new effective value is above the current one. */
export function loosenedLimits(config: RiskLimitsConfig, inputs: Record<ConfigurableLimit, string>): ConfigurableLimit[] {
  return CONFIGURABLE_LIMITS.filter((k) => {
    const next = proposedValue(inputs[k], config.defaults[k]);
    return Number.isFinite(next) && next > Number(config.effective[k]);
  });
}
