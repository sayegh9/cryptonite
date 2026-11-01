import { create } from "zustand";
import { persist } from "zustand/middleware";
import { fetchApi } from "@/lib/api";

/**
 * Setups inbox (AI Phase 2, Track C): the fresh scanner candidates from GET /api/v1/setups. Persisted: the filters,
 * the window and the open row. The rows themselves are server data, refetched on mount; a failed refresh keeps the
 * last good rows and reports the error instead of showing an empty inbox.
 */
export interface SetupDecision {
  id: number;
  stage: string;
  outcome: string;
  reason: string | null;
  mandate_id: string;
  ts: string | null;
}

export interface SetupPolicy {
  policy: "A" | "B" | "C";
  decision: "TAKE" | "SKIP" | "ERROR";
  side: string | null;
  size_factor: string | null;
  reason: string | null;
  model: string | null;
  cost_usd: string | null;
  decided_at: string | null;
}

export interface Setup {
  id: string;
  instrument_id: string;
  provenance: { preset: string | null; timeframe: string; scan_run_id: string; spec_version_id: string };
  direction: "LONG" | "SHORT" | null;
  entry: string | null;
  stop: string | null;
  target: string | null;
  created_at: string | null;
  age_seconds: number;
  /** Seconds since the candidate's bar closed; the gate refuses it after max(15 min, two bars). */
  bar_age_seconds: number | null;
  routeable: boolean;
  routeable_reason: string | null;
  cost: { cost_r: number | null; cost_model: string; market: string; hold_hours: number };
  profile: { id: string; name: string; status: string; eligible: boolean; reason: string | null } | null;
  eligibility: { eligible: boolean; reason: string | null };
  funnel: { furthest_stage: string | null; outcome: string | null; reason: string | null; trail: SetupDecision[] };
  policies: SetupPolicy[];
  brief: { matched_rules: unknown[]; metrics: Record<string, unknown> };
}

export type EligibilityFilter = "all" | "eligible" | "ineligible";
export interface SetupFilters {
  profile: string;
  preset: string;
  direction: "all" | "LONG" | "SHORT";
  eligibility: EligibilityFilter;
}

export const DEFAULT_FILTERS: SetupFilters = { profile: "all", preset: "all", direction: "all", eligibility: "all" };

interface SetupsState {
  filters: SetupFilters;
  windowHours: number;
  selectedId: string | null;
  setups: Setup[];
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
  updatedAt: number | null;
  setFilter: <K extends keyof SetupFilters>(key: K, value: SetupFilters[K]) => void;
  resetFilters: () => void;
  setWindowHours: (hours: number) => void;
  select: (id: string | null) => void;
  refresh: () => Promise<void>;
}

const REFRESH_TIMEOUT_MS = 8_000;

export const useSetupsStore = create<SetupsState>()(
  persist(
    (set, get) => ({
      filters: DEFAULT_FILTERS,
      windowHours: 6,
      selectedId: null,
      setups: [],
      status: "idle",
      error: null,
      updatedAt: null,
      setFilter: (key, value) => set((s) => ({ filters: { ...s.filters, [key]: value } })),
      resetFilters: () => set({ filters: DEFAULT_FILTERS }),
      setWindowHours: (hours) => set({ windowHours: hours }),
      select: (id) => set({ selectedId: id }),
      refresh: async () => {
        set((s) => ({ status: s.updatedAt ? s.status : "loading" }));
        try {
          const since = new Date(Date.now() - get().windowHours * 3_600_000).toISOString();
          const body = await fetchApi(`/setups?since=${encodeURIComponent(since)}`, { timeoutMs: REFRESH_TIMEOUT_MS });
          set({ setups: body.setups ?? [], status: "ready", error: null, updatedAt: Date.now() });
        } catch (e) {
          const message = e instanceof Error ? e.message : "The setups could not be loaded";
          set({ status: "error", error: message });
        }
      },
    }),
    {
      name: "cryptonite-setups",
      partialize: (s) => ({ filters: s.filters, windowHours: s.windowHours, selectedId: s.selectedId }),
    },
  ),
);

export function applyFilters(setups: Setup[], f: SetupFilters): Setup[] {
  return setups.filter(
    (s) =>
      (f.profile === "all" || (f.profile === "none" ? s.profile === null : s.profile?.id === f.profile)) &&
      (f.preset === "all" || s.provenance.preset === f.preset) &&
      (f.direction === "all" || s.direction === f.direction) &&
      (f.eligibility === "all" || (f.eligibility === "eligible") === s.eligibility.eligible),
  );
}
