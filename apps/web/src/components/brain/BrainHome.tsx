"use client";

import { PanelRightClose, PanelRightOpen, Plus } from "lucide-react";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { BrainContextRail } from "./BrainContextRail";
import { BrainConversation } from "./BrainConversation";

/** Brain Home (blueprint §7.1): one conversation with inline widgets, and a context rail. */
export function BrainHome({ active = true }: { active?: boolean }) {
  const railOpen = useBrainStore((s) => s.railOpen);
  const setRailOpen = useBrainStore((s) => s.setRailOpen);
  const newConversation = useBrainStore((s) => s.newConversation);
  const streaming = useBrainStore((s) => s.streaming);

  return (
    <div className="flex h-full min-h-0 w-full" data-testid="brain-home">
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex h-8 flex-none items-center justify-between border-b border-zinc-800 px-3 font-mono text-[10px] text-zinc-500">
          <span>Conversation · paper only · orders need your approval</span>
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
