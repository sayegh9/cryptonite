import { create } from "zustand";
import { persist } from "zustand/middleware";
import { fetchAiStatus, listApprovals, listMandates } from "@/lib/mission/client";
import type { AiStatus, Approval, Mandate } from "@/lib/mission/types";
import { useSessionStore } from "@/lib/stores/useSessionStore";

/**
 * Mission Control state (blueprint §7.1). Persisted: the view, the selected mandate and the editor's
 * TOTP-free draft fields. Server data (mandates, approvals, status) is refetched, never persisted.
 *
 * `skipHydration`: `useMandateStore.persist.rehydrate()` runs after mount (no hydration mismatch).
 */
export type MissionView = "overview" | "mandates" | "autonomy";

/** A refresh that has not answered by then fails, so the banner says "unknown" instead of showing old data */
export const REFRESH_TIMEOUT_MS = 8_000;

// The one in-flight refresh, and the id of the latest one started: only that one may write the store
let inflight: { id: number; startedAt: number; ctrl: AbortController; promise: Promise<void> } | null = null;
let latestRefresh = 0;

interface MandateState {
  view: MissionView;
  selectedMandateId: string | null;

  mandates: Mandate[];
  approvals: Approval[];
  status: AiStatus | null;
  /** server clock minus client clock, so countdowns follow the server's deadline */
  clockOffsetMs: number;
  loaded: boolean;
  error: string | null;
  /** Outcome of the latest fetch of each source (null: not fetched yet), for the mode banner (G1) */
  mandatesOk: boolean | null;
  /** Approvals have their own outcome: an empty list after a failed fetch is unknown, not "nothing waiting" */
  approvalsOk: boolean | null;
  statusOk: boolean | null;
  /** Date.now() of each source's last successful fetch: the banner treats old data as unknown */
  mandatesAt: number | null;
  statusAt: number | null;
  /** The latest refresh was refused (401/403) */
  denied: boolean;

  setView: (view: MissionView) => void;
  selectMandate: (id: string | null) => void;
  /**
   * Refetch mandates, approvals and status. Every caller (header poll, Mission Control poll, events)
   * shares one in-flight refresh; `after` (a performance.now() time, e.g. an event's timeStamp) asks for
   * data fetched after that moment, so an in-flight refresh that started earlier is superseded.
   */
  refresh: (opts?: { after?: number }) => Promise<void>;
  markDecided: (approvalId: string, status: Approval["status"]) => void;
}

export const useMandateStore = create<MandateState>()(
  persist(
    (set) => ({
      view: "overview",
      selectedMandateId: null,

      mandates: [],
      approvals: [],
      status: null,
      clockOffsetMs: 0,
      loaded: false,
      error: null,
      mandatesOk: null,
      approvalsOk: null,
      statusOk: null,
      mandatesAt: null,
      statusAt: null,
      denied: false,

      setView: (view) => set({ view }),
      selectMandate: (selectedMandateId) => set({ selectedMandateId }),

      refresh: ({ after } = {}) => {
        if (inflight && (after === undefined || inflight.startedAt >= after)) return inflight.promise;
        // Superseded: its answer may predate the change that asked for this refresh, so it must not land
        inflight?.ctrl.abort();
        const id = ++latestRefresh;
        const ctrl = new AbortController();
        const init = { signal: ctrl.signal, timeoutMs: REFRESH_TIMEOUT_MS };
        const run = async () => {
          const [m, a, s] = await Promise.allSettled([listMandates(init), listApprovals(init), fetchAiStatus(init)]);
          if (id !== latestRefresh) return; // a newer refresh owns the store
          const at = Date.now();
          const denied = [m, a, s].some((r) => r.status === "rejected" && [401, 403].includes((r.reason as any)?.status));
          set((prev) => ({
            mandates: m.status === "fulfilled" ? m.value : prev.mandates,
            approvals: a.status === "fulfilled" ? a.value.approvals : prev.approvals,
            clockOffsetMs: a.status === "fulfilled" ? new Date(a.value.now).getTime() - at : prev.clockOffsetMs,
            status: s.status === "fulfilled" ? s.value : prev.status,
            loaded: true,
            mandatesOk: m.status === "fulfilled",
            approvalsOk: a.status === "fulfilled",
            statusOk: s.status === "fulfilled",
            mandatesAt: m.status === "fulfilled" ? at : prev.mandatesAt,
            statusAt: s.status === "fulfilled" ? at : prev.statusAt,
            denied,
          error: denied
            ? "Sign in as operator to use Mission Control."
            : m.status === "rejected" && a.status === "rejected"
                ? "Mission Control data unavailable."
                : null,
          }));
          // An answer is evidence of a session. A refusal is not proof of a sign-out: one endpoint can refuse on its
          // own, so the session is re-checked with /auth/me, which decides.
          const session = useSessionStore.getState();
          if (denied) void session.check();
          else if (s.status === "fulfilled") session.markAuthenticated();
        };
        const promise = run().finally(() => {
          if (inflight?.id === id) inflight = null;
        });
        inflight = { id, startedAt: performance.now(), ctrl, promise };
        return promise;
      },

      markDecided: (approvalId, status) =>
        set((s) => ({ approvals: s.approvals.map((x) => (x.id === approvalId ? { ...x, status } : x)) })),
    }),
    {
      name: "cryptonite-mission-control",
      skipHydration: true,
      partialize: (s) => ({ view: s.view, selectedMandateId: s.selectedMandateId }),
    },
  ),
);
