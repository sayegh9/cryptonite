import { fetchApi } from "@/lib/api";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { readSse } from "./sse";
import type { BrainSessionSummary, DecisionTrace, OrderCardWidget, StoredBrainMessage } from "./types";

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

export async function listBrainSessions(): Promise<BrainSessionSummary[]> {
  const res = await fetch("/agent-api/brain/sessions", { credentials: "include" });
  if (!res.ok) return [];
  const body = await res.json().catch(() => ({}));
  return Array.isArray(body.sessions) ? body.sessions : [];
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

async function recordNote(content: string, card: OrderCardWidget, status: OrderCardWidget["status"], commandId?: string) {
  const sessionId = useBrainStore.getState().sessionId;
  if (!sessionId) return;
  try {
    await fetch(`/agent-api/brain/sessions/${encodeURIComponent(sessionId)}/notes`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify({ content, widget_id: card.id, status, command_id: commandId ?? null }),
    });
  } catch {
    // The order itself is already queued; the note only keeps the conversation in sync.
  }
}

/**
 * Operator approval of an order card: the existing paper manual-order API, with CSRF.
 * That endpoint enqueues a PAPER command that GuardedOrderDispatcher checks (1–25% clamp, kill switch…).
 */
export async function approveOrderCard(card: OrderCardWidget): Promise<string> {
  const payload: Record<string, unknown> = {
    instrument_id: card.instrument_id,
    side: card.side,
    order_type: card.order_type,
    quantity: card.quantity,
  };
  if (card.order_type === "LIMIT" && card.limit_price) payload.price = card.limit_price;
  try {
    const res = await fetchApi("/paper/orders", {
      method: "POST",
      headers: { "X-CSRF-Token": csrf() },
      body: JSON.stringify(payload),
    });
    const commandId = String(res.command_id ?? res.id ?? "queued");
    useBrainStore.getState().updateOrderCard(card.id, { status: "APPROVED", command_id: commandId });
    await recordNote(
      `Operator approved order card ${card.id}: ${card.side} ${card.quantity} ${card.instrument_id} ${card.order_type}. ` +
        `Paper command ${commandId} queued for the dispatcher.`,
      card, "APPROVED", commandId,
    );
    return commandId;
  } catch (err: any) {
    useBrainStore.getState().updateOrderCard(card.id, { status: "FAILED" });
    await recordNote(`Operator approved order card ${card.id}, but the paper order API refused it: ${err?.message}.`, card, "FAILED");
    throw err;
  }
}

export async function rejectOrderCard(card: OrderCardWidget): Promise<void> {
  useBrainStore.getState().updateOrderCard(card.id, { status: "REJECTED" });
  await recordNote(`Operator rejected order card ${card.id} (${card.side} ${card.quantity} ${card.instrument_id}).`, card, "REJECTED");
}
