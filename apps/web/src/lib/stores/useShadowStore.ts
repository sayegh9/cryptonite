import { create } from "zustand";
import { persist } from "zustand/middleware";
import { fetchShadow } from "@/lib/mission/client";
import type { ShadowReport } from "@/lib/mission/types";

export const SHADOW_DAYS = [1, 7, 30] as const;

/**
 * Shadow mode panel state (F2, read-only). Persisted: the role filter, the window and which route's
 * config is open. The report itself is refetched.
 */
interface ShadowState {
  role: string | null;
  days: number;
  openRoute: string | null;

  report: ShadowReport | null;
  loading: boolean;
  error: string | null;

  setRole: (role: string | null) => void;
  setDays: (days: number) => void;
  setOpenRoute: (role: string | null) => void;
  load: () => Promise<void>;
}

export const useShadowStore = create<ShadowState>()(
  persist(
    (set, get) => ({
      role: null,
      days: 7,
      openRoute: null,

      report: null,
      loading: false,
      error: null,

      setRole: (role) => set({ role }),
      setDays: (days) => set({ days }),
      setOpenRoute: (openRoute) => set({ openRoute }),
      load: async () => {
        const { role, days } = get();
        set({ loading: true });
        try {
          const report = await fetchShadow(role, days);
          set({ report, loading: false, error: report.available === false ? "Shadow results are not available yet." : null });
        } catch (e) {
          set({ loading: false, report: null, error: (e as Error)?.message || "Shadow report unavailable." });
        }
      },
    }),
    {
      name: "cryptonite-shadow",
      skipHydration: true,
      partialize: (s) => ({ role: s.role, days: s.days, openRoute: s.openRoute }),
    },
  ),
);
