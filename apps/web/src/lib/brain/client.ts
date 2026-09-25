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
 */
export async function approveOrderCard(card: OrderCardWidget): Promise<string> {
  const sessionId = useBrainStore.getState().sessionId;
  if (!sessionId) throw new Error("No Brain session");
  const res = await postCard(
    `/agent-api/brain/sessions/${encodeURIComponent(sessionId)}/cards/${encodeURIComponent(card.id)}/approve`,
  );
  const commandId = String(res.command_id);
  useBrainStore.getState().updateOrderCard(card.id, { status: "APPROVED", command_id: commandId });
  return commandId;
}

export async function rejectOrderCard(card: OrderCardWidget): Promise<void> {
  const sessionId = useBrainStore.getState().sessionId;
  if (!sessionId) return;
  await postCard(`/agent-api/brain/sessions/${encodeURIComponent(sessionId)}/notes`, {
    content: `Operator rejected order card ${card.id} (${card.side} ${card.instrument_id}, ${card.allocation_pct}% of equity).`,
    widget_id: card.id,
    status: "REJECTED",
  });
  useBrainStore.getState().updateOrderCard(card.id, { status: "REJECTED" });
}
