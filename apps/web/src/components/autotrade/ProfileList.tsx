"use client";

import { errorCopy } from "@/lib/autotrade/copy";
import { useAutoTradeStore } from "@/lib/stores/useAutoTradeStore";
import { btnPrimary, btnSecondary } from "./Dialogs";
import { ProfileCard } from "./ProfileCard";
import { StopAllButton } from "./StopAllButton";

export function ProfileList({ onCreate, onOpen }: { onCreate: () => void; onOpen: (id: string) => void }) {
  const profiles = useAutoTradeStore((s) => s.profiles);
  const status = useAutoTradeStore((s) => s.listStatus);
  const error = useAutoTradeStore((s) => s.listError);
  const updatedAt = useAutoTradeStore((s) => s.listUpdatedAt);
  const summaries = useAutoTradeStore((s) => s.summaryById);
  const reload = useAutoTradeStore((s) => s.loadProfiles);
  const denied = error?.status === 401 || error?.status === 403;

  return (
    <section aria-labelledby="autotrade-heading" className="space-y-4">
      <header className="flex flex-wrap items-center gap-3">
        <h2 id="autotrade-heading" tabIndex={-1} className="mr-auto text-lg font-bold text-zinc-100 outline-none">
          Auto-Trade
        </h2>
        <button type="button" className={btnPrimary} onClick={onCreate}>
          Create profile
        </button>
        <StopAllButton profiles={profiles} />
      </header>

      {status === "loading" && updatedAt == null && (
        <div role="status" className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <span className="sr-only">Loading profiles…</span>
          {[0, 1].map((i) => (
            <div key={i} className="h-56 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900/60 motion-reduce:animate-none" aria-hidden="true" />
          ))}
        </div>
      )}

      {status === "error" && updatedAt == null && (
        <div role="alert" className="rounded-xl border border-amber-800/60 bg-amber-950/20 p-4 text-sm">
          <p className="font-bold text-amber-200">Profiles unavailable</p>
          <p className="mt-1 text-amber-100/90">
            {denied ? "Sign in as an operator to manage profiles." : "We couldn't load your profiles. Check your connection and try again."}
          </p>
          <button
            type="button"
            className={`${btnSecondary} mt-3`}
            onClick={() => (denied ? window.dispatchEvent(new CustomEvent("cryptonite:open-login")) : void reload())}
          >
            {denied ? "Sign in" : "Retry"}
          </button>
        </div>
      )}

      {status === "error" && updatedAt != null && (
        <p role="status" className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-800/60 bg-amber-950/20 px-3 py-2 text-xs text-amber-200">
          Couldn&apos;t refresh profiles. Showing the last loaded data (loaded {new Date(updatedAt).toLocaleTimeString()}).
          {error && !denied ? ` ${errorCopy(error)}` : ""}
          <button type="button" className="underline" onClick={() => void reload()}>
            Retry
          </button>
        </p>
      )}

      {updatedAt != null && profiles.length === 0 && (
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/40 p-6 text-center">
          <p className="text-base font-bold text-zinc-100">No profiles yet</p>
          <p className="mt-1 text-sm text-zinc-400">Create a paper profile to set its markets, risk limits, and trading mode.</p>
          <button type="button" className={`${btnPrimary} mt-4`} onClick={onCreate}>
            Create profile
          </button>
        </div>
      )}

      {profiles.length > 0 && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {profiles.map((p) => (
            <ProfileCard key={p.id} profile={p} summary={summaries[p.id]?.data ?? null} onOpen={() => onOpen(p.id)} />
          ))}
        </div>
      )}
    </section>
  );
}
