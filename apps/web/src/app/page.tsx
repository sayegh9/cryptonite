"use client";

import React, { Suspense, useState, useEffect, useCallback, useRef } from "react";
import { useSearchParams } from "next/navigation";
import { buildQuery, DEFAULT_WORKSPACE, parseView, parseWorkspace, type WorkspaceMode } from "@/lib/navigation";
import { type MissionView, useMandateStore } from "@/lib/stores/useMandateStore";
import { MultiChartGrid } from "@/components/MultiChartGrid";
import { WatchlistPanel } from "@/components/WatchlistPanel";
import { ScannerStudio } from "@/components/ScannerStudio";
import { KillSwitchPanel } from "@/components/operator/KillSwitchPanel";
import { FlattenPerpsButton } from "@/components/FlattenPerpsButton";
import { EnforcedLimitsCard } from "@/components/operator/EnforcedLimitsCard";
import { StrategyInstancesPanel } from "@/components/operator/StrategyInstancesPanel";
import { ReconciliationLedger } from "@/components/operator/ReconciliationLedger";
import { OperatorLoginModal } from "@/components/operator/OperatorLoginModal";
import { AgentChatPanel } from "@/components/AgentChatPanel";
import { ScannerBuilder } from "@/components/ScannerBuilder";
import { CoinProfileModal } from "@/components/CoinProfileModal";
import { AlertsPanel } from "@/components/AlertsPanel";
import { MarketOverview } from "@/components/MarketOverview";
import { SettingsPanel } from "@/components/SettingsPanel";
import { AIBrainWorkspace } from "@/components/brain/AIBrainWorkspace";
import { DecisionTraceDrawer } from "@/components/brain/DecisionTraceDrawer";
import { GovernanceConsole } from "@/components/GovernanceConsole";
import { MissionControl } from "@/components/mission/MissionControl";
import { ModeBanner } from "@/components/mission/ModeBanner";
import { StaleBuildBanner } from "@/components/StaleBuildBanner";
import { WorkspaceNav } from "@/components/WorkspaceNav";
import { dispatchBrainAsk } from "@/lib/brain/events";
import { useBrainSetup } from "@/lib/brain/useBrainSetup";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { useSessionStore } from "@/lib/stores/useSessionStore";
import { useDebateStream, useDebateStore } from "@/lib/stores/useDebateStore";
import { usePerpetualStream } from "@/lib/stores/usePerpetualStore";
import { TerminalWorkspace } from "@/components/terminal/TerminalWorkspace";
import { DebateTranscriptViewer } from "@/components/debate/DebateTranscriptViewer";

import { ResearchLab } from "@/components/ResearchLab/ResearchLab";
import { StrategyStudio } from "@/components/StrategyStudio";
import { useBacktestStore } from "@/lib/stores/useBacktestStore";
import { TradesConsole } from "@/components/TradesConsole";
import { getStreamClient } from "@/lib/ws";
import { toast } from "sonner";
import {
  Radio,
  BrainCircuit,
  Search,
} from "lucide-react";
import { useDeskPreferences, applyDeskDOMPreferences } from "@/lib/stores/useDeskPreferences";


// useSearchParams in a prerendered page must sit under a Suspense boundary (or `pnpm build` fails)
export default function TerminalPage() {
  return (
    <Suspense fallback={null}>
      <Workstation />
    </Suspense>
  );
}

function Workstation() {
  // R6.1: the URL is the source of truth for the workspace and its sub-view (?ws=…&view=…)
  const searchParams = useSearchParams();
  const workspaceMode: WorkspaceMode = parseWorkspace(searchParams.get("ws")) ?? DEFAULT_WORKSPACE;
  const urlView = parseView(workspaceMode, searchParams.get("view"));
  const navigate = useCallback((mode: WorkspaceMode, view?: string | null) => {
    // Workspaces with sub-views always carry one, so Back restores the exact view
    if (!view && mode === "debates") view = useBrainStore.getState().workspaceView === "debates" ? "debates" : "home";
    if (!view && (mode === "mission" || mode === "governance")) view = useMandateStore.getState().view;
    const query = buildQuery(mode, view);
    if (query !== window.location.search) window.history.pushState(null, "", query);
  }, []);
  const setWorkspaceMode = useCallback((mode: WorkspaceMode) => navigate(mode), [navigate]);

  // Apply desk DOM preferences (theme, density) on mount and subscribe to store changes
  useEffect(() => {
    applyDeskDOMPreferences(useDeskPreferences.getState());
    return useDeskPreferences.subscribe((state) => {
      applyDeskDOMPreferences(state);
    });
  }, []);

  // URL view -> stores (also after their persisted state rehydrates, so a stored view never overrides the URL)
  const urlViewRef = useRef({ mode: workspaceMode, view: urlView });
  urlViewRef.current = { mode: workspaceMode, view: urlView };

  useEffect(() => {
    const apply = () => {
      const { mode, view } = urlViewRef.current;
      if (!view) return;
      // A set before rehydration would persist the defaults over the stored session; wait for it
      if (mode === "debates" && useBrainStore.persist.hasHydrated()) {
        useBrainStore.getState().setWorkspaceView(view === "home" ? "brain" : "debates");
      }
      if (mode === "mission" && useMandateStore.persist.hasHydrated()) useMandateStore.getState().setView(view as MissionView);
    };
    apply();
    const unsubBrain = useBrainStore.persist.onFinishHydration(apply);
    const unsubMission = useMandateStore.persist.onFinishHydration(apply);
    return () => {
      unsubBrain();
      unsubMission();
    };
  }, [workspaceMode, urlView]);

  // Terminal "Scanner" view: bring the scanner panel into view
  useEffect(() => {
    if (workspaceMode !== "terminal" || urlView !== "scanner") return;
    const t = setTimeout(() => document.getElementById("scanner-studio")?.scrollIntoView({ block: "nearest" }), 50);
    return () => clearTimeout(t);
  }, [workspaceMode, urlView]);

  const [isAgentChatOpen, setIsAgentChatOpen] = useState(false);
  // One session answer for the whole app (see useSessionStore): "checking" is not "signed out"
  const sessionStatus = useSessionStore((s) => s.status);
  const [showScannerBuilder, setShowScannerBuilder] = useState(false);
  const [selectedSymbol, setSelectedSymbol] = useState<string>("BTCUSDT");
  const [activeChartTarget, setActiveChartTarget] = useState<{
    symbol: string;
    timeframe: string;
    candleTime: number;
  } | null>(null);
  const [wsConnected, setWsConnected] = useState<boolean>(false);
  const [streamOpen, setStreamOpen] = useState<boolean>(false);
  const [profileSymbol, setProfileSymbol] = useState<string | null>(null);

  // Brain: restore the session and route cryptonite:brain-ask / cryptonite:brain-open-trace.
  // An ask from another workspace opens the Copilot drawer; inside the AI Brain workspace it goes to Brain Home.
  const workspaceModeRef = useRef(workspaceMode);
  workspaceModeRef.current = workspaceMode;
  const onBrainAsk = useCallback(() => {
    if (workspaceModeRef.current === "debates") navigate("debates", "home");
    else setIsAgentChatOpen(true);
  }, [navigate]);
  useBrainSetup(onBrainAsk);

  useDebateStream(wsConnected ? getStreamClient() : null);
  usePerpetualStream(wsConnected);

  const activeDebates = useDebateStore((state) => state.activeDebates);
  const debatesList = Object.values(activeDebates);
  const activeDebateId = debatesList.length > 0 ? debatesList[0].runId : null;

  const activeBacktestRun = useBacktestStore((state) => state.activeRun);

  // Verify the operator session once on load; every component reads the answer from the session store
  useEffect(() => {
    void useSessionStore.getState().check();
  }, []);

  // Initialize global WebSocket stream connection
  useEffect(() => {
    const client = getStreamClient();
    client.connect({
      channels: ["open_kline", "closed_bar_committed", "signal_candidate"],
      instruments: [],
    });

    const unsubscribe = client.subscribe((msg) => {
      if (msg.type === "subscription_ack" || msg.status === "subscribed") {
        setWsConnected(true);
      } else if (msg.type === "alert_triggered") {
        toast.success(`Price Alert: ${msg.symbol}`, {
          description: `Crossed ${msg.condition} $${msg.threshold.toLocaleString()} (current: $${msg.price.toLocaleString()})`,
          duration: 10000,
        });
      }
    });

    // Stream consumers attach after a short grace period even without an ack (they need the
    // client object, not a live socket); the footer shows the socket's real state.
    const timer = setTimeout(() => {
      setWsConnected(true);
    }, 1500);
    const poll = setInterval(() => setStreamOpen(client.isOpen()), 1000);

    return () => {
      clearTimeout(timer);
      clearInterval(poll);
      unsubscribe();
    };
  }, []);

  const [showWatchlist, setShowWatchlist] = useState<boolean>(true);

  // Restore watchlist panel preference
  useEffect(() => {
    try {
      const saved = localStorage.getItem("cryptonite:panel-watchlist");
      if (saved !== null) setShowWatchlist(saved === "true");
    } catch {}
  }, []);

  // Listen for custom events (e.g., from CommandPalette, shortcuts or Modals)
  useEffect(() => {
    const handler = (e: CustomEvent) => {
      if (e.detail?.symbol) {
        handleSelectSymbol(e.detail.symbol);
        setWorkspaceMode("terminal");
      }
    };
    const profileHandler = (e: CustomEvent) => {
      if (e.detail?.symbol) {
        setProfileSymbol(e.detail.symbol);
      }
    };
    const switchWsHandler = (e: CustomEvent) => {
      const mode = parseWorkspace(e.detail?.mode);
      if (mode) {
        navigate(mode, typeof e.detail?.view === "string" ? e.detail.view : null);
        if (e.detail?.section) {
          window.dispatchEvent(new CustomEvent("cryptonite:settings-section", { detail: { section: e.detail.section } }));
        }
      }
    };
    // Legacy "open copilot" callers ({ prompt, autoSend }) now talk to the Brain
    const copilotHandler = (e: CustomEvent) => {
      setIsAgentChatOpen(true);
      if (e.detail?.prompt) dispatchBrainAsk({ prompt: String(e.detail.prompt), send: Boolean(e.detail.autoSend) });
    };
    const debateTriggerHandler = async (e: CustomEvent) => {
      const sym = e.detail?.symbol || selectedSymbol;
      const customThesis = e.detail?.customThesis;
      try {
        // No local placeholder run: the inbox shows the debate once the worker reports it
        const res = await fetch("/agent-api/debates/trigger", {
          method: "POST",
          credentials: "include",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": localStorage.getItem("csrf_token") || "",
          },
          body: JSON.stringify({ symbol: sym, timeframe: "1m", custom_thesis: customThesis }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || err.message || "Failed to queue debate");
        }
        toast.success(`Debate queued: ${sym}`, {
          description: customThesis ? `Thesis: ${customThesis}` : "Its card appears in the AI Brain inbox when the worker reports it.",
        });
      } catch (err: any) {
        toast.error(`Debate trigger error: ${err.message}`);
      }
    };

    window.addEventListener("cryptonite:select-symbol" as any, handler);
    window.addEventListener("cryptonite:open-profile" as any, profileHandler);
    window.addEventListener("cryptonite:switch-workspace" as any, switchWsHandler);
    window.addEventListener("cryptonite:open-copilot" as any, copilotHandler);
    window.addEventListener("cryptonite:trigger-debate" as any, debateTriggerHandler);

    return () => {
      window.removeEventListener("cryptonite:select-symbol" as any, handler);
      window.removeEventListener("cryptonite:open-profile" as any, profileHandler);
      window.removeEventListener("cryptonite:switch-workspace" as any, switchWsHandler);
      window.removeEventListener("cryptonite:open-copilot" as any, copilotHandler);
      window.removeEventListener("cryptonite:trigger-debate" as any, debateTriggerHandler);
    };
  }, [selectedSymbol, navigate, setWorkspaceMode]);

  // Update subscriptions when selectedSymbol changes
  useEffect(() => {
    if (!wsConnected) return;
    const client = getStreamClient();
    const sym = selectedSymbol.includes(".") ? selectedSymbol.toUpperCase() : `${selectedSymbol.toUpperCase()}.BINANCE`;
    const targetInstruments = Array.from(new Set([sym, "BTCUSDT.BINANCE", "ETHUSDT.BINANCE", "SOLUSDT.BINANCE", "BNBUSDT.BINANCE"]));
    client.updateSubscriptions(
      ["open_kline", "closed_bar_committed", "signal_candidate"],
      targetInstruments
    );
  }, [selectedSymbol, wsConnected]);

  const handleSelectSymbol = (symbol: string) => {
    setSelectedSymbol(symbol);
    setActiveChartTarget({
      symbol,
      timeframe: "1m",
      candleTime: 0,
    });
  };

  const handleViewOnChart = (symbol: string, timeframe: string, candleTime: number) => {
    setActiveChartTarget({
      symbol,
      timeframe,
      candleTime,
    });
  };

  return (
    <div className="flex flex-col h-screen w-screen bg-zinc-950 text-zinc-100 overflow-hidden font-sans">
      <StaleBuildBanner />
      {/*
        Top Institutional Header. md+: one row; the nav shrinks and scrolls so the banner and the Brain
        button always fit. Below md the banner takes a full-width second row, clear of the logo.
      */}
      <header className="flex-none bg-zinc-950 border-b border-white/5 flex flex-wrap md:flex-nowrap items-center px-4 z-20">
        <div className="flex flex-none items-center h-12 space-x-2 mr-3 md:mr-8">
          <div className="w-5 h-5 rounded bg-cyan-500 flex items-center justify-center font-black text-2xs text-zinc-950" aria-hidden="true">
            C
          </div>
          <span className="hidden sm:inline font-bold text-sm tracking-widest text-zinc-100">
            CRYPTONITE
          </span>
        </div>

        <WorkspaceNav workspaceMode={workspaceMode} urlView={urlView} navigate={navigate} className="h-12 flex-1 md:min-w-64" />

        <ModeBanner
          poll
          wrapBelowMd
          className="order-last w-full mb-2 md:order-none md:w-auto md:mb-0 md:ml-4 md:min-w-0 md:shrink"
          summaryClassName="md:max-w-40 lg:max-w-56 xl:max-w-xs 2xl:max-w-md"
        />

        {/* Leverage plan 4.7: shown only while a perp position is open */}
        <FlattenPerpsButton />

        {/* Header Utility Bar: Compact search & command palette trigger (⌘K) */}
        <button
          type="button"
          onClick={() => window.dispatchEvent(new CustomEvent("cryptonite:open-command-palette"))}
          aria-label="Search and commands"
          title="Search pairs, jump to any setting, or run commands (⌘K)"
          className="flex flex-none items-center space-x-1.5 ml-2 md:ml-3 px-2 sm:px-2.5 py-1.5 bg-zinc-900/90 hover:bg-zinc-800 border border-white/10 text-zinc-400 hover:text-zinc-200 rounded transition-colors font-mono text-2xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/60"
        >
          <Search className="w-3.5 h-3.5 text-zinc-400" aria-hidden="true" />
          <span className="hidden xl:inline text-zinc-400">Search</span>
          <kbd className="hidden sm:inline-block px-1 py-0.5 rounded bg-zinc-800 border border-zinc-700/60 text-3xs text-zinc-400 font-mono" aria-hidden="true">
            ⌘K
          </kbd>
        </button>

        {/* On the AI Brain page the Brain chat is already on screen; the drawer would only duplicate it */}

        {workspaceMode !== "debates" && (
          <button
            onClick={() => setIsAgentChatOpen(true)}
            aria-label="Brain"
            title="Brain"
            className="flex flex-none items-center space-x-1.5 ml-2 md:ml-4 px-2 sm:px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-white/5 text-cyan-400 rounded transition-colors font-mono text-xs font-bold"
          >
            <BrainCircuit className="w-3.5 h-3.5" aria-hidden="true" />
            <span className="hidden sm:inline">Brain</span>
          </button>
        )}
      </header>

      {/* Persistent Terminal Workspace */}
      <div className={workspaceMode === "terminal" ? "flex-1 flex min-h-0 min-w-0" : "hidden"}>
        {/* Left Watchlist Sidebar (Collapsible) */}
        {showWatchlist && (
          <aside className="w-60 flex-none h-full min-h-0 hidden md:flex border-r border-white/5 z-10">
            <WatchlistPanel
              selectedSymbol={selectedSymbol}
              onSelectSymbol={handleSelectSymbol}
            />
          </aside>
        )}

        <TerminalWorkspace
          selectedSymbol={selectedSymbol}
          scannerFocus={urlView === "scanner"}
          showWatchlist={showWatchlist}
          onToggleWatchlist={(val) => {
            setShowWatchlist(val);
            try {
              localStorage.setItem("cryptonite:panel-watchlist", String(val));
            } catch {}
          }}
          chartComponent={
            <MultiChartGrid
              activeTarget={activeChartTarget}
              onActiveChartSelect={(sym) => setSelectedSymbol(sym)}
            />
          }
          debateSidebarComponent={
            activeDebateId ? (
              <DebateTranscriptViewer runId={activeDebateId} symbol={selectedSymbol} />
            ) : undefined
          }
          blotterComponent={
            showScannerBuilder ? (
              <div className="flex-1 overflow-y-auto p-4 relative h-full">
                <div className="absolute top-2 right-2 z-10">
                  <button
                    onClick={() => setShowScannerBuilder(false)}
                    className="bg-zinc-800 hover:bg-zinc-700 text-white px-3 py-1.5 rounded text-xs font-bold transition-colors"
                  >
                    Cancel
                  </button>
                </div>
                <ScannerBuilder onCreated={() => setShowScannerBuilder(false)} />
              </div>
            ) : (
              <ScannerStudio 
                onViewOnChart={handleViewOnChart} 
                onCreateClick={() => setShowScannerBuilder(true)} 
              />
            )
          }
        />
      </div>

      {/* Persistent AI Brain Workspace */}
      <div className={workspaceMode === "debates" ? "flex-1 overflow-hidden relative flex flex-col min-h-0 min-w-0" : "hidden"}>
        <AIBrainWorkspace selectedSymbol={selectedSymbol} isActive={workspaceMode === "debates"} />
      </div>

      {/* Risk & Governance Flight Deck: Unified institutional control plane */}
      {workspaceMode === "governance" && (
        <div className="flex-1 min-h-0 min-w-0">
          <GovernanceConsole isActive initialView={urlView ?? undefined} />
        </div>
      )}

      {/* Mission Control: mandates, approvals, autonomy (blueprint §7.1) */}
      {workspaceMode === "mission" && (
        <div className="flex-1 min-h-0 min-w-0">
          <MissionControl isActive />
        </div>
      )}

      {/* On-Demand Secondary Workspaces */}
      {workspaceMode === "markets" && (
        <div className="flex-1 min-h-0 min-w-0 max-w-4xl mx-auto w-full">
          <MarketOverview onSelectSymbol={(sym) => {
            handleSelectSymbol(sym);
            setWorkspaceMode("terminal");
          }} />
        </div>
      )}
      {workspaceMode === "alerts" && (
        <div className="flex-1 min-h-0 min-w-0 max-w-3xl mx-auto w-full">
          <AlertsPanel />
        </div>
      )}
      {workspaceMode === "settings" && (
        <div className="flex-1 min-h-0 min-w-0 w-full px-2 py-3 sm:px-4">
          <SettingsPanel />
        </div>
      )}
      {workspaceMode === "strategies" && (
        <main className="flex-1 overflow-hidden relative">
          <StrategyStudio />
        </main>
      )}
      <main className={workspaceMode === "research" ? "flex-1 overflow-hidden relative p-1 pb-[33px]" : "hidden"}>
        <ResearchLab />
      </main>
      {(workspaceMode === "trades" || workspaceMode === "portfolio") && (
        <div className="flex-1 min-h-0 min-w-0">
          <TradesConsole />
        </div>
      )}
      {workspaceMode === "operator" && (
        <div className="flex-1 min-h-0 min-w-0 bg-zinc-950 p-6 overflow-y-auto">
          {/* Only a known sign-out hides the console. While the session is checked, each panel shows its own state. */}
          {sessionStatus !== "anonymous" ? (
            <div className="max-w-7xl mx-auto space-y-6">
              <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
                {/* Left Column (35%) */}
                <div className="lg:col-span-4 space-y-6">
                  <KillSwitchPanel />
                  <KillSwitchPanel environment="DEMO" accountId="DEMO-ACCOUNT-001" />
                  <EnforcedLimitsCard />
                </div>
                {/* Right Column (65%) */}
                <div className="lg:col-span-8 space-y-6">
                  <StrategyInstancesPanel />
                  <ReconciliationLedger />
                </div>
              </div>
            </div>
          ) : (
            <div role="status" className="flex items-center justify-center h-full text-zinc-500 font-mono">
              Sign in as operator to see the operator console.
            </div>
          )}
        </div>
      )}

      {/* Bottom Global Footer & Attribution */}
      <footer className="h-7 flex-none bg-zinc-950 border-t border-zinc-900 px-4 flex items-center justify-between text-2xs text-zinc-500 z-20 font-mono">
        <div className="flex items-center space-x-4">
          <div className="flex items-center space-x-1.5">
            <Radio className={`w-3 h-3 ${streamOpen ? "text-emerald-400 animate-pulse" : "text-amber-400"}`} aria-hidden="true" />
            <span className={streamOpen ? "text-emerald-400 font-semibold" : "text-amber-400 font-semibold"} role="status">
              {streamOpen ? "STREAM ONLINE" : "STREAM OFFLINE · reconnecting"}
            </span>
          </div>
        </div>

        <div className="flex items-center space-x-3 text-2xs">
          <span>Cryptonite v0.2.0</span>
          <span>•</span>
          <span className="text-zinc-400">PostgreSQL / Parquet / Redis 7</span>
        </div>
      </footer>

      {/* Global Overlays */}
      <AgentChatPanel
        isOpen={isAgentChatOpen}
        onClose={() => setIsAgentChatOpen(false)}
        activeBacktestId={activeBacktestRun?.id || null}
        onExpand={() => {
          setIsAgentChatOpen(false);
          navigate("debates", "home");
        }}
      />

      <DecisionTraceDrawer />

      <CoinProfileModal 
        symbol={profileSymbol} 
        onClose={() => setProfileSymbol(null)} 
      />

      {/* Shown only once the session is confirmed anonymous: never over a page that is still verifying */}
      {workspaceMode === "operator" && sessionStatus === "anonymous" && <OperatorLoginModal />}
    </div>
  );
}
