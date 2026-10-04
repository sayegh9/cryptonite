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
}

export interface EnforcedLimits {
  as_of: string | null;
  paper: EnforcedPaperLimits | null;
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
  return { as_of: typeof raw?.as_of === "string" ? raw.as_of : null, paper };
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
