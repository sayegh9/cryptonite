import { create } from "zustand";

/** The build this tab's JavaScript came from (next.config.mjs bakes it into the bundle) */
export const LOADED_BUILD_ID = process.env.CRYPTONITE_BUILD_ID ?? "";

/** How often an open tab asks the server which build it runs (also on focus and when the tab becomes visible) */
export const BUILD_CHECK_MS = 60_000;

/** Shown on every action a stale tab may not take */
export const STALE_BUILD_TITLE = "A new version is live: reload this tab first";

/**
 * Whether this tab runs an older build than the server (2026-10-21: a tab loaded before a deploy kept the old order
 * card and created a perp mandate without `allow_perps`). While stale, the order-card Approve / Confirm, the approval
 * queue's Approve and every mandate change that needs TOTP are disabled until the tab reloads.
 *
 * Not persisted: it describes this tab's bundle, and a reload is exactly what clears it.
 */
interface BuildState {
  /** The build the server last reported, or null before the first answer */
  serverId: string | null;
  /** Sticky once seen: only a reload loads the new build */
  stale: boolean;
  /** Ask the server which build it runs. A failed request changes nothing. Concurrent callers share one request. */
  check: () => Promise<void>;
}

let inflight: Promise<void> | null = null;

export const useBuildStore = create<BuildState>()((set) => ({
  serverId: null,
  stale: false,
  check: () => {
    if (inflight) return inflight;
    inflight = fetch("/build-id", { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((body: { build_id?: unknown } | null) => {
        const id = typeof body?.build_id === "string" ? body.build_id : "";
        if (!id) return;
        set((s) => ({ serverId: id, stale: s.stale || (!!LOADED_BUILD_ID && id !== LOADED_BUILD_ID) }));
      })
      .catch(() => {
        // Offline or restarting: keep the last answer; the next poll asks again
      })
      .finally(() => {
        inflight = null;
      });
    return inflight;
  },
}));

/** True while this tab must reload before it approves an order or changes a mandate */
export const useStaleBuild = () => useBuildStore((s) => s.stale);

/** For submit handlers: the button is disabled, but a handler never runs on a stale tab either */
export const isStaleBuild = () => useBuildStore.getState().stale;
