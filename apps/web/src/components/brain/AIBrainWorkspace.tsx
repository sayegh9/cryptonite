"use client";

import React, { useState, useEffect } from "react";
import { AgentFleetRoster, TriggerMode } from "./AgentFleetRoster";
import { OrchestratorCanvas } from "./OrchestratorCanvas";
import { ActionInbox } from "./ActionInbox";
import { AIBrainTourModal } from "./AIBrainTourModal";
import { useDebateStore } from "@/lib/stores/useDebateStore";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { BrainHome } from "./BrainHome";
import {
  BrainCircuit,
  HelpCircle,
  ShieldCheck,
  Zap,
  Users,
  Inbox,
  MessagesSquare,
  GitBranch,
} from "lucide-react";
import { toast } from "sonner";

interface AIBrainWorkspaceProps {
  selectedSymbol: string;
  isActive?: boolean;
}

export function AIBrainWorkspace({ selectedSymbol, isActive = false }: AIBrainWorkspaceProps) {
  const [triggerMode, setTriggerMode] = useState<TriggerMode>("high-edge");
  const [selectedAgentId, setSelectedAgentId] = useState<string>("orchestrator");
  const [isTourOpen, setIsTourOpen] = useState(false);
  const view = useBrainStore((s) => s.workspaceView);
  const setView = useBrainStore((s) => s.setWorkspaceView);

  // Responsive panel visibility with local persistence
  const [showFleet, setShowFleet] = useState<boolean>(true);
  const [showInbox, setShowInbox] = useState<boolean>(true);

  const activeDebates = useDebateStore((state) => state.activeDebates);
  const activeDebatesCount = Object.keys(activeDebates).length;

  // Restore panel preferences & auto-open tour for first-time user
  useEffect(() => {
    try {
      const savedFleet = localStorage.getItem("cryptonite:ai-brain-fleet");
      if (savedFleet !== null) setShowFleet(savedFleet === "true");

      const savedInbox = localStorage.getItem("cryptonite:ai-brain-inbox");
      if (savedInbox !== null) setShowInbox(savedInbox === "true");

      if (isActive && useBrainStore.getState().workspaceView === "debates") {
        const tourSeen = localStorage.getItem("cryptonite:ai-brain-tour-seen");
        if (!tourSeen) {
          setIsTourOpen(true);
        }
      }
    } catch {}
  }, [isActive, view]);

  const toggleFleet = (val: boolean) => {
    setShowFleet(val);
    try {
      localStorage.setItem("cryptonite:ai-brain-fleet", String(val));
    } catch {}
  };

  const toggleInbox = (val: boolean) => {
    setShowInbox(val);
    try {
      localStorage.setItem("cryptonite:ai-brain-inbox", String(val));
    } catch {}
  };

  // Queues a real debate; nothing is added to the inbox until the worker reports the run
  const handleTriggerLiveDebate = async (symbol: string) => {
    const sym = symbol || selectedSymbol || "BTCUSDT";
    try {
      const res = await fetch("/agent-api/debates/trigger", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": typeof window !== "undefined" ? localStorage.getItem("csrf_token") || "" : "",
        },
        body: JSON.stringify({ symbol: sym, timeframe: "1m" }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail = typeof body.detail === "string" ? body.detail : typeof body.error === "string" ? body.error : "";
        throw new Error(res.status === 401 || res.status === 403 ? "Session expired, sign in again" : detail || `HTTP ${res.status}`);
      }
      if (!showInbox) toggleInbox(true);
      toast.success(`Debate queued: ${sym}`, {
        description: "Its card appears in the Action Inbox when the debate worker reports it.",
      });
    } catch (err) {
      toast.error(`Debate not queued: ${sym}`, { description: err instanceof Error ? err.message : "Network error" });
    }
  };

  return (
    <div className="flex flex-col h-full w-full bg-zinc-950 overflow-hidden font-sans">
      {/* Top AI Brain Bar - Responsive & High Contrast */}
      <div className="h-10 flex-none bg-zinc-950 border-b border-zinc-800 px-3 sm:px-4 flex flex-wrap items-center justify-between gap-2 z-10">
        <div className="flex items-center space-x-2 sm:space-x-3 min-w-0">
          <div className="flex items-center space-x-1.5 text-xs font-mono font-bold text-zinc-100 flex-none">
            <BrainCircuit className="w-4 h-4 text-cyan-400 flex-none" />
            <span className="uppercase tracking-widest text-cyan-400">AI Brain Hub</span>
            <span className="text-zinc-600 hidden sm:inline">/</span>
            <span className="text-zinc-400 hidden sm:inline">Neural Operations</span>
          </div>

          <div className="flex items-center space-x-1 px-2 py-0.5 rounded bg-emerald-950/60 border border-emerald-800/60 text-[10px] font-mono text-emerald-400 flex-none">
            <ShieldCheck className="w-3 h-3 text-emerald-400 flex-none" />
            <span className="font-bold">SIMULATION MODE (RISK-FREE)</span>
          </div>

          <div role="group" aria-label="AI Brain view" className="flex items-center rounded border border-zinc-800 bg-zinc-900 p-0.5 text-[11px] font-mono flex-none">
            {([
              { id: "brain", label: "Brain", Icon: MessagesSquare },
              { id: "debates", label: "Debates", Icon: GitBranch },
            ] as const).map(({ id, label, Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() => setView(id)}
                aria-pressed={view === id}
                className={`flex items-center gap-1 rounded px-2 py-0.5 transition-colors ${
                  view === id ? "bg-cyan-950/80 text-cyan-300 font-bold" : "text-zinc-400 hover:text-zinc-200"
                }`}
              >
                <Icon className="w-3 h-3" />
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* Panel Toggles & Actions (Debates view) */}
        {view === "debates" && (
        <div className="flex items-center space-x-2 flex-none">
          {/* Toggle Left Rail: Fleet Roster */}
          <button
            onClick={() => toggleFleet(!showFleet)}
            className={`px-2 py-1 rounded border text-[11px] font-mono transition-colors flex items-center space-x-1 ${
              showFleet
                ? "bg-cyan-950/60 border-cyan-800/80 text-cyan-300 font-bold"
                : "bg-zinc-900 border-zinc-800 text-zinc-400 hover:text-zinc-200"
            }`}
            title={showFleet ? "Hide Agent Fleet Panel" : "Show Agent Fleet Panel"}
          >
            <Users className="w-3 h-3" />
            <span className="hidden md:inline">Fleet</span>
          </button>

          {/* Toggle Right Rail: Action Inbox */}
          <button
            onClick={() => toggleInbox(!showInbox)}
            className={`px-2 py-1 rounded border text-[11px] font-mono transition-colors flex items-center space-x-1 ${
              showInbox
                ? "bg-emerald-950/60 border-emerald-800/80 text-emerald-300 font-bold"
                : "bg-zinc-900 border-zinc-800 text-zinc-400 hover:text-zinc-200"
            }`}
            title={showInbox ? "Hide Action Inbox" : "Show Action Inbox"}
          >
            <Inbox className="w-3 h-3" />
            <span className="hidden md:inline">Inbox</span>
            {activeDebatesCount > 0 && (
              <span className="ml-1 px-1 py-0.2 bg-emerald-500 text-zinc-950 rounded-full text-[9px] font-black">
                {activeDebatesCount}
              </span>
            )}
          </button>

          {/* Trigger Debate Action Button */}
          <button
            onClick={() => handleTriggerLiveDebate(selectedSymbol)}
            className="flex items-center space-x-1.5 px-2.5 py-1 bg-purple-950/80 hover:bg-purple-900 border border-purple-800 rounded text-[11px] font-mono font-bold text-purple-300 transition-colors"
            title="Dispatch an adversarial debate on active symbol"
          >
            <Zap className="w-3 h-3 text-purple-400 flex-none" />
            <span>⚡ Trigger {selectedSymbol || "BTCUSDT"} Debate</span>
          </button>

          {/* How It Works Button */}
          <button
            onClick={() => setIsTourOpen(true)}
            className="flex items-center space-x-1 px-2 py-1 bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-300 hover:text-white rounded text-[11px] font-mono transition-colors"
          >
            <HelpCircle className="w-3.5 h-3.5 text-cyan-400 flex-none" />
            <span className="hidden sm:inline">❓ How It Works</span>
          </button>
        </div>
        )}
      </div>

      {view === "brain" && (
        <div className="flex-1 min-h-0 min-w-0 flex">
          <BrainHome active={isActive} />
        </div>
      )}

      {/* 3-Column Robust Responsive Layout (Debates view) */}
      <div className={view === "debates" ? "flex-1 flex min-h-0 min-w-0 bg-zinc-950 overflow-hidden" : "hidden"}>
        {/* Zone 1: Agent Fleet Topology (Left Rail) */}
        {showFleet && (
          <aside className="w-64 xl:w-72 flex-none h-full border-r border-zinc-800 bg-zinc-950 flex flex-col min-h-0 min-w-0 z-10 transition-all">
            <AgentFleetRoster
              selectedAgentId={selectedAgentId}
              onSelectAgent={setSelectedAgentId}
              triggerMode={triggerMode}
              onTriggerModeChange={setTriggerMode}
              activeDebateCount={activeDebatesCount}
            />
          </aside>
        )}

        {/* Zone 2: Lead Orchestrator Canvas (Center Main Canvas) */}
        <main className="flex-1 flex flex-col min-h-0 min-w-0 bg-zinc-950 relative">
          <OrchestratorCanvas
            selectedAgentId={selectedAgentId}
            onSelectAgent={setSelectedAgentId}
            activeSymbol={selectedSymbol || "BTCUSDT"}
            activeTimeframe="1m"
            onTriggerDebate={handleTriggerLiveDebate}
          />
        </main>

        {/* Zone 3: Action & Decision Inbox (Right Rail) */}
        {showInbox && (
          <aside className="w-72 xl:w-80 flex-none h-full border-l border-zinc-800 bg-zinc-950 flex flex-col min-h-0 min-w-0 z-10 transition-all">
            <ActionInbox onTriggerDebate={() => handleTriggerLiveDebate(selectedSymbol)} symbol={selectedSymbol || "BTCUSDT"} />
          </aside>
        )}
      </div>

      {/* Interactive Onboarding Tour Modal */}
      <AIBrainTourModal
        isOpen={isTourOpen}
        onClose={() => setIsTourOpen(false)}
        onRunDebate={() => handleTriggerLiveDebate(selectedSymbol)}
        symbol={selectedSymbol || "BTCUSDT"}
      />
    </div>
  );
}
