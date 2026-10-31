import { useBrainStore } from "@/lib/stores/useBrainStore";
import { readSse } from "./sse";
import {
  DEBATE_CARD_STATUSES,
  type BrainSessionPage,
  type BrainSessionSummary,
  type CardOutcome,
  type CardPreflight,
  type DebateOutcome,
  type DecisionTrace,
  type OrderCardWidget,
  type StoredBrainMessage,
} from "./types";

/**
 * Brain client: one conversation shared by Brain Home and the Copilot drawer.
 * Streams through the Next proxy (/agent-api/brain/stream → API → agent_worker).
 */

let active: AbortController | null = null;

function csrf(): string {
  try {
    return localStorage.getItem("csrf_token") || "";
  } catch {
    return "";
  }
}

export function isBrainStreaming(): boolean {
  return active !== null;
}

/** Send one operator message and stream the Brain's answer into the store. */
export async function sendBrainMessage(text: string): Promise<void> {
  const message = text.trim();
  const store = useBrainStore.getState();
  if (!message || active) return;
  store.setDraft("");
  const assistantId = store.beginTurn(message);
  const controller = new AbortController();
  active = controller;
  let terminal: "done" | "error" | null = null;
  try {
    const res = await fetch("/agent-api/brain/stream", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", "X-CSRF-Token": csrf() },
      body: JSON.stringify({ message, session_id: useBrainStore.getState().sessionId }),
      signal: controller.signal,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const detail = typeof body.detail === "string" ? body.detail : res.statusText;
      const code = res.status === 401 || res.status === 403 ? "AUTH" : res.status === 429 ? "BUDGET" : "HTTP";
      useBrainStore.getState().endTurn(assistantId, "error", { code, message: detail || `HTTP ${res.status}` });
      return;
    }
    await readSse(res, (ev) => {
      useBrainStore.getState().applyEvent(assistantId, ev);
      if (ev.type === "done") terminal = "done";
      if (ev.type === "error") terminal = "error";
    });
    useBrainStore.getState().endTurn(assistantId, terminal ?? "error",
      terminal ? undefined : { code: "STREAM", message: "The stream ended before the answer finished." });
  } catch (err: any) {
    if (err?.name === "AbortError") {
      useBrainStore.getState().endTurn(assistantId, "stopped");
    } else {
      useBrainStore.getState().endTurn(assistantId, "error", { code: "NETWORK", message: err?.message || "Network error" });
    }
  } finally {
    if (active === controller) active = null;
  }
}

/** Stop the running turn. Aborting the request cancels the model call upstream. */
export function stopBrain(): void {
  active?.abort();
}

/** Restore the persisted session from the server (the Brain's own memory). */
export async function loadBrainHistory(sessionId: string): Promise<boolean> {
  try {
    const res = await fetch(`/agent-api/brain/sessions/${encodeURIComponent(sessionId)}`, { credentials: "include" });
    if (res.status === 404) {
      useBrainStore.getState().newConversation();
      return false;
    }
    if (!res.ok) return false;
    const body = (await res.json()) as { messages: StoredBrainMessage[] };
    useBrainStore.getState().loadHistory(sessionId, body.messages ?? []);
    return true;
  } catch {
    return false;
  }
}

/** A history request that failed; `status` tells a refused session (401/403) from an unavailable Brain */
export class BrainHistoryError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export interface BrainSessionQuery {
  query?: string;
  archived?: boolean;
  limit?: number;
  before?: string | null;
  signal?: AbortSignal;
}

/** One page of the operator's conversations, newest activity first. Throws on failure: a failed load is not "no conversations". */
export async function listBrainSessions(opts: BrainSessionQuery = {}): Promise<BrainSessionPage> {
  const params = new URLSearchParams();
  if (opts.query?.trim()) params.set("q", opts.query.trim());
  if (opts.archived) params.set("archived", "true");
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.before) params.set("before", opts.before);
  const qs = params.toString();
  const res = await fetch(`/agent-api/brain/sessions${qs ? `?${qs}` : ""}`, { credentials: "include", signal: opts.signal });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new BrainHistoryError(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`, res.status);
  return {
    sessions: Array.isArray(body.sessions) ? body.sessions : [],
    next_cursor: typeof body.next_cursor === "string" ? body.next_cursor : null,
  };
}

/** Rename a conversation or move it in or out of the archive */
export async function updateBrainSession(
  sessionId: string,
  patch: { title?: string; archived?: boolean },
): Promise<BrainSessionSummary> {
  const res = await fetch(`/agent-api/brain/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
    body: JSON.stringify(patch),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new BrainHistoryError(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`, res.status);
  return body.session as BrainSessionSummary;
}

async function deleteBrain(path: string): Promise<number> {
  const res = await fetch(path, {
    method: "DELETE",
    credentials: "include",
    headers: { "X-CSRF-Token": csrf() },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new BrainHistoryError(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`, res.status);
  return typeof body.deleted === "number" ? body.deleted : 0;
}

/** Delete a conversation for good, with its messages */
export async function deleteBrainSession(sessionId: string): Promise<void> {
  await deleteBrain(`/agent-api/brain/sessions/${encodeURIComponent(sessionId)}`);
}

/** Delete every archived conversation for good; resolves to how many went */
export async function deleteArchivedBrainSessions(): Promise<number> {
  return deleteBrain("/agent-api/brain/sessions?archived=true");
}

/** What an approved card's order did. The server also records a change in the conversation, so the Brain knows. */
export async function fetchCardOutcome(sessionId: string, widgetId: string, signal?: AbortSignal): Promise<CardOutcome> {
  const res = await fetch(
    `/agent-api/brain/sessions/${encodeURIComponent(sessionId)}/cards/${encodeURIComponent(widgetId)}/outcome`,
    { credentials: "include", signal },
  );
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new BrainHistoryError(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`, res.status);
  return body as CardOutcome;
}

/** Continue an earlier conversation: it becomes the current one and its messages come back from the server */
export async function openBrainSession(sessionId: string): Promise<boolean> {
  if (isBrainStreaming()) return false;
  useBrainStore.getState().setSessionId(sessionId);
  return loadBrainHistory(sessionId);
}

export async function fetchBrainSpend(): Promise<{ spent_usd: string; cap_usd: string } | null> {
  const res = await fetch("/agent-api/brain/spend", { credentials: "include" });
  if (!res.ok) return null;
  return res.json().catch(() => null);
}

export async function fetchDecisionTrace(traceId: string): Promise<DecisionTrace> {
  const res = await fetch(`/agent-api/brain/traces/${encodeURIComponent(traceId)}`, { credentials: "include" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
  }
  return res.json();
}

async function postCard(path: string, body?: unknown): Promise<any> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
  return data;
}

/**
 * Operator approval of an order card, decided server-side: the API claims the card, queues one PAPER
 * order for GuardedOrderDispatcher (1–25% clamp, kill switch…) sized by the card's allocation, and
 * refuses while the exit monitor (the position's stop) is down. Repeating it never queues a second order.
 * `mandateId` names the mandate the order trades in; omitted, the API uses the single ACTIVE mandate that allows the instrument.
 */
export async function approveOrderCard(card: OrderCardWidget, mandateId?: string, perp?: PerpApproval): Promise<string> {
  const sessionId = useBrainStore.getState().sessionId;
  if (!sessionId) throw new Error("No Brain session");
  const body: Record<string, unknown> = {};
  if (mandateId) body.mandate_id = mandateId;
  if (perp) {
    body.totp_code = perp.totp_code;
    if (perp.leverage != null) body.leverage = perp.leverage;
    if (perp.confirm_high_leverage) body.confirm_high_leverage = true;
  }
  const res = await postCard(
    `/agent-api/brain/sessions/${encodeURIComponent(sessionId)}/cards/${encodeURIComponent(card.id)}/approve`,
    Object.keys(body).length ? body : undefined,
  );
  const commandId = String(res.command_id);
  // A perp card keeps the leverage it was approved at: the pills lock from here on
  const approvedLeverage = typeof res.leverage === "number" ? res.leverage : perp?.leverage ?? card.leverage ?? null;
  useBrainStore.getState().updateOrderCard(card.id, {
    status: "APPROVED", command_id: commandId, ...(perp ? { leverage: approvedLeverage } : {}),
  });
  return commandId;
}

/**
 * What approving a perp entry sends besides the mandate: the operator's TOTP code (every risk-adding perp order,
 * ADR 0009 D3), the leverage pill picked (the server plans it again and may lower it, never raise it) and the second
 * confirmation the server asks for at 8x and above.
 */
export interface PerpApproval {
  totp_code: string;
  leverage?: number | null;
  confirm_high_leverage?: boolean;
}

/**
 * Dry run of Approve: what the order would size to, or why the server would refuse it. Read-only; it claims, queues and
 * writes nothing, so it can be asked as often as the operator looks at the card. Throws on failure (the card then shows nothing).
 */
export async function fetchCardPreflight(
  sessionId: string, widgetId: string, signal?: AbortSignal, leverage?: number | null,
): Promise<CardPreflight> {
  // A perp card's leverage pill: the dry run plans that leverage instead of the card's
  const query = leverage != null ? `?leverage=${encodeURIComponent(String(leverage))}` : "";
  const res = await fetch(
    `/agent-api/brain/sessions/${encodeURIComponent(sessionId)}/cards/${encodeURIComponent(widgetId)}/preflight${query}`,
    { credentials: "include", cache: "no-store", signal },
  );
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new BrainHistoryError(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`, res.status);
  // Anything but the contract (a proxy page, an odd body) is not a dry run: the card shows nothing
  if (typeof body !== "object" || body === null || typeof body.ok !== "boolean") throw new Error("Unexpected dry-run response");
  return body as CardPreflight;
}

/**
 * Where a debate card's debate stands: read-only, from the debate run its stage call queued. A card whose run does not
 * exist yet reads QUEUED. Throws on failure, and on a status the card does not know (the card then keeps what it had).
 */
export async function fetchCardDebate(sessionId: string, widgetId: string, signal?: AbortSignal): Promise<DebateOutcome> {
  const res = await fetch(
    `/agent-api/brain/sessions/${encodeURIComponent(sessionId)}/cards/${encodeURIComponent(widgetId)}/debate`,
    { credentials: "include", cache: "no-store", signal },
  );
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new BrainHistoryError(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`, res.status);
  if (typeof body !== "object" || body === null || !(DEBATE_CARD_STATUSES as readonly string[]).includes(body.status)) {
    throw new Error("Unexpected debate response");
  }
  return body as DebateOutcome;
}

export async function rejectOrderCard(card: OrderCardWidget): Promise<void> {
  const sessionId = useBrainStore.getState().sessionId;
  if (!sessionId) return;
  await postCard(`/agent-api/brain/sessions/${encodeURIComponent(sessionId)}/notes`, {
    content: `Operator rejected order card ${card.id} (${card.side} ${card.instrument_id}, ${card.allocation_pct}% of mandate).`,
    widget_id: card.id,
    status: "REJECTED",
  });
  useBrainStore.getState().updateOrderCard(card.id, { status: "REJECTED" });
}
