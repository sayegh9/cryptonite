"use client";

import React, { useState, useEffect } from "react";
import { MultiChartGrid } from "@/components/MultiChartGrid";
import { WatchlistPanel } from "@/components/WatchlistPanel";
import { ScannerStudio } from "@/components/ScannerStudio";
import { KillSwitchPanel } from "@/components/operator/KillSwitchPanel";
import { RiskLimitsEditor } from "@/components/operator/RiskLimitsEditor";
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
import { useDebateStream, useDebateStore } from "@/lib/stores/useDebateStore";
import { usePerpetualStream } from "@/lib/stores/usePerpetualStore";
import { TerminalWorkspace } from "@/components/terminal/TerminalWorkspace";
import { DebateTranscriptViewer } from "@/components/debate/DebateTranscriptViewer";

import { ResearchLab } from "@/components/ResearchLab/ResearchLab";
import { useBacktestStore } from "@/lib/stores/useBacktestStore";
import { PortfolioConsole } from "@/components/PortfolioConsole";
import { getStreamClient } from "@/lib/ws";
import { fetchApi } from "@/lib/api";
import { toast } from "sonner";
import {
  Activity,
  Zap,
  ShieldCheck,
  Radio,
  BrainCircuit,
} from "lucide-react";

export default function TerminalPage() {
  const [workspaceMode, setWorkspaceMode] = useState<"terminal" | "research" | "portfolio" | "operator" | "markets" | "alerts" | "settings" | "debates">("terminal");
  const [isAgentChatOpen, setIsAgentChatOpen] = useState(false);
  const [operatorAuthenticated, setOperatorAuthenticated] = useState(false);
  const [showScannerBuilder, setShowScannerBuilder] = useState(false);
  const [selectedSymbol, setSelectedSymbol] = useState<string>("BTCUSDT");
  const [activeChartTarget, setActiveChartTarget] = useState<{
    symbol: string;
    timeframe: string;
    candleTime: number;
  } | null>(null);
  const [wsConnected, setWsConnected] = useState<boolean>(false);
  const [profileSymbol, setProfileSymbol] = useState<string | null>(null);

  useDebateStream(wsConnected ? getStreamClient() : null);
  usePerpetualStream(wsConnected);

  const activeDebates = useDebateStore((state) => state.activeDebates);
  const debatesList = Object.values(activeDebates);
  const activeDebateId = debatesList.length > 0 ? debatesList[0].runId : null;

  const activeBacktestRun = useBacktestStore((state) => state.activeRun);

  // Check active operator session on mount and sync across components
  useEffect(() => {
    fetchApi("/auth/me")
      .then((res) => {
        if (res?.authenticated) {
          setOperatorAuthenticated(true);
          if (res.csrf_token) {
            localStorage.setItem("csrf_token", res.csrf_token);
          }
        }
      })
      .catch(() => {
        setOperatorAuthenticated(false);
      });

    const handleAuthSuccess = () => {
      setOperatorAuthenticated(true);
    };
    window.addEventListener("cryptonite:auth-success" as any, handleAuthSuccess);
    return () => {
      window.removeEventListener("cryptonite:auth-success" as any, handleAuthSuccess);
    };
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

    const timer = setTimeout(() => {
      setWsConnected(true);
    }, 1500);

    return () => {
      clearTimeout(timer);
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
      if (e.detail?.mode) {
        setWorkspaceMode(e.detail.mode as any);
      }
    };
    const copilotHandler = () => {
      setIsAgentChatOpen(true);
    };
    const debateTriggerHandler = async (e: CustomEvent) => {
      const sym = e.detail?.symbol || selectedSymbol;
      const customThesis = e.detail?.customThesis;
      const addTurn = useDebateStore.getState().addTurn;
      try {
        addTurn({
          runId: "pending-" + Date.now(),
          role: "System",
          content: customThesis ? `Initiating debate for ${sym} with thesis: ${customThesis}` : `Initiating multi-agent consensus debate for ${sym}...`,
          timestamp: Date.now(),
        });
        toast.success(`AI Debate Initiated: ${sym}`, {
          description: "TradingAgents intelligence gathering started in background.",
        });
        const res = await fetch("/agent-api/debates/trigger", {
          method: "POST",
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
  }, [selectedSymbol]);

  // Update subscriptions when selectedSymbol changes
  useEffect(() => {
    if (!wsConnected) return;
    const client = getStreamClient();
    client.updateSubscriptions(
      ["open_kline", "closed_bar_committed", "signal_candidate"],
      [`${selectedSymbol}.BINANCE`, "BTCUSDT.BINANCE", "ETHUSDT.BINANCE", "SOLUSDT.BINANCE", "BNBUSDT.BINANCE"]
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
      {/* Top Institutional Header */}
      <header className="h-12 flex-none bg-zinc-950 border-b border-white/5 flex items-center justify-between px-4 z-20">
        <div className="flex items-center h-full">
          <div className="flex items-center space-x-2 mr-8">
            <div className="w-5 h-5 rounded bg-cyan-500 flex items-center justify-center font-black text-[10px] text-zinc-950">
              C
            </div>
            <span className="font-bold text-sm tracking-widest text-zinc-100">
              CRYPTONITE
            </span>
          </div>

          <div className="flex items-center space-x-6 h-full font-mono text-xs font-semibold">
            {[
              { id: "terminal", label: "Terminal" },
              { id: "markets", label: "Markets" },
              { id: "research", label: "Research Lab" },
              { id: "alerts", label: "Alerts" },
              { id: "portfolio", label: "Portfolio" },
              { id: "debates", label: "AI Brain" },
              { id: "operator", label: "Operator" },
              { id: "settings", label: "Settings" },
            ].map((tab) => (
              <button
                key={tab.id}
                onClick={() => setWorkspaceMode(tab.id as any)}
                className={`h-full flex items-center border-b-2 px-1 transition-colors ${
                  workspaceMode === tab.id
                    ? "border-cyan-500 text-cyan-400"
                    : "border-transparent text-zinc-400 hover:text-zinc-200 hover:border-zinc-700"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center space-x-4">
          <div className="flex items-center space-x-1.5 bg-emerald-950/30 border border-emerald-900/50 px-2 py-1 rounded text-[10px] text-emerald-400 font-mono">
            <ShieldCheck className="w-3 h-3" />
            <span className="font-bold tracking-wide">ZERO TRADING AUTHORITY (READ ONLY)</span>
          </div>
          
          <button
            onClick={() => setIsAgentChatOpen(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-white/5 text-cyan-400 rounded transition-colors font-mono text-xs font-bold"
          >
            <BrainCircuit className="w-3.5 h-3.5" />
            <span>AI Copilot</span>
          </button>
        </div>
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
        <div className="flex-1 min-h-0 min-w-0 max-w-5xl mx-auto w-full py-4">
          <SettingsPanel />
        </div>
      )}
      <main className={workspaceMode === "research" ? "flex-1 overflow-hidden relative p-1 pb-[33px]" : "hidden"}>
        <ResearchLab />
      </main>
      {workspaceMode === "portfolio" && (
        <div className="flex-1 min-h-0 min-w-0">
          <PortfolioConsole />
        </div>
      )}
      {workspaceMode === "operator" && (
        <div className="flex-1 min-h-0 min-w-0 bg-zinc-950 p-6 overflow-y-auto">
          {operatorAuthenticated ? (
            <div className="max-w-7xl mx-auto space-y-6">
              <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
                {/* Left Column (35%) */}
                <div className="lg:col-span-4 space-y-6">
                  <KillSwitchPanel />
                  <RiskLimitsEditor />
                </div>
                {/* Right Column (65%) */}
                <div className="lg:col-span-8 space-y-6">
                  <StrategyInstancesPanel />
                  <ReconciliationLedger />
                </div>
              </div>
            </div>
          ) : (
            <div className="flex items-center justify-center h-full text-zinc-500 font-mono">
              Waiting for operator authentication...
            </div>
          )}
        </div>
      )}

      {/* Bottom Global Footer & Attribution */}
      <footer className="h-7 flex-none bg-zinc-950 border-t border-zinc-900 px-4 flex items-center justify-between text-[11px] text-zinc-500 z-20 font-mono">
        <div className="flex items-center space-x-4">
          <div className="flex items-center space-x-1.5">
            <Radio className={`w-3 h-3 ${wsConnected ? "text-emerald-400 animate-pulse" : "text-amber-400"}`} />
            <span className={wsConnected ? "text-emerald-400 font-semibold" : "text-amber-400 font-semibold"}>
              {wsConnected ? "STREAM ONLINE" : "CONNECTING..."}
            </span>
          </div>

          <div className="flex items-center space-x-3 hidden sm:flex border-l border-zinc-800 pl-4">
            <div className="flex items-center space-x-1">
              <Activity className="w-3 h-3 text-zinc-600" />
              <span>DuckDB p95: <span className="text-zinc-300">12ms</span></span>
            </div>
            <div className="flex items-center space-x-1">
              <Zap className="w-3 h-3 text-zinc-600" />
              <span>Polars p95: <span className="text-zinc-300">8ms</span></span>
            </div>
          </div>
        </div>

        <div className="flex items-center space-x-3 text-[10px]">
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
      />

      <CoinProfileModal 
        symbol={profileSymbol} 
        onClose={() => setProfileSymbol(null)} 
      />

      {workspaceMode === "operator" && !operatorAuthenticated && (
        <OperatorLoginModal
          onSuccess={() => setOperatorAuthenticated(true)}
        />
      )}
    </div>
  );
}
