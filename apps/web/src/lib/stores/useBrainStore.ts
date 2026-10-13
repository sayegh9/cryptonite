import { create } from "zustand";
import { persist } from "zustand/middleware";
import type {
  BrainMessage,
  BrainStreamEvent,
  BrainWidget,
  OrderCardWidget,
  StoredBrainMessage,
} from "@/lib/brain/types";

/**
 * Brain Home state (blueprint §7.1). Persisted: the session id, the composer draft and the layout (rail, view).
 * Messages are not persisted in the browser: after a reload they come back from the server session
 * (ai_messages), which is what the Brain itself remembers.
 *
 * `skipHydration`: call `useBrainStore.persist.rehydrate()` after mount so the server render and the
 * first client render match (no hydration errors).
 */
interface BrainState {
  sessionId: string | null;
  draft: string;
  railOpen: boolean;
  /** The conversation history column on Brain Home */
  historyOpen: boolean;
  workspaceView: "brain" | "debates";

  messages: BrainMessage[];
  streaming: boolean;
  historyLoadedFor: string | null;
  traceDrawerId: string | null;

  setDraft: (draft: string) => void;
  setRailOpen: (open: boolean) => void;
  setHistoryOpen: (open: boolean) => void;
  setWorkspaceView: (view: "brain" | "debates") => void;
  setSessionId: (id: string | null) => void;
  newConversation: () => void;
  loadHistory: (sessionId: string, messages: StoredBrainMessage[]) => void;
  beginTurn: (userText: string) => string;
  applyEvent: (assistantId: string, ev: BrainStreamEvent) => void;
  endTurn: (assistantId: string, status: "done" | "stopped" | "error", error?: { code: string; message: string }) => void;
  updateOrderCard: (widgetId: string, patch: Partial<OrderCardWidget>) => void;
  openTrace: (traceId: string) => void;
  closeTrace: () => void;
}

let seq = 0;
const localId = (prefix: string) => `${prefix}-${Date.now().toString(36)}-${(seq++).toString(36)}`;

function upsertWidget(widgets: BrainWidget[], widget: BrainWidget): BrainWidget[] {
  const i = widgets.findIndex((w) => w.id === widget.id);
  if (i === -1) return [...widgets, widget];
  const next = widgets.slice();
  next[i] = widget;
  return next;
}

export const useBrainStore = create<BrainState>()(
  persist(
    (set) => ({
      sessionId: null,
      draft: "",
      railOpen: true,
      historyOpen: false,
      workspaceView: "brain",

      messages: [],
      streaming: false,
      historyLoadedFor: null,
      traceDrawerId: null,

      setDraft: (draft) => set({ draft }),
      setRailOpen: (railOpen) => set({ railOpen }),
      setHistoryOpen: (historyOpen) => set({ historyOpen }),
      setWorkspaceView: (workspaceView) => set({ workspaceView }),
      setSessionId: (sessionId) => set({ sessionId }),
      newConversation: () => set({ sessionId: null, messages: [], historyLoadedFor: null, streaming: false }),

      loadHistory: (sessionId, stored) =>
        set({
          historyLoadedFor: sessionId,
          messages: stored.map((m) => ({
            id: `srv-${m.id}`,
            role: m.role,
            content: m.content,
            widgets: m.widgets ?? [],
            toolCalls: [],
            traceId: m.trace_id,
            status: "done",
          })),
        }),

      beginTurn: (userText) => {
        const assistantId = localId("a");
        set((s) => ({
          streaming: true,
          messages: [
            ...s.messages,
            { id: localId("u"), role: "user", content: userText, widgets: [], toolCalls: [], traceId: null, status: "done" },
            { id: assistantId, role: "assistant", content: "", widgets: [], toolCalls: [], traceId: null, status: "streaming" },
          ],
        }));
        return assistantId;
      },

      applyEvent: (assistantId, ev) => {
        if (ev.type === "session") {
          set({ sessionId: ev.data.session_id, historyLoadedFor: ev.data.session_id });
        }
        set((s) => ({
          messages: s.messages.map((m) => {
            if (m.id !== assistantId) return m;
            switch (ev.type) {
              case "session":
                return { ...m, traceId: ev.data.trace_id };
              case "token":
                return { ...m, content: m.content + ev.data.text };
              case "tool_call": {
                const others = m.toolCalls.filter((c) => c.id !== ev.data.id);
                const prev = m.toolCalls.find((c) => c.id === ev.data.id);
                return {
                  ...m,
                  toolCalls: [...others, { id: ev.data.id, tool: ev.data.tool, args: ev.data.args ?? prev?.args, status: ev.data.status }],
                };
              }
              case "widget":
                return { ...m, widgets: upsertWidget(m.widgets, ev.data.widget) };
              case "done":
                return {
                  ...m,
                  content: ev.data.content || m.content,
                  traceId: ev.data.trace_id,
                  model: ev.data.model,
                  costUsd: ev.data.cost_usd,
                  status: "done",
                };
              case "error":
                return { ...m, status: "error", error: ev.data };
              default:
                return m;
            }
          }),
        }));
      },

      endTurn: (assistantId, status, error) =>
        set((s) => ({
          streaming: false,
          messages: s.messages.map((m) =>
            m.id === assistantId && m.status === "streaming" ? { ...m, status, ...(error ? { error } : {}) } : m,
          ),
        })),

      updateOrderCard: (widgetId, patch) =>
        set((s) => ({
          messages: s.messages.map((m) => ({
            ...m,
            widgets: m.widgets.map((w) => (w.kind === "order_card" && w.id === widgetId ? { ...w, ...patch } : w)),
          })),
        })),

      openTrace: (traceDrawerId) => set({ traceDrawerId }),
      closeTrace: () => set({ traceDrawerId: null }),
    }),
    {
      name: "cryptonite-brain",
      skipHydration: true,
      partialize: (s) => ({
        sessionId: s.sessionId,
        draft: s.draft,
        railOpen: s.railOpen,
        historyOpen: s.historyOpen,
        workspaceView: s.workspaceView,
      }),
    },
  ),
);

/** The order card (and the message holding it) with this widget id, if any. */
export function findOrderCard(widgetId: string): OrderCardWidget | undefined {
  for (const m of useBrainStore.getState().messages) {
    for (const w of m.widgets) if (w.kind === "order_card" && w.id === widgetId) return w;
  }
  return undefined;
}
