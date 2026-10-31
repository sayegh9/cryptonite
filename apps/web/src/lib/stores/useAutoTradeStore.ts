import { create } from "zustand";
import { persist } from "zustand/middleware";
import * as api from "@/lib/autotrade/client";
import { dispatchMandateUpdated } from "@/lib/mission/events";
import { EMPTY_DRAFT, applyFieldPreset, createBody } from "@/lib/autotrade/presets";
import type {
  ApiError,
  CreatedProfile,
  DecisionPage,
  Direction,
  ExecutionMode,
  FieldPreset,
  PerformanceRange,
  Profile,
  ProfileDraft,
  ProfileEstimate,
  ProfilePerformance,
  PresetStats,
  ProfileSummary,
  StopAllResult,
  Style,
} from "@/lib/autotrade/types";

/**
 * Auto-Trade workspace state (docs/ux/auto-trade-tab.md). Persisted: the view, the selected profile, the active tab
 * and the TOTP-free wizard draft. Server data is refetched on mount and never persisted; a failed refresh keeps the
 * last good data. A TOTP code is only ever a call argument.
 */
export type AutoTradeView = "list" | "new" | "profile";
export type ProfileTab = "timeline" | "performance" | "positions" | "settings";

export interface Loadable<T> {
  data: T | null;
  status: "idle" | "loading" | "ready" | "error";
  error: ApiError | null;
  updatedAt: number | null;
}

export interface CursorPage {
  items: DecisionPage["decisions"];
  nextCursor: string | null;
  hasMore: boolean;
  status: Loadable<unknown>["status"];
  error: ApiError | null;
  updatedAt: number | null;
}

const idle = <T,>(): Loadable<T> => ({ data: null, status: "idle", error: null, updatedAt: null });
const REFRESH_TIMEOUT_MS = 8_000;
const init = { timeoutMs: REFRESH_TIMEOUT_MS };

interface AutoTradeState {
  view: AutoTradeView;
  selectedProfileId: string | null;
  activeTab: ProfileTab;
  profiles: Profile[];
  listStatus: "idle" | "loading" | "ready" | "error";
  listError: ApiError | null;
  listUpdatedAt: number | null;
  summaryById: Record<string, Loadable<ProfileSummary>>;
  detailById: Record<string, Loadable<Profile>>;
  decisionsById: Record<string, CursorPage>;
  performanceById: Record<string, Loadable<ProfilePerformance>>;
  performanceRange: PerformanceRange;
  presets: Loadable<PresetStats>;
  estimate: Loadable<ProfileEstimate>;
  wizardDraft: ProfileDraft | null;
  mutationById: Record<string, "idle" | "pending" | "error">;

  setView(view: AutoTradeView): void;
  selectProfile(id: string | null): void;
  setActiveTab(tab: ProfileTab): void;
  setWizardDraft(draft: ProfileDraft | null): void;
  applyPreset(preset: FieldPreset): void;
  setPerformanceRange(range: PerformanceRange): void;
  loadProfiles(): Promise<void>;
  loadSummary(id: string): Promise<void>;
  /** All cards' summaries in one request; a failure keeps each card's last good summary, marked stale. */
  loadSummaries(): Promise<void>;
  loadDetail(id: string): Promise<void>;
  loadDecisions(id: string, opts?: { append?: boolean }): Promise<void>;
  loadPerformance(id: string, range?: PerformanceRange): Promise<void>;
  loadPresetStats(): Promise<void>;
  estimateProfile(opts: { coins: string[]; style: Style; direction: Direction }): Promise<void>;
  createProfile(draft: ProfileDraft, totp: string): Promise<CreatedProfile>;
  patchProfile(id: string, patch: Record<string, unknown>, totp?: string): Promise<Profile>;
  setMode(id: string, mode: ExecutionMode, totp?: string): Promise<void>;
  startProfile(id: string, totp?: string): Promise<Profile>;
  resumeProfile(id: string, totp: string): Promise<Profile>;
  pauseProfile(id: string): Promise<Profile>;
  stopProfile(id: string, close: boolean): Promise<Profile>;
  archiveProfile(id: string): Promise<Profile>;
  stopAllProfiles(close: boolean): Promise<StopAllResult>;
}

let estimateSeq = 0;

export const useAutoTradeStore = create<AutoTradeState>()(
  persist(
    (set, get) => {
      /** Run a write for one profile, mark it pending, then refresh that profile and the list. */
      const mutate = async <T,>(id: string, call: () => Promise<T>, change: Parameters<typeof dispatchMandateUpdated>[0]["change"]) => {
        set((s) => ({ mutationById: { ...s.mutationById, [id]: "pending" } }));
        try {
          const out = await call();
          set((s) => ({ mutationById: { ...s.mutationById, [id]: "idle" } }));
          dispatchMandateUpdated({ mandateId: id, change });
          void get().loadProfiles();
          if (get().summaryById[id]) void get().loadSummary(id);
          if (get().detailById[id]) void get().loadDetail(id);
          return out;
        } catch (err) {
          set((s) => ({ mutationById: { ...s.mutationById, [id]: "error" } }));
          throw api.toApiError(err);
        }
      };
      const loadInto = async <K extends "summaryById" | "detailById" | "performanceById", T>(
        key: K,
        id: string,
        fetcher: () => Promise<T>,
      ) => {
        set((s) => ({ [key]: { ...s[key], [id]: { ...(s[key][id] ?? idle<T>()), status: "loading" } } }) as Partial<AutoTradeState>);
        try {
          const data = await fetcher();
          set((s) => ({ [key]: { ...s[key], [id]: { data, status: "ready", error: null, updatedAt: Date.now() } } }) as Partial<AutoTradeState>);
        } catch (err) {
          // Keep the last good data: a failed refresh is shown as stale, never as empty
          set((s) => ({
            [key]: { ...s[key], [id]: { ...(s[key][id] ?? idle<T>()), status: "error", error: api.toApiError(err) } },
          }) as Partial<AutoTradeState>);
        }
      };

      return {
        view: "list",
        selectedProfileId: null,
        activeTab: "timeline",
        profiles: [],
        listStatus: "idle",
        listError: null,
        listUpdatedAt: null,
        summaryById: {},
        detailById: {},
        decisionsById: {},
        performanceById: {},
        performanceRange: "7d",
        presets: idle(),
        estimate: idle(),
        wizardDraft: null,
        mutationById: {},

        setView: (view) => set({ view }),
        selectProfile: (selectedProfileId) => set({ selectedProfileId }),
        setActiveTab: (activeTab) => set({ activeTab }),
        setWizardDraft: (wizardDraft) => set({ wizardDraft }),
        applyPreset: (preset) => set((s) => ({ wizardDraft: applyFieldPreset(s.wizardDraft ?? EMPTY_DRAFT, preset) })),
        setPerformanceRange: (performanceRange) => set({ performanceRange }),

        loadProfiles: async () => {
          set((s) => ({ listStatus: s.listUpdatedAt ? s.listStatus : "loading" }));
          try {
            const profiles = await api.listProfiles(init);
            set({ profiles, listStatus: "ready", listError: null, listUpdatedAt: Date.now() });
          } catch (err) {
            set({ listStatus: "error", listError: api.toApiError(err) });
          }
        },
        loadSummary: (id) => loadInto("summaryById", id, () => api.getSummary(id, init)),
        loadSummaries: async () => {
          try {
            const { summaries } = await api.getSummaries(init);
            const now = Date.now();
            set((s) => ({
              summaryById: {
                ...s.summaryById,
                ...Object.fromEntries(summaries.map((data) => [data.profile_id, { data, status: "ready" as const, error: null, updatedAt: now }])),
              },
            }));
          } catch (err) {
            const error = api.toApiError(err);
            // Mark the summaries the store already has as stale; never drop one (a detail page may be showing it)
            set((s) => ({
              summaryById: Object.fromEntries(
                Object.entries(s.summaryById).map(([id, entry]) => [id, { ...entry, status: "error" as const, error }]),
              ),
            }));
          }
        },
        loadDetail: (id) => loadInto("detailById", id, () => api.getProfile(id, init)),
        loadPerformance: (id, range) => loadInto("performanceById", id, () => api.getPerformance(id, range ?? get().performanceRange, init)),

        loadDecisions: async (id, { append = false } = {}) => {
          const prev = get().decisionsById[id];
          const cursor = append ? prev?.nextCursor ?? undefined : undefined;
          set((s) => ({
            decisionsById: {
              ...s.decisionsById,
              [id]: { items: prev?.items ?? [], nextCursor: prev?.nextCursor ?? null, hasMore: prev?.hasMore ?? false, error: null, updatedAt: prev?.updatedAt ?? null, status: "loading" },
            },
          }));
          try {
            const page = await api.getDecisions(id, cursor, init);
            set((s) => {
              const cur = s.decisionsById[id];
              // A poll refreshes the newest page; rows already paged in below it stay
              const items = append
                ? [...(cur?.items ?? []), ...page.decisions]
                : [...page.decisions, ...(cur?.items ?? []).filter((d) => !page.decisions.some((n) => n.id === d.id) && d.id < (page.decisions.at(-1)?.id ?? 0))];
              const keepCursor = !append && (cur?.items.length ?? 0) > page.decisions.length;
              return {
                decisionsById: {
                  ...s.decisionsById,
                  [id]: {
                    items,
                    nextCursor: keepCursor ? cur!.nextCursor : page.next_cursor,
                    hasMore: keepCursor ? cur!.hasMore : page.has_more,
                    status: "ready",
                    error: null,
                    updatedAt: Date.now(),
                  },
                },
              };
            });
          } catch (err) {
            set((s) => ({ decisionsById: { ...s.decisionsById, [id]: { ...s.decisionsById[id], status: "error", error: api.toApiError(err) } } }));
          }
        },

        loadPresetStats: async () => {
          if (get().presets.status === "ready") return;
          set((s) => ({ presets: { ...s.presets, status: "loading" } }));
          try {
            set({ presets: { data: await api.getPresetStats(init), status: "ready", error: null, updatedAt: Date.now() } });
          } catch (err) {
            set((s) => ({ presets: { ...s.presets, status: "error", error: api.toApiError(err) } }));
          }
        },

        estimateProfile: async ({ coins, style, direction }) => {
          const seq = ++estimateSeq;
          if (coins.length === 0) {
            set({ estimate: idle() });
            return;
          }
          set((s) => ({ estimate: { ...s.estimate, status: "loading" } }));
          try {
            const data = await api.getEstimate(coins, style, direction, init);
            if (seq === estimateSeq) set({ estimate: { data, status: "ready", error: null, updatedAt: Date.now() } });
          } catch (err) {
            if (seq === estimateSeq) set({ estimate: { data: null, status: "error", error: api.toApiError(err), updatedAt: null } });
          }
        },

        createProfile: async (draft, totp) => {
          try {
            const created = await api.createProfile(createBody(draft), totp);
            dispatchMandateUpdated({ mandateId: created.id, change: "created" });
            set({ wizardDraft: null });
            void get().loadProfiles();
            return created;
          } catch (err) {
            throw api.toApiError(err);
          }
        },
        patchProfile: (id, patch, totp) => mutate(id, () => api.patchProfile(id, patch, totp), "edited"),
        setMode: async (id, mode, totp) => {
          await mutate(id, () => api.setProfileMode(id, mode, totp), "promoted");
        },
        startProfile: (id, totp) => mutate(id, () => api.startProfile(id, totp), "resumed"),
        resumeProfile: (id, totp) => mutate(id, () => api.resumeProfile(id, totp), "resumed"),
        pauseProfile: (id) => mutate(id, () => api.pauseProfile(id), "paused"),
        stopProfile: (id, close) => mutate(id, () => api.stopProfile(id, close), "paused"),
        archiveProfile: (id) => mutate(id, () => api.archiveProfile(id), "edited"),
        stopAllProfiles: async (close) => {
          try {
            const result = await api.stopAllProfiles(close);
            for (const p of result.profiles) dispatchMandateUpdated({ mandateId: p.id, change: "paused" });
            await get().loadProfiles();
            return result;
          } catch (err) {
            throw api.toApiError(err);
          }
        },
      };
    },
    {
      name: "cryptonite-autotrade",
      skipHydration: true,
      partialize: (s) => ({ view: s.view, selectedProfileId: s.selectedProfileId, activeTab: s.activeTab, wizardDraft: s.wizardDraft }),
    },
  ),
);

export const selectProfileById = (s: AutoTradeState, id: string | null) => (id ? s.profiles.find((p) => p.id === id) ?? null : null);
export const selectProfileSummary = (s: AutoTradeState, id: string) => s.summaryById[id] ?? null;
export const selectProfileDecisions = (s: AutoTradeState, id: string) => s.decisionsById[id] ?? null;
export const selectProfilePerformance = (s: AutoTradeState, id: string) => s.performanceById[id] ?? null;
export const selectSelectedProfile = (s: AutoTradeState) =>
  s.selectedProfileId ? s.detailById[s.selectedProfileId]?.data ?? selectProfileById(s, s.selectedProfileId) : null;
export const selectWizardDraft = (s: AutoTradeState) => s.wizardDraft;
