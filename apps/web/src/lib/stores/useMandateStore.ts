import { create } from "zustand";
import { persist } from "zustand/middleware";
import { fetchAiStatus, listApprovals, listMandates } from "@/lib/mission/client";
import type { AiStatus, Approval, Mandate } from "@/lib/mission/types";

/**
 * Mission Control state (blueprint §7.1). Persisted: the view, the selected mandate and the editor's
 * TOTP-free draft fields. Server data (mandates, approvals, status) is refetched, never persisted.
 *
 * `skipHydration`: `useMandateStore.persist.rehydrate()` runs after mount (no hydration mismatch).
 */
export type MissionView = "overview" | "mandates" | "autonomy";

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

  setView: (view: MissionView) => void;
  selectMandate: (id: string | null) => void;
  refresh: () => Promise<void>;
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

      setView: (view) => set({ view }),
      selectMandate: (selectedMandateId) => set({ selectedMandateId }),

      refresh: async () => {
        const [m, a, s] = await Promise.allSettled([listMandates(), listApprovals(), fetchAiStatus()]);
        const denied = [m, a, s].some((r) => r.status === "rejected" && [401, 403].includes((r.reason as any)?.status));
        set((prev) => ({
          mandates: m.status === "fulfilled" ? m.value : prev.mandates,
          approvals: a.status === "fulfilled" ? a.value.approvals : prev.approvals,
          clockOffsetMs: a.status === "fulfilled" ? new Date(a.value.now).getTime() - Date.now() : prev.clockOffsetMs,
          status: s.status === "fulfilled" ? s.value : prev.status,
          loaded: true,
          error: denied
            ? "Sign in as operator to use Mission Control."
            : m.status === "rejected" && a.status === "rejected"
              ? "Mission Control data unavailable."
              : null,
        }));
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
