import { create } from "zustand";
import { persist } from "zustand/middleware";
import { fetchAutonomy, fetchEvals, fetchOutcomesByRegime } from "@/lib/mission/client";
import type { AutonomyView, EvalRun, OutcomesByRegime, Reliability } from "@/lib/mission/types";

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
  calibration: Record<string, Reliability> | null;
  /** F1: 24h outcomes per regime for the open mandate; null = not available */
  byRegime: OutcomesByRegime | null;
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
      calibration: null,
      byRegime: null,
      error: null,

      setMandateId: (mandateId) => set({ mandateId, view: null }),
      setShowEvents: (showEvents) => set({ showEvents }),
      setView: (view) => set({ view }),
      load: async (mandateId) => {
        const [v, e, r] = await Promise.allSettled([fetchAutonomy(mandateId), fetchEvals(), fetchOutcomesByRegime(mandateId)]);
        set((prev) => ({
          byRegime: r.status === "fulfilled" && r.value?.available !== false ? r.value : null,
          view: v.status === "fulfilled" ? v.value : prev.view,
          evals: e.status === "fulfilled" ? e.value.runs : prev.evals,
          calibrationNote: e.status === "fulfilled" ? e.value.calibration_note : prev.calibrationNote,
          calibration: e.status === "fulfilled" ? e.value.calibration ?? null : prev.calibration,
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
