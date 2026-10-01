"use client";

import { useEffect, useRef } from "react";
import { BrainCircuit, FileText, Maximize2, Plus, X } from "lucide-react";
import { BrainConversation } from "@/components/brain/BrainConversation";
import { dispatchBrainAsk } from "@/lib/brain/events";
import { useBrainStore } from "@/lib/stores/useBrainStore";

/**
 * The Copilot drawer: the same Brain conversation as Brain Home, reachable from every workspace.
 * (Blueprint A3.4: replaces the old persona picker; the personas are now Brain tools.)
 */
export function AgentChatPanel({
  isOpen,
  onClose,
  activeBacktestId,
  onExpand,
}: {
  isOpen: boolean;
  onClose: () => void;
  activeBacktestId?: string | null;
  onExpand?: () => void;
}) {
  const newConversation = useBrainStore((s) => s.newConversation);
  const streaming = useBrainStore((s) => s.streaming);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !useBrainStore.getState().traceDrawerId) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-label="Brain copilot"
      data-testid="brain-copilot"
      className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col border-l border-zinc-800 bg-zinc-950 shadow-2xl shadow-black/80"
    >
      <header className="flex flex-none items-center justify-between border-b border-zinc-800 bg-zinc-900/90 px-4 py-3">
        <div className="flex items-center gap-2">
          <BrainCircuit className="h-5 w-5 text-cyan-400" />
          <h2 className="font-mono text-sm font-bold uppercase tracking-wider text-zinc-100">Brain</h2>
          <span className="hidden font-mono text-[10px] text-zinc-500 sm:inline">one conversation · paper</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={streaming}
            onClick={newConversation}
            className="flex items-center gap-1 rounded px-2 py-1 font-mono text-[10px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
          >
            <Plus className="h-3 w-3" /> New
          </button>
          {onExpand && (
            <button
              type="button"
              onClick={onExpand}
              className="rounded p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
              aria-label="Open Brain Home"
              title="Open Brain Home"
            >
              <Maximize2 className="h-3.5 w-3.5" />
            </button>
          )}
          <button type="button" onClick={onClose} className="rounded p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100" aria-label="Close Brain copilot">
            <X className="h-4 w-4" />
          </button>
        </div>
      </header>

      {activeBacktestId && (
        <div className="flex flex-none items-center justify-between gap-2 border-b border-zinc-800 px-4 py-2 font-mono text-[10px] text-zinc-400">
          <span className="truncate">Active backtest {activeBacktestId}</span>
          <button
            type="button"
            onClick={() => dispatchBrainAsk({ prompt: `Explain backtest ${activeBacktestId}: return, drawdown, trades and what to improve.` })}
            className="flex flex-none items-center gap-1 rounded border border-amber-800 px-1.5 py-0.5 text-amber-300 hover:bg-amber-950/40"
          >
            <FileText className="h-3 w-3" /> Explain it
          </button>
        </div>
      )}

      <div className="min-h-0 flex-1">
        <BrainConversation compact />
      </div>
    </div>
  );
}
