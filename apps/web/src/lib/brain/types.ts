/**
 * Brain wire types (mirror of packages/agents/cryptonite_agents/brain/{agent,widgets}.py).
 *
 * SSE events from `/agent-api/brain/stream`:
 *   session → token* / tool_call* / widget* → done | error
 */

import type { Direction, ExecutionMode, Market, NotifyLevel, Style } from "@/lib/autotrade/types";

export interface PriceChartWidget {
  kind: "price_chart";
  id: string;
  symbol: string;
  timeframe: string;
  entry?: number | null;
  stop?: number | null;
  target?: number | null;
}

export interface TearsheetWidget {
  kind: "tearsheet";
  id: string;
  backtest_id: string;
  metrics: Record<string, unknown>;
}

/**
 * PENDING_APPROVAL → APPROVED (your decision) → what the order did: WORKING (a resting limit), FILLED, or NOT_EXECUTED
 * (refused, failed or cancelled with nothing filled). REJECTED is you saying no; FAILED is the approval not queued.
 */
export type OrderCardStatus =
  | "PENDING_APPROVAL" | "APPROVED" | "REJECTED" | "FAILED" | "WORKING" | "FILLED" | "NOT_EXECUTED";

export interface OrderCardWidget {
  kind: "order_card";
  id: string;
  instrument_id: string;
  side: "BUY" | "SELL";
  /** Not shown: an approved order is sized from allocation_pct at the live price (capped by the mandate). */
  quantity?: number | null;
  order_type: "MARKET" | "LIMIT";
  limit_price: number | null;
  allocation_pct: number;
  stop_loss: number | null;
  take_profit: number | null;
  reduce_pct?: 25 | 50 | 100 | null;
  rationale: string;
  risk_class: "ADD_RISK";
  status: OrderCardStatus;
  trace_id: string | null;
  command_id: string | null;
  /** Why the order was not executed (or a note on a partial fill), from the server */
  execution_reason?: string | null;
  filled_qty?: string | null;
  avg_fill_price?: string | null;
  /** Set when the live data feed does not stream the symbol (the reason line); null when it does or the feed is unknown */
  feed_refusal?: string | null;
  /** Past lessons on this instrument, shown collapsed under the rationale; absent or empty hides the disclosure */
  lessons?: CardLesson[];
  /** The structured rationale the model wrote (all optional: a card from before them has none). Shown as plain text. */
  thesis?: string | null;
  evidence?: CardEvidence[];
  invalidation?: string | null;
  horizon?: CardHorizon | null;
  /** 0 to 1; shown as a percentage */
  confidence?: number | null;
  /** A perp card's requested leverage (1–10); the approved plan may be lower. Null on spot */
  leverage?: number | null;
  /** The system's setup grade (never the model's), its reasons and the grader version; null when ungraded */
  setup_grade?: SetupGrade | null;
  grade_reasons?: string[];
  grade_version?: string | null;
}

export type SetupGrade = "A" | "B" | "C" | "NO_TRADE";

/** A perp instrument id ("BTCUSDT-PERP.BINANCE") */
export function isPerpInstrument(instrumentId: string): boolean {
  return instrumentId.split(".")[0].toUpperCase().endsWith("-PERP");
}

/**
 * The leverage plan of a perp entry, from the server (``plan_leveraged_order``): the leverage is the highest the six
 * ceilings allow, at most what was asked. Decimals arrive as strings; null when not known.
 */
export interface LeveragePlan {
  instrument_id: string;
  side: "BUY" | "SELL";
  reject: string | null;
  ok: boolean;
  /** 0 when refused */
  leverage: number;
  /** "7x (risk at stop)" when a ceiling cut the request, else "7x" */
  leverage_label: string;
  margin_mode: "ISOLATED";
  requested_leverage: number | null;
  /** The lowest ceiling: the recommended leverage */
  allowed_max_leverage: number;
  binding_ceiling: string | null;
  capped: boolean;
  /** Each ceiling's own maximum leverage, by label ("risk at stop", "liquidation buffer", "exposure", ...) */
  ceilings: Record<string, number>;
  margin_usd: string | null;
  notional_usd: string | null;
  quantity: string | null;
  entry_price: string | null;
  stop_price: string | null;
  stop_distance_pct: string | null;
  liquidation_price: string | null;
  liquidation_distance_pct: string | null;
  /** (stop distance + slippage) / liquidation distance, % */
  buffer_used_pct: string | null;
  loss_at_stop_usd: string | null;
  /** % of the mandate budget */
  loss_at_stop_pct: string | null;
  loss_if_liquidated_usd: string | null;
  /** Funding cash flow per 8 h at the current rate: negative = the position pays */
  funding_8h_usd: string | null;
  margin_cut: boolean;
}

export type CardHorizon = "1h" | "4h" | "24h" | "3d" | "7d";

/** One fact the proposal rests on: the tool it came from, the field, its value, and the time it describes */
export interface CardEvidence {
  source_tool: string;
  field: string;
  value: string;
  as_of?: string | null;
}

/** A past reflection on the same instrument, from the server (at most 3). `lesson_text` is model-written: shown as quoted data. */
export interface CardLesson {
  lesson_text: string;
  outcome: string | null;
  /** Alpha against the benchmark, in percent (e.g. 1.2 for +1.2%) */
  alpha_pct: number | null;
  created_at: string;
}

/** What an approved card's order did, from /brain/sessions/{id}/cards/{card}/outcome */
export interface CardOutcome {
  status: OrderCardStatus | null;
  state: "QUEUED" | "WORKING" | "FILLED" | "NOT_EXECUTED" | null;
  reason?: string | null;
  filled_qty?: string | null;
  avg_fill_price?: string | null;
  limit_price?: string | null;
}

/** An ACTIVE mandate a refused BUY's instrument could be added to, with the room the entry would have in it */
export interface RemedyMandate {
  id: string;
  name: string;
  autonomy_level: number;
  instruments: string[];
  max_position_pct: number;
  capital_budget_usd: number;
  room_pct: number;
}

/**
 * The mandate change that would let a refused BUY be queued, worked out by the server's own sizing. The card sends it
 * through the mandate routes (TOTP, caps, audit) and then approves; approve checks everything again.
 */
export type CardRemedy =
  | {
      kind: "CREATE_MANDATE";
      /** Whether a new mandate can be created for the instrument (a perp gets one with perps allowed, at L1) */
      can_create: boolean;
      /** The position limit a new single-instrument mandate starts with */
      position_pct: number;
      /** True for a perp: the new mandate is created with perps allowed (L1, 3x) */
      allow_perps?: boolean;
      add_to: RemedyMandate[];
    }
  | {
      kind: "EDIT_MANDATE";
      mandate_id: string;
      mandate_name: string;
      autonomy_level: number;
      field: "max_position_pct" | "capital_budget_usd";
      from: number;
      to: number;
      max_position_pct: number;
      capital_budget_usd: number;
      held_pct: number;
      requested_pct: number;
      /** The size the entry gets after the edit (the request, or the room left when that is smaller) */
      allocation_after_pct: number;
    }
  | { kind: "RESUME_MANDATE"; mandate_id: string; mandate_name: string }
  /** A perp entry in a mandate without perps: perps are L1 only, and turning them on starts at max_leverage_after */
  | { kind: "ALLOW_PERPS"; mandate_id: string; mandate_name: string; autonomy_level: number; max_leverage_after: number }
  | { kind: "CHOOSE_MANDATE"; candidates: MandateChoice[] };

/** One of several ACTIVE mandates that allow a BUY's instrument (AMBIGUOUS_MANDATE): approve needs one named */
export interface MandateChoice {
  id: string;
  name: string;
  autonomy_level: number;
  instruments: string[];
  max_position_pct: number;
  capital_budget_usd: number;
  room_pct: number;
  /** What approve would size the entry to inside this mandate */
  allocation_pct: number;
  holds: boolean;
  /** Why this mandate cannot take the order, or null */
  blocked: string | null;
  /** How this mandate can be taken off the instrument when another one trades it: neither needs TOTP (both take room
   * away); null for the mandate that holds it */
  tidy: "REMOVE_INSTRUMENT" | "PAUSE" | null;
}

/**
 * What approving an order card would do now, from /brain/sessions/{id}/cards/{card}/preflight. Read-only: the card stays
 * PENDING_APPROVAL and nothing is claimed or queued. `needs_mandate` is true for a BUY that has no ACTIVE mandate yet;
 * `remedy` is the mandate change that would fix the refusal, when one does.
 */
export type CardPreflight =
  | {
      ok: true;
      mandate: { id: string; name: string; status: string };
      side: "BUY" | "SELL";
      allocation_pct: number;
      budget_usd: string | number;
      /** Set when the room left under the mandate's position limit clipped the size */
      clipped_from_pct: number | null;
      sell_quantity: string | null;
      summary: string;
      /** A perp entry: the plan approve would queue, whether approve needs TOTP, and the 8x second confirmation */
      leverage_plan?: LeveragePlan | null;
      needs_totp?: boolean;
      high_leverage_confirm?: boolean;
      /** The mandate's own maximum cut the request: raising it (TOTP) would allow what was asked */
      leverage_remedy?: LeverageRemedy | null;
    }
  | {
      ok: false;
      code: string;
      detail: string;
      needs_mandate: boolean;
      remedy?: CardRemedy | null;
      leverage_plan?: LeveragePlan | null;
      needs_totp?: boolean;
    };

export interface LeverageRemedy {
  kind: "RAISE_MAX_LEVERAGE";
  mandate_id: string;
  mandate_name: string;
  from: number;
  to: number;
}

/**
 * Where a Brain debate stands, from its debate run (QUEUED until the run exists). UNTRACKED: a card queued before Wave 3
 * has no message id, so there is no run to follow (the route answers it; it is not a failure)
 */
export const DEBATE_CARD_STATUSES = [
  "QUEUED", "RUNNING", "VERDICT", "NO_TRADE", "GATE_REFUSED", "FAILED", "UNTRACKED",
] as const;
export type DebateCardStatus = (typeof DEBATE_CARD_STATUSES)[number];

/** The debate's verdict. approval_status is null until an approval exists for the run */
export interface DebateVerdict {
  recommendation: string;
  /** 0 to 1, shown as a percentage */
  conviction_score: number | null;
  allocation_pct: number | null;
  approval_status: string | null;
}

export interface DebateSummaryWidget {
  kind: "debate_summary";
  id: string;
  instrument_id: string;
  timeframe: string;
  recommendation: string | null;
  conviction_score: number | null;
  status: DebateCardStatus;
  message: string;
  /** The queue message the debate was staged under; links the card to its run */
  message_ref: string | null;
  run_id: string | null;
  verdict: DebateVerdict | null;
  /** NO_PRICE / NO_TRADE for no trade, the gate reason for GATE_REFUSED, else null */
  reason: string | null;
}

/** What a debate card shows now, from /brain/sessions/{id}/cards/{card}/debate. Read-only. */
export interface DebateOutcome {
  status: DebateCardStatus;
  run_id: string | null;
  verdict: DebateVerdict | null;
  reason: string | null;
}

export interface SwingCandidateItem {
  instrument_id: string;
  preset_slug: string;
  timeframe: string;
  direction: "LONG" | "SHORT";
  score: number;
  entry: number;
  stop: number;
  target: number;
  rr_ratio: number;
  trend_4h?: string;
  trend_1h?: string;
  trend_15m?: string;
  matched_rules?: string[];
}

export interface SwingRadarWidget {
  kind: "swing_radar";
  id?: string;
  preset_slug: string;
  timeframe: string;
  scanned_count: number;
  candidates: SwingCandidateItem[];
}

/** The read-only tool's resolved spec; the API rechecks it when the operator creates a profile. */
export interface ProfileProposalSpec {
  name: string;
  coins: string[];
  markets: Market[];
  capital_budget_usd: string;
  style: Style;
  direction: Direction;
  max_position_pct: string;
  max_leverage: number;
  max_open_positions: number;
  max_debates_per_day: number;
  max_risk_per_trade_pct: string;
  daily_loss_limit_pct: string;
  daily_loss_defaulted: boolean;
  max_drawdown_pct: string;
  exit_tp_r: string;
  exit_tp1_scale_pct: string;
  exit_trail_activate_pct: string | null;
  exit_trail_pct: string | null;
  exit_max_hold_hours: number | null;
  notify_level: NotifyLevel;
  breaker_max_loss_streak: number;
  breaker_max_reject_streak: number;
  mode: ExecutionMode;
  dry_run: boolean;
}

export interface ProfileProposalWidget {
  kind: "profile_proposal";
  id: string;
  status: "PROPOSED" | "INVALID";
  proposal: ProfileProposalSpec;
  errors: string[];
  warnings: string[];
  feed_status?: "known" | "unknown" | null;
}

export type BrainWidget =
  | PriceChartWidget
  | TearsheetWidget
  | OrderCardWidget
  | DebateSummaryWidget
  | SwingRadarWidget
  | ProfileProposalWidget;

export type ToolCallStatus = "RUNNING" | "OK" | "DENIED" | "UNKNOWN";

export interface BrainToolCall {
  id: string;
  tool: string;
  args?: Record<string, unknown>;
  status: ToolCallStatus;
}

export type BrainStreamEvent =
  | { type: "session"; data: { session_id: string; trace_id: string } }
  | { type: "token"; data: { text: string } }
  | {
      type: "tool_call";
      data: { id: string; tool: string; args?: Record<string, unknown>; phase: "start" | "end"; status: ToolCallStatus };
    }
  | { type: "widget"; data: { widget: BrainWidget } }
  | {
      type: "done";
      data: { session_id: string; message_id: number; trace_id: string; model: string; cost_usd: string; content: string };
    }
  | { type: "error"; data: { code: string; message: string } };

export type BrainMessageStatus = "streaming" | "done" | "stopped" | "error";

export interface BrainMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  widgets: BrainWidget[];
  toolCalls: BrainToolCall[];
  traceId: string | null;
  status: BrainMessageStatus;
  error?: { code: string; message: string };
  model?: string;
  costUsd?: string;
}

/** A stored message as returned by GET /agent-api/brain/sessions/{id}. */
export interface StoredBrainMessage {
  id: number;
  role: "user" | "assistant" | "system";
  content: string;
  widgets: BrainWidget[];
  trace_id: string | null;
  created_at: string | null;
}

export interface BrainSessionSummary {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  archived?: boolean;
  /** Operator and Brain turns (system lines such as approved cards are not counted) */
  message_count?: number;
  /** The last thing said, one line, clipped by the server */
  preview?: { role: "user" | "assistant"; content: string } | null;
}

/** One page of conversation history; `next_cursor` is null on the last page */
export interface BrainSessionPage {
  sessions: BrainSessionSummary[];
  next_cursor: string | null;
}

export interface DecisionTraceRow {
  id: string;
  parent_id: string | null;
  role: string;
  model: string;
  session_id: string | null;
  mandate_id: string | null;
  status: string;
  tool_calls: { tool: string; risk?: string; args?: Record<string, unknown>; status: string }[];
  output: unknown;
  error: string | null;
  cost_usd: string;
  latency_ms: number | null;
  created_at: string;
}

export interface DecisionTrace {
  trace: DecisionTraceRow;
  parent: DecisionTraceRow | null;
  children: DecisionTraceRow[];
}
