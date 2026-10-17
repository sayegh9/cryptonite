/**
 * Brain wire types (mirror of packages/agents/cryptonite_agents/brain/{agent,widgets}.py).
 *
 * SSE events from `/agent-api/brain/stream`:
 *   session → token* / tool_call* / widget* → done | error
 */

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

/**
 * What approving an order card would do now, from /brain/sessions/{id}/cards/{card}/preflight. Read-only: the card stays
 * PENDING_APPROVAL and nothing is claimed or queued. `needs_mandate` is true for a BUY that has no ACTIVE mandate yet.
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
    }
  | { ok: false; code: string; detail: string; needs_mandate: boolean };

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

export type BrainWidget =
  | PriceChartWidget
  | TearsheetWidget
  | OrderCardWidget
  | DebateSummaryWidget
  | SwingRadarWidget;

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
