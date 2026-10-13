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
}

export interface TearsheetWidget {
  kind: "tearsheet";
  id: string;
  backtest_id: string;
  metrics: Record<string, unknown>;
}

export type OrderCardStatus = "PENDING_APPROVAL" | "APPROVED" | "REJECTED" | "FAILED";

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
  rationale: string;
  risk_class: "ADD_RISK";
  status: OrderCardStatus;
  trace_id: string | null;
  command_id: string | null;
}

export interface DebateSummaryWidget {
  kind: "debate_summary";
  id: string;
  instrument_id: string;
  timeframe: string;
  recommendation: string | null;
  conviction_score: number | null;
  status: "QUEUED" | "FAILED";
  message: string;
}

export type BrainWidget = PriceChartWidget | TearsheetWidget | OrderCardWidget | DebateSummaryWidget;

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
