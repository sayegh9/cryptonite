"use client";

import { useEffect } from "react";
import { RefreshCw } from "lucide-react";
import { BUILD_CHECK_MS, useBuildStore } from "@/lib/stores/useBuildStore";

/**
 * Asks the server which build it runs (every minute, on focus, when the tab becomes visible) and, once it differs from
 * the build this tab loaded, stays up until the operator reloads. The actions a stale tab may not take read
 * `useStaleBuild()` and are disabled meanwhile.
 */
export function StaleBuildBanner() {
  const stale = useBuildStore((s) => s.stale);
  const check = useBuildStore((s) => s.check);

  useEffect(() => {
    void check();
    const timer = window.setInterval(() => void check(), BUILD_CHECK_MS);
    const onFocus = () => void check();
    const onVisible = () => {
      if (document.visibilityState === "visible") void check();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [check]);

  if (!stale) return null;
  return (
    <div
      role="alert"
      data-testid="stale-build-banner"
      className="flex flex-none flex-wrap items-center gap-x-3 gap-y-1 border-b border-amber-700 bg-amber-950/60 px-4 py-1.5 text-2xs text-amber-200"
    >
      <RefreshCw className="h-3.5 w-3.5 flex-none text-amber-400" aria-hidden="true" />
      <p className="font-bold uppercase tracking-wide">A new version is live — reload</p>
      <p className="text-amber-200/80">Approving orders and changing mandates are off in this tab until it reloads.</p>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="ml-auto rounded bg-amber-600 px-2 py-0.5 font-bold text-zinc-950 hover:bg-amber-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400/60"
      >
        Reload
      </button>
    </div>
  );
}
