"use client";

import React, { useRef, useState, useEffect } from "react";
import { Panel, Group as PanelGroup, Separator as PanelResizeHandle, PanelImperativeHandle } from "react-resizable-panels";
import { KillSwitchPanel } from "@/components/operator/KillSwitchPanel";

import { ShieldAlert, ChevronDown, ChevronUp, BookOpen, BrainCircuit, X, Play, SlidersHorizontal, ListFilter, ArrowLeftRight } from "lucide-react";
import { OrderbookPanel } from "./OrderbookPanel";
import { MarginRiskHUD } from "./MarginRiskHUD";
import { OrderEntryPanel } from "./OrderEntryPanel";
import { usePerpetualPositionsPoll } from "@/lib/stores/usePerpetualStore";
import { hasBrackets, toPerpInstrumentId } from "@/lib/perp/margin";

interface TerminalWorkspaceProps {
  chartComponent: React.ReactNode;
  orderbookComponent?: React.ReactNode;
  debateSidebarComponent?: React.ReactNode;
  blotterComponent: React.ReactNode;
  selectedSymbol?: string;
  showWatchlist?: boolean;
  onToggleWatchlist?: (val: boolean) => void;
  onTriggerDebate?: () => void;
}

/** Order ticket + perp margin HUD; polls positions only while mounted (operator sidebar open). */
function TradingControls({ symbol }: { symbol: string }) {
  usePerpetualPositionsPoll();
  const perpInstrument = toPerpInstrumentId(symbol);
  return (
    <>
      {hasBrackets(perpInstrument) && <MarginRiskHUD instrumentId={perpInstrument} />}
      <OrderEntryPanel symbol={symbol} />
    </>
  );
}

const CustomResizeHandle = ({ horizontal = false }: { horizontal?: boolean }) => (
  <PanelResizeHandle className={`bg-zinc-900 hover:bg-cyan-500/50 transition-colors flex items-center justify-center select-none ${horizontal ? "h-1 w-full" : "w-1 h-full"}`}>
    <div className={`bg-white/20 rounded-full ${horizontal ? "w-8 h-0.5" : "w-0.5 h-8"}`} />
  </PanelResizeHandle>
);

export function TerminalWorkspace({
  chartComponent,
  orderbookComponent,
  debateSidebarComponent,
  blotterComponent,
  selectedSymbol = "BTCUSDT",
  showWatchlist = true,
  onToggleWatchlist,
  onTriggerDebate,
}: TerminalWorkspaceProps) {
  const blotterPanelRef = useRef<PanelImperativeHandle>(null);
  const [isBlotterCollapsed, setIsBlotterCollapsed] = useState(false);

  // Panel visibility state with localStorage persistence
  const [showOrderbook, setShowOrderbook] = useState<boolean>(true);
  const [showDebateSidebar, setShowDebateSidebar] = useState<boolean>(false);
  const [showOperatorSidebar, setShowOperatorSidebar] = useState<boolean>(false);

  useEffect(() => {
    try {
      const savedOb = localStorage.getItem("cryptonite:panel-orderbook");
      if (savedOb !== null) setShowOrderbook(savedOb === "true");
      
      const savedDebate = localStorage.getItem("cryptonite:panel-debate");
      if (savedDebate !== null) setShowDebateSidebar(savedDebate === "true");
      const savedOperator = localStorage.getItem("cryptonite:panel-operator");
      if (savedOperator !== null) setShowOperatorSidebar(savedOperator === "true");
    } catch {
      // Ignore storage errors in restricted contexts
    }
  }, []);

  const toggleOrderbook = (val: boolean) => {
    if (val) setShowOperatorSidebar(false);
    setShowOrderbook(val);
    try {
      localStorage.setItem("cryptonite:panel-orderbook", String(val));
    } catch {}
  };

  
  const toggleOperator = (val: boolean) => {
    setShowOperatorSidebar(val);
    if (val) {
      setShowOrderbook(false);
      setShowDebateSidebar(false);
    }
    localStorage.setItem("cryptonite:panel-operator", String(val));
  };

  const toggleDebate = (val: boolean) => {
    if (val) setShowOperatorSidebar(false);
    setShowDebateSidebar(val);
    try {
      localStorage.setItem("cryptonite:panel-debate", String(val));
    } catch {}
  };

  const toggleBlotter = () => {
    const panel = blotterPanelRef.current;
    if (panel) {
      if (panel.isCollapsed()) {
        panel.expand();
      } else {
        panel.collapse();
      }
    }
  };

  const handleLaunchDebate = () => {
    if (onTriggerDebate) {
      onTriggerDebate();
    } else {
      window.dispatchEvent(
        new CustomEvent("cryptonite:trigger-debate", {
          detail: { symbol: selectedSymbol },
        })
      );
    }
    // Also ensure sidebar is opened
    toggleDebate(true);
  };

  // Calculate panel sizes based on active panels
  const chartDefaultSize = (!showOrderbook && !showDebateSidebar && !showOperatorSidebar) ? 100 : ((showOrderbook && showDebateSidebar) || showOperatorSidebar) ? 55 : 75;

  return (
    <div className="flex-1 flex flex-col min-h-0 min-w-0 bg-zinc-950">
      {/* Workspace Quick-Toggles Bar */}
      <div className="h-8 bg-zinc-950 border-b border-white/5 px-3 flex items-center justify-between text-[11px] font-mono select-none flex-none">
        <div className="flex items-center space-x-2">
          <span className="text-zinc-500 uppercase tracking-wider text-[10px] flex items-center space-x-1">
            <SlidersHorizontal className="w-3 h-3 text-zinc-400" />
            <span>Panels:</span>
          </span>
          
          {/* Watchlist Toggle if parent handler provided */}
          {onToggleWatchlist && (
            <button
              onClick={() => onToggleWatchlist(!showWatchlist)}
              className={`px-2 py-0.5 rounded border transition-colors flex items-center space-x-1.5 focus-visible:ring-1 focus-visible:ring-cyan-500/50 outline-none ${
                showWatchlist 
                  ? "bg-cyan-950/40 border-cyan-800/60 text-cyan-400 font-bold" 
                  : "bg-zinc-900/60 border-white/5 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800"
              }`}
              title={showWatchlist ? "Hide Left Watchlist" : "Show Left Watchlist"}
              aria-label={showWatchlist ? "Hide Left Watchlist" : "Show Left Watchlist"}
            >
              <ListFilter className="w-3 h-3" />
              <span>Watchlist</span>
            </button>
          )}

          {/* Orderbook Toggle */}
          <button
            onClick={() => toggleOrderbook(!showOrderbook)}
            className={`px-2 py-0.5 rounded border transition-colors flex items-center space-x-1.5 focus-visible:ring-1 focus-visible:ring-cyan-500/50 outline-none ${
              showOrderbook 
                ? "bg-cyan-950/50 border-cyan-800/60 text-cyan-400 font-bold" 
                : "bg-zinc-900/60 border-white/5 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800"
            }`}
            title={showOrderbook ? "Hide Orderbook Sidebar" : "Show Orderbook Sidebar"}
            aria-label={showOrderbook ? "Hide Orderbook Sidebar" : "Show Orderbook Sidebar"}
          >
            <BookOpen className="w-3 h-3" />
            <span>Orderbook</span>
          </button>

          {/* AI Debate Toggle */}
          <button
            onClick={() => toggleDebate(!showDebateSidebar)}
            className={`px-2 py-0.5 rounded border transition-colors flex items-center space-x-1.5 focus-visible:ring-1 focus-visible:ring-purple-500/50 outline-none ${
              showDebateSidebar 
                ? "bg-purple-950/50 border-purple-800/60 text-purple-400 font-bold" 
                : "bg-zinc-900/60 border-white/5 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800"
            }`}
            title={showDebateSidebar ? "Hide AI Debate Sidebar" : "Show AI Debate Sidebar"}
            aria-label={showDebateSidebar ? "Hide AI Debate Sidebar" : "Show AI Debate Sidebar"}
          >
            <BrainCircuit className="w-3 h-3" />
            <span>AI Debate</span>
          </button>

          {/* Trade Toggle: order ticket, perp margin HUD, and kill switch (operator sidebar) */}
          <button
            onClick={() => toggleOperator(!showOperatorSidebar)}
            className={`px-2 py-0.5 rounded border transition-colors flex items-center space-x-1.5 focus-visible:ring-1 focus-visible:ring-rose-500/50 outline-none ${
              showOperatorSidebar
                ? "bg-rose-950/50 border-rose-800/60 text-rose-400 font-bold"
                : "bg-zinc-900/60 border-white/5 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800"
            }`}
            title={showOperatorSidebar ? "Hide Trade Sidebar" : "Show Trade Sidebar"}
            aria-label={showOperatorSidebar ? "Hide Trade Sidebar" : "Show Trade Sidebar"}
          >
            <ArrowLeftRight className="w-3 h-3" />
            <span>Trade</span>
          </button>
        </div>

        <div className="flex items-center space-x-3 text-zinc-500 text-[10px]">
          <span>
            Layout:{" "}
            <span className="text-zinc-300 font-bold">
              {showOrderbook && showDebateSidebar ? "3-Column" : (showOrderbook || showDebateSidebar) ? "2-Column" : "Full Canvas"}
            </span>
          </span>
        </div>
      </div>

      <PanelGroup id="cryptonite-workspace-v2" orientation="vertical">
        {/* Upper Section */}
        <Panel defaultSize={70} minSize={30}>
          <PanelGroup id="cryptonite-workspace-v2-upper" orientation="horizontal">
            {/* Left: Chart Area */}
            <Panel defaultSize={chartDefaultSize} minSize={30} className="flex flex-col min-h-0 min-w-0">
              {chartComponent}
            </Panel>

            {/* Middle: Orderbook (Collapsible / Toggleable) */}
            {showOrderbook && (
              <>
                <CustomResizeHandle />
                <Panel defaultSize={22} minSize={12} className="flex flex-col min-h-0 min-w-0 border-l border-white/5 bg-zinc-950">
                  <div className="h-7 bg-zinc-900/80 border-b border-white/5 px-3 flex items-center justify-between flex-none select-none">
                    <div className="flex items-center space-x-1.5 text-zinc-300 font-mono text-[10px] font-bold uppercase tracking-wider">
                      <BookOpen className="w-3 h-3 text-cyan-400" />
                      <span>Orderbook L2</span>
                    </div>
                    <button
                      onClick={() => toggleOrderbook(false)}
                      className="text-zinc-500 hover:text-zinc-200 p-0.5 rounded hover:bg-zinc-800 transition-colors focus-visible:ring-1 focus-visible:ring-cyan-500 outline-none"
                      title="Hide Orderbook panel"
                      aria-label="Hide Orderbook panel"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </div>
                  <div className="flex-1 min-h-0 overflow-auto">
                    {orderbookComponent || <OrderbookPanel symbol={selectedSymbol} />}
                  </div>
                </Panel>
              </>
            )}

            {/* Right: AI Debate Sidebar (Collapsible / Toggleable) */}
            {showDebateSidebar && (
              <>
                <CustomResizeHandle />
                <Panel defaultSize={25} minSize={15} className="flex flex-col min-h-0 min-w-0 border-l border-white/5 bg-zinc-950/50">
                  <div className="h-7 bg-zinc-900/80 border-b border-white/5 px-3 flex items-center justify-between flex-none select-none">
                    <div className="flex items-center space-x-1.5 text-zinc-300 font-mono text-[10px] font-bold uppercase tracking-wider">
                      <BrainCircuit className="w-3 h-3 text-purple-400" />
                      <span>AI Consensus Debate</span>
                    </div>
                    <button
                      onClick={() => toggleDebate(false)}
                      className="text-zinc-500 hover:text-zinc-200 p-0.5 rounded hover:bg-zinc-800 transition-colors focus-visible:ring-1 focus-visible:ring-purple-500 outline-none"
                      title="Hide Debate sidebar"
                      aria-label="Hide Debate sidebar"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </div>
                  <div className="flex-1 min-h-0 min-w-0 overflow-hidden">
                    {debateSidebarComponent || (
                      <div className="flex-1 h-full flex flex-col items-center justify-center text-zinc-500 font-mono text-xs p-5 text-center">
                        <div className="w-12 h-12 rounded-xl bg-purple-950/40 border border-purple-800/40 flex items-center justify-center mb-3">
                          <BrainCircuit className="w-6 h-6 text-purple-400" />
                        </div>
                        <p className="font-semibold text-zinc-200 mb-1">No Active Debate for {selectedSymbol}</p>
                        <p className="text-[11px] text-zinc-400 max-w-xs mb-4 leading-relaxed">
                          TradingAgents will convene Market, Sentiment, and Risk officers to debate long vs. short conviction.
                        </p>
                        <button
                          onClick={handleLaunchDebate}
                          className="px-3 py-1.5 rounded bg-purple-600 hover:bg-purple-500 text-white font-bold text-xs flex items-center space-x-1.5 shadow-lg shadow-purple-900/20 transition-colors"
                        >
                          <Play className="w-3 h-3 fill-current" />
                          <span>Run Debate Now</span>
                        </button>
                      </div>
                    )}
                  </div>
                </Panel>
              </>
            )}

            {/* Operator Sidebar */}
            {showOperatorSidebar && (
              <>
                <CustomResizeHandle />
                <Panel defaultSize={30} minSize={20} className="flex flex-col min-h-0 min-w-0 border-l border-white/5 bg-zinc-950">
                  <div className="h-7 bg-zinc-900/80 border-b border-rose-900/30 px-3 flex items-center justify-between flex-none select-none">
                    <div className="flex items-center space-x-1.5 text-rose-400 font-mono text-[10px] font-bold uppercase tracking-wider">
                      <ShieldAlert className="w-3 h-3" />
                      <span>Operator Control</span>
                    </div>
                    <button
                      onClick={() => toggleOperator(false)}
                      className="text-zinc-500 hover:text-zinc-200 p-0.5 rounded hover:bg-zinc-800 transition-colors focus-visible:ring-1 focus-visible:ring-rose-500 outline-none"
                      title="Hide Operator Control"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </div>
                  <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
                    {/* The Trade sidebar trades on the paper engine, so its kill switch must stop that engine */}
                    <KillSwitchPanel environment="PAPER" accountId="PAPER-ACCOUNT-001" />
                    <TradingControls symbol={selectedSymbol} />
                  </div>
                </Panel>
              </>
            )}
          </PanelGroup>

        </Panel>

        <CustomResizeHandle horizontal />

        {/* Lower Section: Blotter/Console */}
        <Panel 
          panelRef={blotterPanelRef}
          defaultSize={30} 
          minSize={10} 
          collapsible={true}
          onResize={(size) => setIsBlotterCollapsed(size.asPercentage < 5)}
          className="flex flex-col min-h-0 min-w-0 border-t border-white/5 relative"
        >
          {/* Toggle Button */}
          <button
            onClick={toggleBlotter}
            className="absolute top-0 right-4 -translate-y-full bg-zinc-900 border-x border-t border-white/5 hover:bg-zinc-800 text-zinc-400 hover:text-cyan-400 px-3 py-1 rounded-t-md transition-colors z-10 flex items-center gap-2 focus-visible:ring-2 focus-visible:ring-cyan-500/50 outline-none select-none"
            aria-label={isBlotterCollapsed ? "Expand blotter" : "Collapse blotter"}
          >
            <span className="font-mono text-[10px] uppercase font-bold tracking-wider">Blotter</span>
            {isBlotterCollapsed ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          </button>
          
          <div className="flex-1 min-h-0 overflow-hidden">
            {blotterComponent}
          </div>
        </Panel>
      </PanelGroup>
    </div>
  );
}
