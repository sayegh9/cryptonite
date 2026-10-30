"use client";

import { Activity, Ban, Bell, CircleDot, Gavel, Layers, LogOut, MessagesSquare, Power, Route, ShoppingCart, type LucideIcon } from "lucide-react";
import { coinOf, errorCopy } from "@/lib/autotrade/copy";
import { useAutoTradeStore } from "@/lib/stores/useAutoTradeStore";
import { useVisiblePoll } from "../AutoTradeWorkspace";
import { btnSecondary } from "../Dialogs";

const STAGE_ICON: Record<string, LucideIcon> = {
  GATE: Gavel,
  DEBATE: MessagesSquare,
  ROUTE: Route,
  APPROVAL: Bell,
  ORDER: ShoppingCart,
  FILL: Layers,
  EXIT: LogOut,
  LIFECYCLE: Power,
  BREAKER: Ban,
  SESSION: Activity,
};

/** Newest first, 50 at a time. GATE: REFUSED and ROUTE: NO_TRADE are policy decisions, shown plainly, never as errors. */
export function TimelineTab({ id }: { id: string }) {
  const page = useAutoTradeStore((s) => s.decisionsById[id]);
  const load = useAutoTradeStore((s) => s.loadDecisions);
  useVisiblePoll(() => void load(id), 5_000);

  if (!page || (page.status === "loading" && page.items.length === 0 && page.updatedAt == null))
    return <p role="status" className="text-sm text-zinc-400">Loading timeline…</p>;
  if (page.status === "error" && page.items.length === 0)
    return (
      <div role="alert" className="space-y-2 text-sm">
        <p className="font-bold text-amber-200">Timeline unavailable.</p>
        {page.error && <p className="text-zinc-400">{errorCopy(page.error)}</p>}
        <button type="button" className={btnSecondary} onClick={() => void load(id)}>
          Retry
        </button>
      </div>
    );
  if (page.items.length === 0) return <p className="text-sm text-zinc-400">No decisions yet.</p>;

  return (
    <div className="space-y-3">
      {page.status === "error" && (
        <p role="status" className="text-xs text-amber-200">
          Couldn&apos;t refresh the timeline. Showing the last loaded decisions.{" "}
          <button type="button" className="underline" onClick={() => void load(id)}>
            Retry
          </button>
        </p>
      )}
      <ol className="divide-y divide-zinc-800 rounded-xl border border-zinc-800" aria-label="Decisions, newest first">
        {page.items.map((d) => {
          const Icon = STAGE_ICON[d.stage] ?? CircleDot;
          return (
            <li key={d.id} className="flex flex-wrap items-start gap-x-3 gap-y-1 px-3 py-2 text-xs sm:flex-nowrap">
              <time dateTime={d.ts} className="w-36 shrink-0 font-mono text-zinc-500">
                {new Date(d.ts).toLocaleString()}
              </time>
              <span className="inline-flex w-28 shrink-0 items-center gap-1.5 font-semibold text-zinc-200">
                <Icon className="h-3.5 w-3.5" aria-hidden="true" />
                {d.stage}
              </span>
              <span className="w-32 shrink-0 text-zinc-100">{d.outcome.replaceAll("_", " ")}</span>
              <span className="w-16 shrink-0 text-zinc-300">{d.instrument_id ? coinOf(d.instrument_id) : "—"}</span>
              <span className="min-w-0 flex-1 break-words text-zinc-400">{d.reason ?? ""}</span>
            </li>
          );
        })}
      </ol>
      {page.hasMore && (
        <button type="button" className={btnSecondary} disabled={page.status === "loading"} onClick={() => void load(id, { append: true })}>
          {page.status === "loading" ? "Loading…" : "Load older decisions"}
        </button>
      )}
    </div>
  );
}
