"use client";

import { useEffect, useState } from "react";
import { History } from "lucide-react";
import { listBrainSessions, openBrainSession } from "@/lib/brain/client";
import type { BrainSessionSummary } from "@/lib/brain/types";
import { useBrainStore } from "@/lib/stores/useBrainStore";

const RECENT = 3;

function when(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/**
 * The latest conversations on the empty Brain screen, so an earlier one can be picked up where it was left.
 * It is a shortcut, not the history: when the list cannot load it stays out of the way (History reports it).
 */
export function BrainRecentConversations({ onShowAll }: { onShowAll?: () => void }) {
  const [recent, setRecent] = useState<BrainSessionSummary[]>([]);
  const streaming = useBrainStore((s) => s.streaming);

  useEffect(() => {
    const ctrl = new AbortController();
    listBrainSessions({ limit: RECENT, signal: ctrl.signal })
      .then((page) => setRecent(page.sessions))
      .catch(() => setRecent([]));
    return () => ctrl.abort();
  }, []);

  if (recent.length === 0) return null;

  return (
    <section aria-label="Continue a conversation" className="mt-6 text-left" data-testid="brain-recent">
      <div className="mb-1.5 flex items-center justify-between">
        <h3 className="flex items-center gap-1.5 text-2xs text-zinc-400">
          <History className="h-3 w-3" aria-hidden="true" /> Continue a conversation
        </h3>
        {onShowAll && (
          <button type="button" onClick={onShowAll} className="text-2xs text-cyan-400 hover:text-cyan-300">
            All conversations
          </button>
        )}
      </div>
      <ul className="divide-y divide-zinc-800/80 rounded border border-zinc-800">
        {recent.map((s) => (
          <li key={s.id}>
            <button
              type="button"
              disabled={streaming}
              onClick={() => void openBrainSession(s.id)}
              className="flex w-full items-baseline justify-between gap-3 px-3 py-2 text-left hover:bg-zinc-900 disabled:opacity-50"
            >
              <span className="min-w-0">
                <span className="block truncate text-xs text-zinc-200">{s.title || "Untitled"}</span>
                {s.preview && <span className="block truncate text-2xs text-zinc-500">{s.preview.content}</span>}
              </span>
              <time dateTime={s.updated_at} className="flex-none font-mono text-2xs tabular-nums text-zinc-500">
                {when(s.updated_at)}
              </time>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
