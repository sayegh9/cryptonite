"use client";

import { History, PanelRightClose, PanelRightOpen, Plus } from "lucide-react";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { BrainContextRail } from "./BrainContextRail";
import { BrainHistoryPanel } from "./BrainHistoryPanel";
import { BrainConversation } from "./BrainConversation";

/** Brain Home (blueprint §7.1): one conversation with inline widgets, its history, and a context rail. */
export function BrainHome({ active = true }: { active?: boolean }) {
  const railOpen = useBrainStore((s) => s.railOpen);
  const historyOpen = useBrainStore((s) => s.historyOpen);
  const setHistoryOpen = useBrainStore((s) => s.setHistoryOpen);
  const setRailOpen = useBrainStore((s) => s.setRailOpen);
  const newConversation = useBrainStore((s) => s.newConversation);
  const streaming = useBrainStore((s) => s.streaming);

  return (
    <div className="relative flex h-full min-h-0 w-full" data-testid="brain-home">
      {historyOpen && active && ( /* the history loads only while Brain Home is on screen */
        <>
          <div className="fixed inset-0 z-30 bg-black/60 md:hidden" onClick={() => setHistoryOpen(false)} aria-hidden="true" />
          <div className="fixed inset-y-0 left-0 z-40 w-[85vw] max-w-xs border-r border-zinc-800 md:static md:z-auto md:w-72 md:max-w-none md:flex-none">
            <BrainHistoryPanel
              onClose={() => setHistoryOpen(false)}
              onOpened={() => {
                // On a phone the drawer covers the conversation: get out of the way once one is chosen
                if (window.matchMedia("(max-width: 767px)").matches) setHistoryOpen(false);
              }}
            />
          </div>
        </>
      )}
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex h-8 flex-none items-center justify-between border-b border-zinc-800 px-3 font-mono text-2xs text-zinc-500">
          <div className="flex min-w-0 items-center gap-2">
            <button
              type="button"
              onClick={() => setHistoryOpen(!historyOpen)}
              aria-pressed={historyOpen}
              className={`flex items-center gap-1 rounded border px-1.5 py-0.5 ${historyOpen ? "border-cyan-800 text-cyan-300" : "border-zinc-800 text-zinc-400 hover:text-zinc-100"}`}
            >
              <History className="h-3 w-3" aria-hidden="true" /> History
            </button>
            <span className="truncate">Conversation · paper only · orders need your approval</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={streaming}
              onClick={newConversation}
              className="flex items-center gap-1 rounded border border-zinc-800 px-1.5 py-0.5 text-zinc-400 hover:text-zinc-100 disabled:opacity-40"
            >
              <Plus className="h-3 w-3" /> New
            </button>
            <button
              type="button"
              onClick={() => setRailOpen(!railOpen)}
              className="text-zinc-400 hover:text-zinc-100"
              aria-label={railOpen ? "Hide context rail" : "Show context rail"}
              aria-pressed={railOpen}
            >
              {railOpen ? <PanelRightClose className="h-3.5 w-3.5" /> : <PanelRightOpen className="h-3.5 w-3.5" />}
            </button>
          </div>
        </div>
        <div className="mx-auto flex min-h-0 w-full max-w-3xl flex-1 flex-col">
          <BrainConversation />
        </div>
      </main>
      {railOpen && active && ( /* the rail polls positions/spend only while Brain Home is on screen */
        <div className="hidden w-64 flex-none border-l border-zinc-800 md:block xl:w-72">
          <BrainContextRail />
        </div>
      )}
    </div>
  );
}
