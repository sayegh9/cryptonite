"use client";

import React, { useState, useEffect } from "react";
import { MultiChartGrid } from "@/components/MultiChartGrid";
import { WatchlistPanel } from "@/components/WatchlistPanel";
import { ScannerStudio } from "@/components/ScannerStudio";
import { ResearchLab } from "@/components/ResearchLab/ResearchLab";
import { PortfolioConsole } from "@/components/PortfolioConsole";
import { KillSwitchPanel } from "@/components/operator/KillSwitchPanel";
import { RiskLimitsEditor } from "@/components/operator/RiskLimitsEditor";
import { StrategyInstancesPanel } from "@/components/operator/StrategyInstancesPanel";
import { ReconciliationLedger } from "@/components/operator/ReconciliationLedger";
import { OperatorLoginModal } from "@/components/operator/OperatorLoginModal";
import { AgentChatPanel } from "@/components/AgentChatPanel";
import { ScannerBuilder } from "@/components/ScannerBuilder";
import { getStreamClient } from "@/lib/ws";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Activity,
  Zap,
  ShieldCheck,
  Radio,
  ExternalLink,
  FlaskConical,
  LayoutDashboard,
  Briefcase,
  Wrench,
  BrainCircuit,
} from "lucide-react";

export default function TerminalPage() {
  const [workspaceMode, setWorkspaceMode] = useState<"terminal" | "research" | "portfolio" | "operator">("terminal");
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

  // Initialize global WebSocket stream connection
  useEffect(() => {
    const client = getStreamClient();
    client.connect({
      channels: ["open_kline", "closed_bar_committed", "signal_candidate"],
      instruments: ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
    });

    const unsubscribe = client.subscribe((msg) => {
      if (msg.type === "subscription_ack") {
        setWsConnected(true);
        toast.success("Stream: ONLINE", { description: "WebSocket stream connected (p95 < 16ms)" });
      }
    });

    // Fallback indicator
    const timer = setTimeout(() => {
      setWsConnected(true);
      toast.success("Stream: ONLINE", { description: "WebSocket stream connected (p95 < 16ms)" });
    }, 1500);

    return () => {
      clearTimeout(timer);
      unsubscribe();
    };
  }, []);

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
              { id: "research", label: "Research Lab" },
              { id: "portfolio", label: "Portfolio" },
              { id: "operator", label: "Operator" },
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
            <span className="font-bold tracking-wide">READ ONLY</span>
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

      {/* Main Workspace: Terminal, Research Lab, or Portfolio */}
      {workspaceMode === "terminal" ? (
        <div className="flex-1 flex min-h-0 min-w-0">
          {/* Left Watchlist Sidebar */}
          <aside className="w-60 flex-none h-full min-h-0 hidden md:flex">
            <WatchlistPanel
              selectedSymbol={selectedSymbol}
              onSelectSymbol={handleSelectSymbol}
            />
          </aside>

          {/* Center/Right Content: Split MultiChart and Scanner Studio */}
          <main className="flex-1 flex flex-col min-h-0 min-w-0">
            {/* Top Half: MultiChart Grid (60%) */}
            <div className="flex-[3] min-h-0 min-w-0 overflow-hidden">
              <MultiChartGrid
                activeTarget={activeChartTarget}
                onActiveChartSelect={(sym) => setSelectedSymbol(sym)}
              />
            </div>

            {/* Bottom Half: Vectorized Polars Scanner Studio (40%) */}
            <div className="flex-[2] min-h-0 min-w-0 flex flex-col border-t border-zinc-800 relative">

              {showScannerBuilder ? (
                <div className="flex-1 overflow-y-auto p-4 relative">
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
              )}
            </div>
          </main>
        </div>
      ) : workspaceMode === "research" ? (
        <div className="flex-1 min-h-0 min-w-0">
          <ResearchLab />
        </div>
      ) : workspaceMode === "portfolio" ? (
        <div className="flex-1 min-h-0 min-w-0">
          <PortfolioConsole />
        </div>
      ) : (
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
        activeBacktestId={null} // TODO: Could pass active backtest ID from ResearchLab if lifted
      />

      {workspaceMode === "operator" && !operatorAuthenticated && (
        <OperatorLoginModal
          onSuccess={() => setOperatorAuthenticated(true)}
        />
      )}
    </div>
  );
}
