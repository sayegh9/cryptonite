"use client";

import { useEffect, useState } from "react";
import { usePortfolioStore } from "@/lib/stores/usePortfolioStore";

/** Data-age indicator. Owns its own 1 s clock so the tables never re-render just to age a label. */
export function FreshnessBadge() {
  const lastSnapshotAt = usePortfolioStore((s) => s.lastSnapshotAt);
  const lastRestOkAt = usePortfolioStore((s) => s.lastRestOkAt);
  const degraded = usePortfolioStore((s) => s.restDegraded);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const last = Math.max(lastSnapshotAt ?? 0, lastRestOkAt ?? 0);
  const age = last ? Math.max(0, Math.round((now - last) / 1000)) : null;
  const streaming = lastSnapshotAt !== null && now - lastSnapshotAt < 5000;
  const stale = age === null || age > 10;

  const [dot, label] = streaming
    ? ["bg-emerald-400", "Live 1s mark-to-market"]
    : stale
    ? ["bg-red-400", age === null ? "Connecting" : `Stale · ${age}s`]
    : ["bg-amber-400", `Polling · ${age}s ago`];

  return (
    <span className="flex items-center gap-1.5 text-[10px] font-normal normal-case tracking-normal text-zinc-500" role="status">
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
      {degraded && !stale && <span className="text-amber-400/80" title="Some endpoints failed on the last refresh; showing last known data">· partial</span>}
    </span>
  );
}
