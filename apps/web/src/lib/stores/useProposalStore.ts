import { create } from "zustand";
import { persist } from "zustand/middleware";
import { listProposals, type Proposal } from "@/lib/proposals/client";

/**
 * Setups to review (H13b). Persisted: whether the decided rows are shown. The rows themselves are refetched: the server
 * decides what is still pending.
 */
interface ProposalState {
  showRecent: boolean;

  proposals: Proposal[];
  pending: number;
  /** Server time minus this browser's, so a countdown does not run on a wrong local clock */
  clockOffsetMs: number;
  loaded: boolean;
  error: string | null;

  setShowRecent: (show: boolean) => void;
  load: () => Promise<void>;
}

export const useProposalStore = create<ProposalState>()(
  persist(
    (set) => ({
      showRecent: false,

      proposals: [],
      pending: 0,
      clockOffsetMs: 0,
      loaded: false,
      error: null,

      setShowRecent: (showRecent) => set({ showRecent }),
      load: async () => {
        try {
          const list = await listProposals();
          set({
            proposals: list.proposals, pending: list.pending, loaded: true, error: null,
            clockOffsetMs: new Date(list.now).getTime() - Date.now(),
          });
        } catch (e) {
          set({ error: e instanceof Error ? e.message : "unavailable" });
        }
      },
    }),
    { name: "cryptonite-proposals", skipHydration: true, partialize: (s) => ({ showRecent: s.showRecent }) },
  ),
);
