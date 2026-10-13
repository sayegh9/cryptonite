import { useEffect } from "react";
import { create } from "zustand";
import { fetchApi } from "@/lib/api";
import { useSessionStore, type SessionStatus } from "@/lib/stores/useSessionStore";

/**
 * Kill-switch status per (environment, account), shared by every panel that shows it. One poller per key
 * however many panels are mounted (the Operator page, Mission Control, the Terminal, the Portfolio all
 * show it), so the server is not asked the same question five times every five seconds.
 */
export type KillSwitchEnvironment = "PAPER" | "DEMO";

export interface KillSwitchState {
  environment: string;
  account_id: string;
  is_latched: boolean;
  latch_generation: number;
  updated_at?: string;
}

export interface KillSwitchEntry {
  /** The last answer from the server, kept so the panel can say when it was last confirmed */
  status: KillSwitchState | null;
  /** The latest request failed or timed out. While set, the state is unknown, whatever `status` says. */
  error: string | null;
  /** When the latest successful answer arrived */
  updatedAt: number | null;
  /** A request is in flight */
  pending: boolean;
}

/** What a panel shows. `unknown` is never rendered as armed. */
export type KillSwitchView = "checking" | "signed-out" | "unknown" | "armed" | "latched";

export const KILL_SWITCH_POLL_MS = 5_000;
export const KILL_SWITCH_TIMEOUT_MS = 8_000;

const EMPTY_ENTRY: KillSwitchEntry = { status: null, error: null, updatedAt: null, pending: false };

const keyOf = (environment: string, accountId: string) => `${environment}|${accountId}`;

interface KillSwitchStore {
  entries: Record<string, KillSwitchEntry>;
  /** Fetch the status now. A request already in flight for the same key is not repeated. */
  refresh: (environment: string, accountId: string) => Promise<void>;
}

export const useKillSwitchStore = create<KillSwitchStore>()((set, get) => ({
  entries: {},
  refresh: async (environment, accountId) => {
    const key = keyOf(environment, accountId);
    const prev = get().entries[key] ?? EMPTY_ENTRY;
    if (prev.pending) return;
    set((s) => ({ entries: { ...s.entries, [key]: { ...prev, pending: true } } }));
    try {
      const qs = new URLSearchParams({ environment, account_id: accountId });
      const status = (await fetchApi(`/control/kill-switch/status?${qs}`, {
        timeoutMs: KILL_SWITCH_TIMEOUT_MS,
      })) as KillSwitchState;
      set((s) => ({
        entries: { ...s.entries, [key]: { status, error: null, updatedAt: Date.now(), pending: false } },
      }));
    } catch (err: any) {
      set((s) => ({
        entries: {
          ...s.entries,
          [key]: {
            ...(s.entries[key] ?? EMPTY_ENTRY),
            error: err?.timeout ? "The status request timed out." : err?.message || "Kill-switch status unavailable.",
            pending: false,
          },
        },
      }));
    }
  },
}));

// One interval per key, shared by every subscriber to that key
const pollers = new Map<string, { count: number; timer: ReturnType<typeof setInterval> }>();

function retainPoller(environment: string, accountId: string): () => void {
  const key = keyOf(environment, accountId);
  const refresh = () => void useKillSwitchStore.getState().refresh(environment, accountId);
  const poller = pollers.get(key);
  if (poller) {
    poller.count += 1;
  } else {
    pollers.set(key, { count: 1, timer: setInterval(refresh, KILL_SWITCH_POLL_MS) });
  }
  refresh();
  return () => {
    const current = pollers.get(key);
    if (!current) return;
    current.count -= 1;
    if (current.count <= 0) {
      clearInterval(current.timer);
      pollers.delete(key);
    }
  };
}

/**
 * The kill switch for one (environment, account). Polls unless the session is known to be signed out: the
 * endpoint is operator-only, so polling then would only produce refusals. While the session is still being
 * checked, the request itself decides, so a slow check never delays the status.
 */
export function useKillSwitch(environment: KillSwitchEnvironment, accountId: string) {
  const key = keyOf(environment, accountId);
  const entry = useKillSwitchStore((s) => s.entries[key]) ?? EMPTY_ENTRY;
  const session = useSessionStore((s) => s.status);

  useEffect(() => {
    if (session === "anonymous") return;
    return retainPoller(environment, accountId);
  }, [session, environment, accountId]);

  return {
    ...entry,
    view: killSwitchView(session, entry),
    refresh: () => useKillSwitchStore.getState().refresh(environment, accountId),
  };
}

/**
 * The one mapping from data to what a panel says. Anything other than a confirmed answer is unknown:
 * a failed request never leaves the panel showing the last armed state as if it were current.
 */
export function killSwitchView(session: SessionStatus, entry: KillSwitchEntry): KillSwitchView {
  if (session === "anonymous") return "signed-out";
  if (entry.error) return "unknown";
  if (!entry.status) return "checking";
  // A status without a boolean is_latched is not an answer (#70): it must never render as armed
  if (typeof entry.status.is_latched !== "boolean") return "unknown";
  return entry.status.is_latched ? "latched" : "armed";
}
