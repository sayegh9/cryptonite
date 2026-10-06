/** Mission Control contracts (blueprint §4.4–§4.6, §7.1): the shapes /api/v1/ai/* returns. */
import type { SimilarDecision } from "@/components/ApprovalRiskContext";

export type MandateStatus = "ACTIVE" | "HALTED" | "PAUSED" | "ARCHIVED";
export type ActionClass = "ADD_RISK" | "REDUCE_RISK";
export type ApprovalStatus = "PENDING" | "APPROVED" | "VETOED" | "EXPIRED" | "EXPIRED_PROCEEDED" | "CANCELLED";

export interface MandatePosition {
  instrument_id: string;
  side: string;
  quantity: string;
  entry_price: string;
  current_price: string;
  /** B3: PnL at the live mark (or at the stored mark when mark_stale). */
  unrealized_pnl?: string;
  /** B3: "live" = the orchestrator's live quote; "stored" = the last mark written, flagged stale. */
  mark_source?: "live" | "stored";
  mark_stale?: boolean;
  /** Seconds since the quote (live) or since the mark was last confirmed (stored); null = never marked. */
  mark_age_s?: number | null;
}

export interface Mandate {
  id: string;
  name: string;
  capital_budget_usd: string;
  instruments: string[];
  max_position_pct: string;
  max_drawdown_pct: string;
  allow_perps: boolean;
  max_leverage: number;
  strategies: string[];
  autonomy_level: 1 | 2 | 3;
  level_name: string;
  status: MandateStatus;
  active_hours_utc: string | null;
  veto_window_s: number;
  halted_reason: string | null;
  halted_at: string | null;
  level_since: string;
  equity: string;
  hwm: string;
  drawdown_pct: string;
  exposure_usd: string;
  budget_used_pct: string;
  positions: MandatePosition[];
  /** B3: true when any position is valued at a stored (not live) mark; exposure is then approximate. */
  marks_stale?: boolean;
  marks_age_s?: number | null;
  pending_approvals: number;
  pending_proposal: { id: number; to_level: number; created_at: string } | null;
}

export interface Approval {
  id: string;
  mandate_id: string;
  mandate: string | null;
  action_class: ActionClass;
  command_type: string;
  summary: string;
  source: string;
  level: number;
  timeout_policy: "DENY" | "PROCEED";
  status: ApprovalStatus;
  expires_at: string;
  seconds_left: number;
  decided_via: string | null;
  command_id: string | null;
  trace_id: string | null;
  /** The command the approval would queue (instrument_id, side, quantity, price, allocation_pct, …) */
  payload?: Record<string, unknown> | null;
  /** Up to 3 past decisions on the same instrument and side (read-only; omitted when unavailable) */
  similar_decisions?: SimilarDecision[] | null;
}

export interface AiStatus {
  kill_switch: { latched: boolean; reason: string | null; latched_by: string | null };
  halted_mandates: { id: string; name: string; reason: string | null; halted_at: string | null }[];
  account: { equity: string | null; hwm: string | null; drawdown_pct: string | null; backstop_pct: string } | null;
  exit_monitor_up: boolean;
  notifier_up: boolean;
  budget_alert: boolean;
  degraded: string[];
  /** null until a gateway has probed the local model tier */
  local_tier?: { healthy: boolean; models: string[]; error: string | null; checked_at: string | null } | null;
  /** Not yet reported by /ai/status (R6.3/R7.5): absent = unknown */
  learner_up?: boolean | null;
  /** Unix seconds of the debate worker's last successful get_news_sentiment (`sentiment:last_ok`, N19) */
  sentiment_last_ok?: number | string | null;
}

export interface GateCheck {
  name: string;
  required: string;
  actual: string;
  passed: boolean;
  /** Reported but not required: the gate's `passed` ignores it (R4.7, D6). Missing = required. */
  informational?: boolean;
  /** 90% interval [lo, hi] of the measured value, as strings (R4.2). */
  interval?: [string, string] | null;
}

export interface AutonomyView {
  mandate_id: string;
  name: string;
  status: MandateStatus;
  level: number;
  level_name: string;
  level_since: string;
  requested_level: number | null;
  proposal: { id: number; from_level: number; to_level: number; created_at: string } | null;
  gate: { level: number; target_level: number | null; passed: boolean; checks: GateCheck[]; metrics: Record<string, unknown> };
  events: { id: number; kind: string; from_level: number | null; to_level: number | null; reason: string; actor: string; created_at: string }[];
}

/** Confidence calibration of one role (A7.2): bins of stated confidence vs hit rate, ECE in pp. */
export interface CalibrationBin {
  lo: number;
  hi: number;
  n: number;
  mean_confidence: number | null;
  hit_rate: number | null;
}

/** Isotonic map stated confidence -> hit rate (F5): fitted from `min_points` labelled decisions on.
 * Informational only: never used for sizing, routing or a gate. */
export interface IsotonicFit {
  fitted: boolean;
  n: number;
  min_points?: number;
  /** Step function as [[x, y], ...] (each step's first and last x), sorted by x */
  points: [number, number][];
}

export interface Reliability {
  n: number;
  min_samples: number;
  ece_pp: number | null;
  bins: CalibrationBin[];
  /** F5; absent from servers before it */
  isotonic?: IsotonicFit | null;
}

/** Labelled decisions per market regime at decision time (F1, reporting only). */
export interface RegimeOutcome {
  regime: string;
  n: number;
  wins: number;
  losses: number;
  neutral: number;
  mean_signed_return: number | null;
}

export interface OutcomesByRegime {
  horizon: string;
  mandate_id: string | null;
  available?: boolean;
  regimes: RegimeOutcome[];
}

/** F2 shadow mode: a route with a shadow model (read-only view of ai_model_routes as the gateway resolves it). */
export interface ShadowRoute {
  role: string;
  primary_model: string;
  fallback_model: string | null;
  fallback_policy: string;
  priority: string;
  shadow_model: string;
  shadow_sample_rate: number;
  shadow_paid: boolean;
  active: boolean;
  updated_at: string | null;
}

export interface ShadowCounts { wins: number; losses: number; neutral: number }

export interface ShadowStats {
  role: string;
  shadow_model?: string;
  primary_models?: string[];
  n: number;
  ok: number;
  errors: number;
  skipped: number;
  skip_reasons: Record<string, number>;
  key_field: string | null;
  agreement: { n: number; agree: number; rate: number | null };
  latency_ms: { primary_p50: number | null; primary_p95: number | null; shadow_p50: number | null; shadow_p95: number | null };
  cost_usd: { shadow: string };
  outcomes:
    | { horizon: string; labelled: number; primary?: ShadowCounts; shadow?: ShadowCounts; agreement_rate?: number | null;
        debated?: ShadowCounts; shadow_would_skip?: ShadowCounts & { n: number } }
    | null;
}

export interface ShadowReport {
  available: boolean;
  days: number;
  role: string | null;
  allow_paid: boolean;
  routes: ShadowRoute[];
  models: ShadowStats[];
  roles: ShadowStats[];
}

export interface EvalRun {
  id: string;
  set_version: string;
  model: string;
  passed: boolean | null;
  metrics: Record<string, unknown>;
  created_at: string;
}

export interface MandateDraft {
  name: string;
  capital_budget_usd: string;
  instruments: string;
  max_position_pct: string;
  max_drawdown_pct: string;
  max_leverage: string;
  active_hours_utc: string;
  veto_window_s: string;
}
