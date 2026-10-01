/** Mission Control contracts (blueprint §4.4–§4.6, §7.1): the shapes /api/v1/ai/* returns. */

export type MandateStatus = "ACTIVE" | "HALTED" | "PAUSED" | "ARCHIVED";
export type ActionClass = "ADD_RISK" | "REDUCE_RISK";
export type ApprovalStatus = "PENDING" | "APPROVED" | "VETOED" | "EXPIRED" | "EXPIRED_PROCEEDED" | "CANCELLED";

export interface MandatePosition {
  instrument_id: string;
  side: string;
  quantity: string;
  entry_price: string;
  current_price: string;
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
}

export interface AiStatus {
  kill_switch: { latched: boolean; reason: string | null; latched_by: string | null };
  halted_mandates: { id: string; name: string; reason: string | null; halted_at: string | null }[];
  account: { equity: string | null; hwm: string | null; drawdown_pct: string | null; backstop_pct: string } | null;
  exit_monitor_up: boolean;
  notifier_up: boolean;
  budget_alert: boolean;
  degraded: string[];
}

export interface GateCheck {
  name: string;
  required: string;
  actual: string;
  passed: boolean;
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
