/** Auto-Trade contracts: the profile view of /api/v1/ai/mandates/* and the P2 reads of /api/v1/ai/profiles/*. */
import type { Mandate } from "@/lib/mission/types";

export type Market = "SPOT" | "SPOT_MARGIN" | "PERP";
export type Direction = "LONG" | "SHORT" | "BOTH";
export type Style = "SWING" | "INTRADAY";
export type NotifyLevel = "ALL" | "IMPORTANT" | "NONE";
export type ExecutionMode = "ASK" | "VETO" | "AUTO";
export type ProfileStatus = "ACTIVE" | "PAUSED" | "HALTED" | "STOPPING" | "STOPPED" | "ARCHIVED";

/** The mandate view with the ADR 0011 profile fields the API already returns. */
export interface Profile extends Omit<Mandate, "status"> {
  status: ProfileStatus;
  execution_mode: ExecutionMode;
  markets: Market[];
  direction: Direction;
  style: Style;
  environment: "PAPER" | "LIVE";
  max_open_positions: number;
  max_debates_per_day: number;
  exit_tp_r: string | null;
  exit_tp1_scale_pct: string | null;
  exit_trail_activate_pct: string | null;
  exit_trail_pct: string | null;
  exit_max_hold_hours: number | null;
  dry_run: boolean;
  notify_level: NotifyLevel;
  breaker_max_loss_streak: number;
  breaker_max_reject_streak: number;
  paused_reason?: string | null;
  created_at?: string;
  updated_at?: string;
}

/** POST /ai/mandates answers 201 with the view plus whether the start went through. */
export interface CreatedProfile extends Profile {
  started: boolean;
  start_error: { code: string; detail: string } | null;
}

export interface ProfileDecision {
  id: number;
  mandate_id: string;
  ts: string;
  instrument_id: string | null;
  stage: string;
  outcome: string;
  reason: string | null;
  detail: Record<string, unknown>;
  run_id: string | null;
  command_id: string | null;
}

export interface DecisionPage {
  profile_id: string;
  decisions: ProfileDecision[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface ProfileSummary {
  profile_id: string;
  status: ProfileStatus;
  today: {
    date_utc: string;
    realized_pnl: string;
    unrealized_pnl: string;
    pnl: string;
    session_pnl: string;
    daily_loss_limit_usd: string | null;
    daily_profit_target_usd: string | null;
  };
  open_positions_count: number;
  last_decision: ProfileDecision | null;
  last_decision_at: string | null;
  last_decision_age_seconds: number | null;
  breaker: { active: boolean; state: "ACTIVE" | "CLEAR"; reason: string | null };
  dry_run: boolean;
  mode: { level: number; label: string };
  master_switch: "ON" | "OFF";
  health_score: {
    score: number | null;
    running: boolean;
    components: {
      freshness: { points: number; fresh_instruments: number; instruments: { instrument_id: string; fresh: boolean; age_seconds: number | null }[] };
      recent_failures: { points: number; failed_debates_24h: number };
      breaker: { points: number; active: boolean; reason: string | null };
    };
  };
}

export interface ProfilePerformance {
  profile_id: string;
  range: PerformanceRange;
  equity_curve: { ts: string; equity: string | null; hwm: string | null; drawdown_pct: string | null }[];
  equity_curve_sampled: boolean;
  fills: {
    trade_id: string;
    instrument_id: string;
    side: string;
    price: string | null;
    quantity: string | null;
    fee: string | null;
    fee_currency: string;
    executed_at: string | null;
  }[];
  exit_outcomes: {
    decision_id: number;
    instrument_id: string | null;
    realized_pnl: string | null;
    r_multiple: string | null;
    timestamp: string | null;
    outcome: string;
  }[];
  stats: {
    audited_exit_count: number;
    audited_wins: number;
    audited_win_rate: string | null;
    avg_r: string | null;
    r_known: number;
    r_unknown: number;
    outcome_coverage: { scope: string; complete: boolean; profile_fill_count: number; audited_exit_count: number; note: string };
    fees: { total_amount: string | null; by_currency: Record<string, string> };
    funding: string | null;
    margin_interest_by_asset: Record<string, string>;
    max_drawdown_pct: string | null;
  };
}

export type PerformanceRange = "7d" | "30d" | "all";
export type PresetQuality = "thin" | "negative" | "ok";

export interface PresetStatRow {
  preset: string;
  coin: string;
  trades: number;
  win_rate: number | null;
  avg_r: number | null;
  median_r?: number | null;
  max_drawdown_r?: number | null;
  first_bar?: string;
  last_bar?: string;
  quality: PresetQuality;
}

export interface PresetStats {
  generated_at: string;
  source_command: string;
  results: PresetStatRow[];
  /** `preset_ids_for(style, direction)` keyed "STYLE:DIRECTION" */
  preset_ids: Record<string, string[]>;
}

export interface ProfileEstimate {
  coins: string[];
  master_switch: "ON" | "OFF";
  style: Style;
  direction: Direction;
  feed: {
    ingest_max_symbols: number;
    coins_in_feed: string[];
    not_in_feed: { coin: string; code: "NOT_IN_FEED" }[];
  };
  estimate: { debates_per_day: string | null; llm_usd_per_day: string | null; method: string };
}

export interface StopAllResult {
  close: boolean;
  selected_count: number;
  requested_count: number;
  refused_count: number;
  profiles: { id: string; status: string; outcome?: string; error?: string; detail?: unknown }[];
}

/** The wizard's TOTP-free draft (persisted on this device only). Numbers stay strings, as typed. */
export interface ProfileDraft {
  name: string;
  coins: string[];
  markets: Market[];
  capital_budget_usd: string;
  style: Style;
  direction: Direction;
  max_risk_per_trade_pct: string;
  daily_loss_limit_pct: string;
  max_drawdown_pct: string;
  max_position_pct: string;
  max_leverage: string;
  max_open_positions: string;
  max_debates_per_day: string;
  exit_tp_r: string;
  exit_tp1_scale_pct: string;
  exit_trail_activate_pct: string;
  exit_trail_pct: string;
  exit_max_hold_hours: string;
  notify_level: NotifyLevel;
  breaker_max_loss_streak: string;
  breaker_max_reject_streak: string;
  mode: ExecutionMode;
  dry_run: boolean;
  preset: FieldPreset | null;
}

export type FieldPreset = "conservative-swing" | "leveraged-trend" | "intraday-active" | "short-only-hedge";

export interface ApiError {
  message: string;
  status?: number;
  code?: string;
  /** Field name -> message, from a 422 that names its fields */
  fields?: Record<string, string>;
}
