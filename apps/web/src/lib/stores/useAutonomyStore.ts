import { create } from "zustand";
import { persist } from "zustand/middleware";
import { fetchAutonomy, fetchEvals } from "@/lib/mission/client";
import type { AutonomyView, EvalRun } from "@/lib/mission/types";

/**
 * Autonomy & Evals state (blueprint §4.5, §7.1). Persisted: which mandate is open and whether the
 * event log is expanded. Gate reports and eval runs are refetched.
 */
interface AutonomyState {
  mandateId: string | null;
  showEvents: boolean;

  view: AutonomyView | null;
  evals: EvalRun[];
  calibrationNote: string | null;
  error: string | null;

  setMandateId: (id: string | null) => void;
  setShowEvents: (show: boolean) => void;
  setView: (view: AutonomyView) => void;
  load: (mandateId: string) => Promise<void>;
}

export const useAutonomyStore = create<AutonomyState>()(
  persist(
    (set) => ({
      mandateId: null,
      showEvents: true,

      view: null,
      evals: [],
      calibrationNote: null,
      error: null,

      setMandateId: (mandateId) => set({ mandateId, view: null }),
      setShowEvents: (showEvents) => set({ showEvents }),
      setView: (view) => set({ view }),
      load: async (mandateId) => {
        const [v, e] = await Promise.allSettled([fetchAutonomy(mandateId), fetchEvals()]);
        set((prev) => ({
          view: v.status === "fulfilled" ? v.value : prev.view,
          evals: e.status === "fulfilled" ? e.value.runs : prev.evals,
          calibrationNote: e.status === "fulfilled" ? e.value.calibration_note : prev.calibrationNote,
          error: v.status === "rejected" ? (v.reason as Error)?.message || "Gate report unavailable." : null,
        }));
      },
    }),
    {
      name: "cryptonite-autonomy",
      skipHydration: true,
      partialize: (s) => ({ mandateId: s.mandateId, showEvents: s.showEvents }),
    },
  ),
);
