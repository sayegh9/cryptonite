"use client";

import React, { useState, useEffect, useCallback } from "react";
import {
  Strategy,
  BacktestRun,
  BacktestTrade,
  } from "@/lib/types";
import { EquityCurveChart } from "./EquityCurveChart";
import { TearsheetCards } from "./TearsheetCards";
import { MonthlyHeatmap } from "./MonthlyHeatmap";
import { TradesTable } from "./TradesTable";
import { MultiRunComparator } from "./MultiRunComparator";
import { ManifestModal } from "./ManifestModal";
import { StrategyManagerModal } from "./StrategyManagerModal";
import { ResearchApprovalsModal } from "./ResearchApprovalsModal";
import { LiveVsBacktestPanel } from "./LiveVsBacktestPanel";
import {
  Play,
  Sliders,
  Layers,
  LineChart,
  Table,
  Calendar,
  ShieldCheck,
  AlertTriangle,
  RefreshCw,
  Scale,
} from "lucide-react";
import { useBacktestStore } from "@/lib/stores/useBacktestStore";

export const ResearchLab: React.FC = () => {
  const strategies = useBacktestStore((state) => state.strategies);
  const setStrategies = useBacktestStore((state) => state.setStrategies);
  const selectedSlug = useBacktestStore((state) => state.selectedSlug);
  const setSelectedSlug = useBacktestStore((state) => state.setSelectedSlug);
  const [parameters, setParameters] = useState<Record<string, any>>({});
  // Conservative (fees + slippage, like the paper engine) by default; optimistic is costless (R2.1)
  const [executionProfile, setExecutionProfile] = useState<string>("conservative");
  const [timeframe, setTimeframe] = useState<string>("1m");
  const [instrumentId, setInstrumentId] = useState<string>("BTCUSDT.BINANCE");

  // Selected & active run states
  const activeRun = useBacktestStore((state) => state.activeRun);
  const setActiveRun = useBacktestStore((state) => state.setActiveRun);
  const [running, setRunning] = useState<boolean>(false);
  const [runError, setRunError] = useState<string | null>(null);

  // Active view tabs
  const [activeTab, setActiveTab] = useState<"tearsheet" | "trades" | "heatmap" | "compare" | "live">("tearsheet");
  const [selectedTrade, setSelectedTrade] = useState<BacktestTrade | null>(null);
  const [isManifestOpen, setIsManifestOpen] = useState<boolean>(false);
  const [isManagerOpen, setIsManagerOpen] = useState<boolean>(false);
  const [isApprovalsOpen, setIsApprovalsOpen] = useState<boolean>(false);

  const [instruments, setInstruments] = useState<any[]>([]);

  // Load registered strategies
  const fetchStrategies = useCallback(async (preserveSelection = true) => {
    try {
      const res = await fetch("/api/v1/strategies");
      if (res.ok) {
        const data: Strategy[] = await res.json();
        const uniqueStrategies = Array.from(new Map(data.map(s => [s.slug, s])).values());
        setStrategies(uniqueStrategies);
        const currentSlug = useBacktestStore.getState().selectedSlug;
        if (!preserveSelection || !currentSlug || !uniqueStrategies.some(s => s.slug === currentSlug)) {
          if (uniqueStrategies.length > 0) {
            const first = uniqueStrategies[0];
            setSelectedSlug(first.slug);
            setParameters(first.default_parameters || {});
          }
        }
      }
    } catch (e) {
      console.error("Failed to fetch strategies:", e);
    }
  }, [setStrategies, setSelectedSlug]);

  useEffect(() => {
    fetchStrategies(false);
    
    // Fetch available instruments
    const fetchInstruments = async () => {
      try {
        const res = await fetch("/api/v1/instruments");
        if (res.ok) {
          const data = await res.json();
          setInstruments(data);
        }
      } catch (e) {
        console.error("Failed to fetch instruments:", e);
      }
    };
    fetchInstruments();
  }, [fetchStrategies]);

  useEffect(() => {
    const handleUpdate = () => {
      fetchStrategies(true);
    };
    window.addEventListener("cryptonite:strategy-updated", handleUpdate);
    return () => {
      window.removeEventListener("cryptonite:strategy-updated", handleUpdate);
    };
  }, [fetchStrategies]);

  useEffect(() => {
    const handleLoadStrategy = (e: CustomEvent) => {
      const { manifest } = e.detail;
      if (manifest?.slug) {
        const currentStrategies = useBacktestStore.getState().strategies;
        if (!currentStrategies.find(s => s.slug === manifest.slug)) {
          setStrategies([{
            id: manifest.slug,
            name: manifest.name || manifest.slug,
            slug: manifest.slug,
            description: "AI Generated Strategy",
            author: "AI Agent",
            active_version_id: "pending",
            version_num: 1,
            code_hash: "",
            default_parameters: manifest.default_parameters || {},
            parameter_schema: {}
          }, ...currentStrategies]);
        }
        
        setSelectedSlug(manifest.slug);
        setParameters(manifest.default_parameters || {});
        if (manifest.timeframe) setTimeframe(manifest.timeframe);
        if (manifest.instrument_id) setInstrumentId(manifest.instrument_id);
      }
    };
    window.addEventListener("cryptonite:load-strategy" as any, handleLoadStrategy);
    return () => {
      window.removeEventListener("cryptonite:load-strategy" as any, handleLoadStrategy);
    };
  }, [setSelectedSlug, setStrategies]);

  // When strategy changes, populate default parameters
  const handleStrategyChange = (slug: string) => {
    setSelectedSlug(slug);
    const found = strategies.find((s) => s.slug === slug);
    if (found) {
      setParameters(found.default_parameters || {});
    }
  };

  const handleParamChange = (key: string, val: any) => {
    setParameters((prev) => ({ ...prev, [key]: val }));
  };

  const [lookbackDays, setLookbackDays] = useState<number>(3);

  // Submit backtest run
  const handleRunBacktest = async () => {
    setRunning(true);
    setRunError(null);
    setSelectedTrade(null);

    const nowMs = Date.now();
    const evalEndNs = nowMs * 1_000_000;
    // Evaluation is the lookback window, warmup is an extra 30% before it
    const lookbackMs = lookbackDays * 24 * 3600 * 1000;
    const warmupMs = Math.max(lookbackMs * 0.3, 4 * 3600 * 1000); 
    const evalStartNs = (nowMs - lookbackMs) * 1_000_000;
    const warmupStartNs = (nowMs - lookbackMs - warmupMs) * 1_000_000;

    const timeframeMap: Record<string, string> = {
      "1m": "1-MINUTE",
      "5m": "5-MINUTE",
      "15m": "15-MINUTE",
      "30m": "30-MINUTE",
      "1h": "1-HOUR",
      "4h": "4-HOUR",
      "1d": "1-DAY"
    };
    const barTypeSuffix = timeframeMap[timeframe] || "1-MINUTE";
    const bar_type = `${instrumentId}-${barTypeSuffix}-LAST-EXTERNAL`;

    const payload = {
      strategy_slug: selectedSlug,
      instrument_id: instrumentId,
      bar_type,
      timeframe,
      parameters,
      warmup_start_ns: warmupStartNs,
      evaluation_start_ns: evalStartNs,
      evaluation_end_ns: evalEndNs,
      execution_profile: executionProfile,
    };

    try {
      const res = await fetch("/api/v1/backtests", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "",
        },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Submission failed with HTTP ${res.status}`);
      }

      const created = await res.json();
      pollRunStatus(created.id);
    } catch (err: any) {
      setRunError(err.message || "Failed to submit backtest run.");
      setRunning(false);
    }
  };

  // Poll until run completes or fails
  const pollRunStatus = async (runId: string) => {
    const startTime = Date.now();
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`/api/v1/backtests/${runId}`);
        if (res.ok) {
          const runData: BacktestRun = await res.json();
          setActiveRun(runData);

          if (runData.status === "COMPLETED") {
            clearInterval(interval);
            setRunning(false);
          } else if (runData.status === "FAILED") {
            clearInterval(interval);
            setRunning(false);
            setRunError(runData.error_message || "Backtest execution failed.");
          }
        }
      } catch (e) {
        console.error("Error polling run status:", e);
      }

      // Timeout after 60s
      if (Date.now() - startTime > 60000) {
        clearInterval(interval);
        setRunning(false);
        setRunError("Backtest run timed out waiting for worker completion.");
      }
    }, 1000);
  };

  const currentStrategy = strategies.find((s) => s.slug === selectedSlug);

  return (
    <div className="flex h-full w-full bg-zinc-950 text-zinc-100 overflow-hidden font-sans">
      {/* Left Sidebar: Strategy & Execution Profile Configuration Form */}
      <aside className="w-80 flex-none h-full border-r border-white/5 bg-zinc-950 flex flex-col min-h-0 font-mono text-2xs">
        <div className="p-4 border-b border-white/5 bg-zinc-950 flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <Sliders className="w-4 h-4 text-cyan-400" />
            <span className="font-bold text-xs text-zinc-200 uppercase tracking-wider">Research Lab</span>
          </div>
          <span className="text-2xs bg-cyan-950/30 text-cyan-400 border border-cyan-900/50 px-1.5 py-0.5 rounded font-semibold">
            Nautilus rc4
          </span>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-5">
          {/* Strategy Selection */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-2xs text-zinc-400 uppercase tracking-wider font-bold">
                Target Strategy
              </label>
              <div className="flex items-center space-x-2">
              <button
                type="button"
                onClick={() => setIsApprovalsOpen(true)}
                className="text-2xs text-zinc-300 hover:text-white font-mono bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 rounded px-2 py-0.5"
              >
                Approvals
              </button>
              <button
                type="button"
                onClick={() => setIsManagerOpen(true)}
                className="text-2xs text-cyan-400 hover:text-cyan-300 font-mono flex items-center space-x-1 bg-cyan-950/30 hover:bg-cyan-900/40 border border-cyan-800/40 px-1.5 py-0.5 rounded transition-colors"
                title="Manage, inspect and delete registered strategies"
              >
                <Layers className="w-3 h-3" />
                <span>Manage</span>
              </button>
              </div>
            </div>
            <select
              value={selectedSlug}
              aria-label="Target strategy"
              onChange={(e) => handleStrategyChange(e.target.value)}
              className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-zinc-200 text-xs focus:border-cyan-500 focus:outline-none transition-colors"
            >
              {strategies.map((s) => (
                <option key={s.slug} value={s.slug}>
                  {s.name}
                </option>
              ))}
            </select>
            {currentStrategy?.description && (
              <p className="text-2xs text-zinc-500 mt-1.5 leading-relaxed">
                {currentStrategy.description}
              </p>
            )}
          </div>

          {/* Execution Profile */}
          <div>
            <label className="block text-2xs text-zinc-400 uppercase tracking-wider mb-2 font-bold">
              Execution Profile
            </label>
            <div className="space-y-2">
              {[
                {
                  id: "conservative",
                  label: "Conservative Institutional",
                  desc: "80% limit fills, 50% slip, 5/10 bps fees",
                  color: "border-purple-800 text-purple-400",
                },
                {
                  id: "adverse_slippage",
                  label: "Adverse Slippage (Stress)",
                  desc: "Forces 1 adverse tick per fill",
                  color: "border-amber-800 text-amber-400",
                },
                {
                  id: "optimistic",
                  label: "Optimistic: no costs – not for decisions",
                  desc: "Zero slippage, zero fees, DefaultFillModel",
                  color: "border-rose-800 text-rose-400",
                },
              ].map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => setExecutionProfile(p.id)}
                  aria-pressed={executionProfile === p.id}
                  className={`w-full text-left p-2.5 rounded border transition-colors ${
                    executionProfile === p.id
                      ? `bg-cyan-950/20 border-cyan-500/50 text-zinc-100 ring-1 ring-cyan-500/20`
                      : "bg-zinc-900/40 border-white/5 text-zinc-400 hover:border-zinc-700/50 hover:bg-zinc-900"
                  }`}
                >
                  <div className="font-semibold text-xs text-zinc-200">{p.label}</div>
                  <div className="text-2xs text-zinc-500 mt-1">{p.desc}</div>
                </button>
              ))}
            </div>
          </div>

          {/* Settings Grid */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-2xs text-zinc-400 uppercase mb-2 font-bold">Instrument</label>
              {instruments.length > 0 ? (
                <select
                  value={instrumentId}
                  aria-label="Instrument"
                  onChange={(e) => setInstrumentId(e.target.value)}
                  className="w-full bg-zinc-900 border border-white/5 rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-cyan-500 transition-colors"
                >
                  {instruments.map(inst => (
                    <option key={inst.instrument_id} value={inst.instrument_id}>
                      {inst.instrument_id} {inst.is_active ? "🟢" : "⚪"}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={instrumentId}
                  aria-label="Instrument"
                  onChange={(e) => setInstrumentId(e.target.value)}
                  placeholder="e.g. BTCUSDT.BINANCE"
                  className="w-full bg-zinc-900 border border-white/5 rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-cyan-500 transition-colors"
                />
              )}
            </div>
            <div>
              <label className="block text-2xs text-zinc-400 uppercase mb-2 font-bold">Timeframe</label>
              <select
                value={timeframe}
                aria-label="Timeframe"
                onChange={(e) => setTimeframe(e.target.value)}
                className="w-full bg-zinc-900 border border-white/5 rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-cyan-500 transition-colors"
              >
                <option value="1m">1 Minute</option>
                <option value="5m">5 Minutes</option>
                <option value="15m">15 Minutes</option>
                <option value="30m">30 Minutes</option>
                <option value="1h">1 Hour</option>
                <option value="4h">4 Hours</option>
                <option value="1d">1 Day</option>
              </select>
            </div>
            <div className="col-span-2">
              <label className="block text-2xs text-zinc-400 uppercase mb-2 font-bold">Lookback Window</label>
              <select
                value={lookbackDays}
                aria-label="Lookback window"
                onChange={(e) => setLookbackDays(Number(e.target.value))}
                className="w-full bg-zinc-900 border border-white/5 rounded px-2.5 py-1.5 text-xs text-zinc-200 focus:outline-none focus:border-cyan-500 transition-colors"
              >
                <option value={1}>Last 24 Hours</option>
                <option value={3}>Last 3 Days</option>
                <option value={7}>Last 7 Days</option>
                <option value={14}>Last 14 Days</option>
                <option value={30}>Last 30 Days</option>
              </select>
            </div>
          </div>

          {/* Dynamic Strategy Parameters */}
          <div>
            <label className="block text-2xs text-zinc-400 uppercase tracking-wider mb-2 font-bold flex items-center justify-between">
              <span>Strategy Parameters</span>
              <span className="text-2xs text-zinc-500">v{currentStrategy?.version_num || 1}</span>
            </label>
            {!currentStrategy?.parameter_schema || Object.keys(currentStrategy.parameter_schema).length === 0 ? (
              <div className="text-2xs text-zinc-500 bg-zinc-900/50 p-3 rounded border border-white/5 text-center italic">
                No tunable parameters configured.<br/>Executing with strategy defaults.
              </div>
            ) : (
              <div className="space-y-2 bg-zinc-900/40 border border-white/5 rounded p-3">
                {Object.entries(currentStrategy.parameter_schema).map(([k, schema]: [string, any]) => (
                  <div key={k} className="flex justify-between items-center group">
                    <div>
                      <div className="text-xs text-zinc-300 font-medium">
                        {schema.title || k}
                      </div>
                      {schema.description && (
                        <div className="text-2xs text-zinc-500 opacity-0 group-hover:opacity-100 transition-opacity absolute bg-black border border-white/10 p-1.5 rounded z-10 -ml-1 mt-1 max-w-[200px]">
                          {schema.description}
                        </div>
                      )}
                    </div>
                    <input
                      type={schema.type === "integer" || schema.type === "number" ? "number" : "text"}
                      aria-label={schema.title || k}
                      step={schema.type === "integer" ? "1" : "0.01"}
                      min={schema.minimum}
                      max={schema.maximum}
                      value={parameters[k] ?? schema.default ?? 0}
                      onChange={(e) =>
                        handleParamChange(
                          k,
                          schema.type === "integer"
                            ? parseInt(e.target.value) || 0
                            : schema.type === "number"
                            ? parseFloat(e.target.value) || 0
                            : e.target.value
                        )
                      }
                      className="w-16 bg-zinc-950 border border-white/5 rounded px-2 py-1 text-xs text-right text-zinc-200 focus:border-cyan-500 focus:outline-none"
                    />
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Run Error Message */}
          {runError && (
            <div className="p-3 rounded bg-rose-950/30 border border-rose-900/50 flex flex-col space-y-2">
              <div className="text-rose-400 text-2xs leading-relaxed flex items-start space-x-2">
                <AlertTriangle className="w-3.5 h-3.5 flex-none mt-0.5" />
                <span>{runError}</span>
              </div>
              {runError.includes("No bar data found") && (
                <button
                  onClick={async () => {
                    try {
                      setRunning(true);
                      setRunError(null);
                      const res = await fetch("/api/v1/instruments/backfill", {
                        method: "POST",
                        credentials: "include",
                        headers: {
                          "Content-Type": "application/json",
                          "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "",
                        },
                        body: JSON.stringify({
                          instrument_id: instrumentId,
                          timeframe: timeframe,
                          lookback_days: lookbackDays
                        })
                      });
                      if (!res.ok) throw new Error("Failed to start backfill");
                      // Optionally we could poll, but let's just clear and ask user to wait
                      setRunError("Data backfill started. Please wait 1-2 minutes and run backtest again.");
                    } catch (e: any) {
                      setRunError("Backfill failed: " + e.message);
                    } finally {
                      setRunning(false);
                    }
                  }}
                  className="bg-rose-900/40 hover:bg-rose-900/60 text-rose-200 py-1.5 px-3 rounded border border-rose-900/50 text-xs font-medium self-end transition-colors"
                >
                  Download Missing Data
                </button>
              )}
            </div>
          )}
        </div>

        {/* Form Action Buttons */}
        <div className="p-4 border-t border-white/5 bg-zinc-950">
          <button
            onClick={handleRunBacktest}
            disabled={running}
            className="w-full flex items-center justify-center space-x-2 py-2.5 px-3 rounded bg-cyan-600 hover:bg-cyan-500 disabled:bg-zinc-800 disabled:text-zinc-500 font-bold text-xs text-zinc-950 transition-colors"
          >
            {running ? (
              <>
                <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                <span>Simulating...</span>
              </>
            ) : (
              <>
                <Play className="w-3.5 h-3.5 fill-current" />
                <span>Run Quantitative Backtest</span>
              </>
            )}
          </button>
        </div>
      </aside>

      {/* Main Backtest Analysis & Tearsheet Workspace */}
      <main className="flex-1 flex flex-col min-h-0 min-w-0 bg-zinc-950">
        {/* Navigation Tabs Header */}
        <header className="h-12 flex-none border-b border-white/5 bg-zinc-950 flex items-center justify-between px-4 font-mono text-xs">
          <div className="flex items-center space-x-1">
            {[
              { id: "tearsheet", label: "Tearsheet & Curves", icon: LineChart },
              { id: "trades", label: "Trades Log", icon: Table },
              { id: "heatmap", label: "Monthly Returns", icon: Calendar },
              { id: "compare", label: "History & Compare", icon: Layers },
              { id: "live", label: "Live vs Backtest", icon: Scale },
            ].map((tab) => {
              const Icon = tab.icon;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id as any)}
                  className={`flex items-center space-x-1.5 px-3 py-1.5 rounded transition-colors ${
                    activeTab === tab.id
                      ? "bg-zinc-900 text-cyan-400 font-bold border border-white/5"
                      : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/50 border border-transparent"
                  }`}
                >
                  <Icon className="w-3.5 h-3.5" />
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </div>

          {/* Active Run Status & Manifest Trigger */}
          {activeRun && (
            <div className="flex items-center space-x-3 text-2xs">
              <span className="text-zinc-400">
                Run: <strong className="text-zinc-200">{activeRun.id.slice(0, 8)}</strong>
              </span>
              <span
                className={`px-1.5 py-0.5 rounded font-bold uppercase text-2xs ${
                  activeRun.status === "COMPLETED"
                    ? "bg-emerald-950/40 text-emerald-400 border border-emerald-900/50"
                    : activeRun.status === "FAILED"
                    ? "bg-rose-950/40 text-rose-400 border border-rose-900/50"
                    : "bg-amber-950/40 text-amber-400 border border-amber-900/50"
                }`}
              >
                {activeRun.status}
              </span>

              {activeRun.status === "COMPLETED" && (
                <>
                  {activeRun.results?.metrics?.sharpe_ratio && activeRun.results.metrics.sharpe_ratio > 1.0 && (
                    <button
                      onClick={() => {
                        window.dispatchEvent(new CustomEvent("cryptonite:switch-workspace", { detail: { mode: "debates" } }));
                        const customThesis = `Evaluate strategy ${activeRun.strategy_slug}. Sharpe: ${activeRun.results?.metrics?.sharpe_ratio?.toFixed(2) ?? 'N/A'}, Max Drawdown: ${((activeRun.results?.metrics?.max_drawdown ?? 0) * 100).toFixed(2)}%.`;
                        window.dispatchEvent(new CustomEvent("cryptonite:trigger-debate", { 
                          detail: { symbol: activeRun.instrument_id, customThesis } 
                        }));
                      }}
                      className="flex items-center space-x-1 px-2 py-0.5 rounded bg-cyan-900/60 hover:bg-cyan-800 text-cyan-300 transition-colors border border-cyan-800"
                    >
                      <span>⚡</span>
                      <span>Run Consensus Debate</span>
                    </button>
                  )}
                  <button
                    onClick={() => setIsManifestOpen(true)}
                    className="flex items-center space-x-1 px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 transition-colors"
                    title="View complete input catalog & artifact reproducibility manifest"
                  >
                  <ShieldCheck className="w-3.5 h-3.5 text-cyan-400" />
                  <span>Manifest</span>
                </button>
                </>
              )}
            </div>
          )}
        </header>

        {/* Tab Content Panes */}
        <div className="flex-1 overflow-hidden min-h-0 min-w-0 p-3">
          {activeTab === "tearsheet" && (
            <div className="flex flex-col h-full space-y-3 overflow-y-auto">
              {activeRun?.results?.metrics ? (
                <>
                  <TearsheetCards
                    metrics={activeRun.results.metrics}
                    executionProfile={activeRun.execution_profile}
                  />
                  <div className="flex-1 min-h-[360px] border border-zinc-900 rounded overflow-hidden">
                    <EquityCurveChart
                      equityPoints={activeRun.results.downsampled_equity || []}
                      selectedTrade={selectedTrade}
                    />
                  </div>
                </>
              ) : (
                <div className="flex flex-col items-center justify-center h-full text-zinc-500 font-mono text-xs space-y-2">
                  <LineChart className="w-8 h-8 text-zinc-600" />
                  <span>Configure a strategy and click &quot;Run Quantitative Backtest&quot; to inspect tearsheet metrics.</span>
                </div>
              )}
            </div>
          )}

          {activeTab === "trades" && (
            <div className="h-full">
              {activeRun ? (
                <TradesTable
                  runId={activeRun.id}
                  onSelectTrade={(t) => {
                    setSelectedTrade(t);
                    setActiveTab("tearsheet"); // Switch to chart to view marker
                  }}
                  selectedTrade={selectedTrade}
                />
              ) : (
                <div className="flex items-center justify-center h-full text-zinc-500 font-mono text-xs">
                  No active backtest run selected.
                </div>
              )}
            </div>
          )}

          {activeTab === "heatmap" && (
            <div className="h-full overflow-y-auto">
              {activeRun?.results?.monthly_returns ? (
                <MonthlyHeatmap monthlyReturns={activeRun.results.monthly_returns} />
              ) : (
                <div className="flex items-center justify-center h-full text-zinc-500 font-mono text-xs">
                  No monthly return matrix available for the active run.
                </div>
              )}
            </div>
          )}

          {activeTab === "live" && <LiveVsBacktestPanel />}

          {activeTab === "compare" && (
            <div className="h-full">
              <MultiRunComparator
                onSelectRun={async (id) => {
                  try {
                    const res = await fetch(`/api/v1/backtests/${id}`);
                    if (res.ok) {
                      const data = await res.json();
                      setActiveRun(data);
                      setActiveTab("tearsheet");
                    }
                  } catch (e) {
                    console.error("Failed to load run:", e);
                  }
                }}
              />
            </div>
          )}
        </div>
      </main>

      {/* Manifest Modal */}
      {activeRun?.results && (
        <ManifestModal
          isOpen={isManifestOpen}
          onClose={() => setIsManifestOpen(false)}
          runId={activeRun.id}
          runConfigHash={activeRun.run_config_hash}
          catalogManifest={activeRun.results.catalog_manifest || []}
          artifactManifest={activeRun.results.artifact_manifest || {}}
        />
      )}

      {/* Research Approvals Modal */}
      {isApprovalsOpen && <ResearchApprovalsModal onClose={() => setIsApprovalsOpen(false)} />}

      {/* Strategy Manager Modal */}
      {isManagerOpen && (
        <StrategyManagerModal
          onClose={() => setIsManagerOpen(false)}
          onStrategiesChanged={() => fetchStrategies(true)}
        />
      )}
    </div>
  );
};
