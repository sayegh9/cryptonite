"use client";

import React, { useState, useEffect, useMemo } from "react";
import {
  X,
  Play,
  Pause,
  Square,
  RefreshCw,
  Sliders,
  LineChart,
  BookOpen,
  Cpu,
  AlertTriangle,
  TrendingUp,
  Activity,
  Plus,
  Quote,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import {
  Strategy,
  BacktestRun,
  BacktestTrade,
} from "@/lib/types";
import { StrategyIdentityBadge, type StrategyStatus } from "./StrategyIdentityBadge";
import { MarketRegimePill } from "./MarketRegimePill";
import { EquityCurveChart } from "../ResearchLab/EquityCurveChart";
import { TearsheetCards } from "../ResearchLab/TearsheetCards";
import { MonthlyHeatmap } from "../ResearchLab/MonthlyHeatmap";
import { TradesTable } from "../ResearchLab/TradesTable";
import {
  usePortfolioStore,
  refreshPortfolio,
} from "@/lib/stores/usePortfolioStore";
import { postCommand, closePosition } from "@/lib/portfolio/actions";
import {
  strategyStats,
  fmtSignedUsd,
  fmtPct,
  fmtCompactUsd,
} from "@/lib/portfolio/metrics";
import { Sparkline } from "../portfolio/Sparkline";
import { fetchApi } from "@/lib/api";
import { toast } from "sonner";
import { cn } from "@/lib/utils";

export interface StrategyInspectorDrawerProps {
  strategy: Strategy | null;
  isOpen: boolean;
  onClose: () => void;
  onStrategyUpdated?: () => void;
  initialTab?: "overview" | "backtest" | "live";
}

const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];

const TIMEFRAME_MAP: Record<string, string> = {
  "1m": "1-MINUTE",
  "5m": "5-MINUTE",
  "15m": "15-MINUTE",
  "30m": "30-MINUTE",
  "1h": "1-HOUR",
  "4h": "4-HOUR",
  "1d": "1-DAY",
};

const isAiMandate = (instanceId: string) => instanceId.startsWith("AI_MANDATE_");

export function StrategyInspectorDrawer({
  strategy,
  isOpen,
  onClose,
  onStrategyUpdated,
  initialTab = "overview",
}: StrategyInspectorDrawerProps) {
  const [activeTab, setActiveTab] = useState<"overview" | "backtest" | "live">(initialTab);

  // Sync initial tab when changed externally
  useEffect(() => {
    if (initialTab) {
      setActiveTab(initialTab);
    }
  }, [initialTab, strategy?.slug]);

  // Escape key handler
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isOpen) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose]);

  // Backtest states
  const [timeframe, setTimeframe] = useState<string>("15m");
  const [lookbackDays, setLookbackDays] = useState<number>(7);
  const [executionProfile, setExecutionProfile] = useState<string>("conservative");
  const [instrumentId, setInstrumentId] = useState<string>("BTCUSDT.BINANCE");
  const [instruments, setInstruments] = useState<any[]>([]);
  const [parameters, setParameters] = useState<Record<string, any>>({});
  const [activeRun, setActiveRun] = useState<BacktestRun | null>(null);
  const [running, setRunning] = useState<boolean>(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [selectedTrade, setSelectedTrade] = useState<BacktestTrade | null>(null);
  const [backtestSubTab, setBacktestSubTab] = useState<"tearsheet" | "heatmap" | "trades">("tearsheet");

  // Live instances & portfolio store
  const liveInstances = usePortfolioStore((s) => s.strategies);
  const positions = usePortfolioStore((s) => s.positions);

  // Deploy modal inside Live tab
  const [isDeploying, setIsDeploying] = useState<boolean>(false);
  const [showDeployForm, setShowDeployForm] = useState<boolean>(false);
  const [deployInstrument, setDeployInstrument] = useState<string>("BTCUSDT.BINANCE");
  const [deployTimeframe, setDeployTimeframe] = useState<string>("15m");
  const [deployAllocation, setDeployAllocation] = useState<number>(5000);
  const [deployParameters, setDeployParameters] = useState<Record<string, any>>({});

  // Sync parameters and default timeframe when strategy opens
  useEffect(() => {
    if (strategy) {
      const defTf = strategy.timeframe_default || "15m";
      setTimeframe(defTf);
      setDeployTimeframe(defTf);
      setParameters(strategy.default_parameters || {});
      setDeployParameters(strategy.default_parameters || {});
      setActiveRun(null);
      setRunError(null);
      setSelectedTrade(null);

      // Fetch instruments if not loaded
      fetchApi("/instruments?catalog_only=true")
        .then((data) => {
          if (Array.isArray(data) && data.length > 0) {
            setInstruments(data);
          }
        })
        .catch(() => {
          // Fallback instruments
          setInstruments([
            { id: "BTCUSDT.BINANCE", symbol: "BTCUSDT" },
            { id: "ETHUSDT.BINANCE", symbol: "ETHUSDT" },
            { id: "SOLUSDT.BINANCE", symbol: "SOLUSDT" },
          ]);
        });

      // Try loading the latest completed backtest for this strategy
      fetchApi(`/backtests?strategy_slug=${encodeURIComponent(strategy.slug)}&status=COMPLETED&limit=1`)
        .then(async (runs) => {
          if (Array.isArray(runs) && runs.length > 0) {
            const latest = runs[0];
            try {
              const fullRun = await fetchApi(`/backtests/${latest.id}`);
              if (fullRun && fullRun.id) {
                setActiveRun(fullRun);
              }
            } catch {
              // ignore
            }
          }
        })
        .catch(() => {
          // No prior run found
        });
    }
  }, [strategy?.slug]);

  // Compute matched instances for this strategy
  const matchedInstances = useMemo(() => {
    if (!strategy) return [];
    const slugLower = strategy.slug.toLowerCase();
    const nameLower = strategy.name.toLowerCase();
    return liveInstances.filter(
      (st) =>
        st.strategy_name.toLowerCase() === slugLower ||
        st.strategy_name.toLowerCase() === nameLower
    );
  }, [strategy, liveInstances]);

  // Overall strategy live status
  const strategyLiveStatus: StrategyStatus = useMemo(() => {
    if (matchedInstances.length === 0) return "IDLE";
    if (matchedInstances.some((i) => i.status === "RUNNING")) return "RUNNING";
    if (matchedInstances.some((i) => i.status === "PAUSED")) return "PAUSED";
    return "STOPPED";
  }, [matchedInstances]);

  // Handle run backtest
  const handleRunBacktest = async () => {
    if (!strategy) return;
    setRunning(true);
    setRunError(null);
    setSelectedTrade(null);

    const barTypeSuffix = TIMEFRAME_MAP[timeframe] || "15-MINUTE";
    const bar_type = `${instrumentId}-${barTypeSuffix}-LAST-EXTERNAL`;

    let endMs = Date.now();
    try {
      const covRes = await fetch(`/api/v1/backtests/coverage?bar_type=${encodeURIComponent(bar_type)}`, {
        credentials: "include",
      });
      if (covRes.ok) {
        const cov = await covRes.json();
        if (cov.end_ns == null) {
          setRunError(`The data catalog has no recorded bars for ${bar_type}.`);
          setRunning(false);
          return;
        }
        endMs = Math.min(endMs, Math.floor(cov.end_ns / 1_000_000));
      }
    } catch {
      // Coverage check fallback
    }

    const nowMs = endMs;
    const evalEndNs = nowMs * 1_000_000;
    const lookbackMs = lookbackDays * 24 * 3600 * 1000;
    const warmupMs = Math.max(lookbackMs * 0.3, 4 * 3600 * 1000);
    const evalStartNs = (nowMs - lookbackMs) * 1_000_000;
    const warmupStartNs = (nowMs - lookbackMs - warmupMs) * 1_000_000;

    const payload = {
      strategy_slug: strategy.slug,
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

  const pollRunStatus = (runId: string) => {
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
            toast.success("Backtest completed successfully!");
          } else if (runData.status === "FAILED") {
            clearInterval(interval);
            setRunning(false);
            setRunError(runData.error_message || "Backtest failed during execution.");
            toast.error("Backtest run failed");
          }
        }
      } catch (err: any) {
        clearInterval(interval);
        setRunning(false);
        setRunError("Error checking run status: " + err.message);
      }

      if (Date.now() - startTime > 120_000) {
        clearInterval(interval);
        setRunning(false);
        setRunError("Backtest run timed out after 2 minutes.");
      }
    }, 1000);
  };

  // Handle instance commands
  const handleInstanceCommand = async (instanceId: string, verb: "pause" | "resume" | "stop") => {
    const nextStatus = verb === "pause" ? "PAUSED" : verb === "resume" ? "RUNNING" : "STOPPED";
    usePortfolioStore.getState().setStrategyStatusOptimistic(instanceId, nextStatus);
    const success = await postCommand(
      `/paper/strategies/${instanceId}/${verb}`,
      undefined,
      `Could not ${verb} ${instanceId}`
    );
    if (success) {
      toast.success(`Instance ${instanceId} ${verb}d`);
      void refreshPortfolio();
      onStrategyUpdated?.();
    }
  };

  // Handle deploy new paper instance
  const handleDeployInstance = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!strategy) return;
    setIsDeploying(true);

    try {
      const success = await postCommand(
        "/paper/strategies/deploy",
        {
          strategy_name: strategy.slug,
          strategy_version_id: strategy.active_version_id,
          instrument_id: deployInstrument,
          timeframe: deployTimeframe,
          allocation_usd: deployAllocation,
          parameters: deployParameters,
        },
        "Deploy failed"
      );

      if (success) {
        toast.success(`Successfully deployed ${strategy.name} on ${deployInstrument}`);
        setShowDeployForm(false);
        void refreshPortfolio();
        onStrategyUpdated?.();
      }
    } finally {
      setIsDeploying(false);
    }
  };

  if (!isOpen || !strategy) return null;

  return (
    <div
      className="fixed inset-0 z-50 overflow-hidden bg-black/70 backdrop-blur-xs flex justify-end animate-in fade-in duration-200"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`Strategy Inspector: ${strategy.name}`}
    >
      <div
        className="w-full max-w-4xl bg-zinc-950 border-l border-zinc-800 h-full flex flex-col shadow-2xl animate-in slide-in-from-right duration-250 select-text"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Drawer Header */}
        <div className="p-5 border-b border-zinc-800 bg-zinc-900/60 flex flex-col gap-4">
          <div className="flex items-start justify-between gap-4">
            <div className="flex flex-col gap-2 min-w-0">
              <div className="flex flex-wrap items-center gap-2.5">
                <StrategyIdentityBadge
                  name={strategy.name}
                  category={strategy.category}
                  status={strategyLiveStatus}
                  timeframe={strategy.timeframe_default || "15m"}
                  size="lg"
                />
                <MarketRegimePill regime={strategy.market_regime || "High Volatility Trends"} />
              </div>
              <div className="flex items-center gap-3 text-2xs text-zinc-400 font-mono">
                <span>
                  Slug: <span className="text-zinc-200">{strategy.slug}</span>
                </span>
                <span>•</span>
                <span>
                  Author: <span className="text-zinc-200">{strategy.author || "Cryptonite Core"}</span>
                </span>
                <span>•</span>
                <span>
                  Active: <span className="text-cyan-400">v{strategy.version_num}</span>
                </span>
                {strategy.code_hash && (
                  <>
                    <span>•</span>
                    <span title={`Code Hash: ${strategy.code_hash}`}>
                      Hash: <span className="text-zinc-400">{strategy.code_hash.slice(0, 8)}</span>
                    </span>
                  </>
                )}
              </div>
            </div>

            <button
              onClick={onClose}
              aria-label="Close strategy inspector"
              className="p-1.5 rounded-lg border border-zinc-800 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 transition-colors shrink-0"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Tab Selector */}
          <div className="flex items-center gap-1 border-b border-zinc-800/80 -mb-1 pt-1">
            <button
              onClick={() => setActiveTab("overview")}
              className={cn(
                "flex items-center gap-2 px-4 py-2 text-xs font-semibold rounded-t-lg transition-all border-b-2",
                activeTab === "overview"
                  ? "border-cyan-400 text-cyan-300 bg-zinc-800/40"
                  : "border-transparent text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/20"
              )}
            >
              <BookOpen className="w-3.5 h-3.5" />
              <span>Overview & Rationale</span>
            </button>

            <button
              onClick={() => setActiveTab("backtest")}
              className={cn(
                "flex items-center gap-2 px-4 py-2 text-xs font-semibold rounded-t-lg transition-all border-b-2",
                activeTab === "backtest"
                  ? "border-cyan-400 text-cyan-300 bg-zinc-800/40"
                  : "border-transparent text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/20"
              )}
            >
              <LineChart className="w-3.5 h-3.5" />
              <span>Quantitative Backtest & Tearsheet</span>
              {activeRun && (
                <span className="ml-1 px-1.5 py-0.2 rounded text-[10px] font-mono bg-emerald-950/70 text-emerald-400 border border-emerald-800/50">
                  Ready
                </span>
              )}
            </button>

            <button
              onClick={() => setActiveTab("live")}
              className={cn(
                "flex items-center gap-2 px-4 py-2 text-xs font-semibold rounded-t-lg transition-all border-b-2",
                activeTab === "live"
                  ? "border-cyan-400 text-cyan-300 bg-zinc-800/40"
                  : "border-transparent text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/20"
              )}
            >
              <Cpu className="w-3.5 h-3.5" />
              <span>Live Paper Instances</span>
              <span
                className={cn(
                  "ml-1 px-1.5 py-0.2 rounded text-[10px] font-mono font-bold",
                  matchedInstances.length > 0
                    ? "bg-emerald-950 text-emerald-400 border border-emerald-800/60"
                    : "bg-zinc-800 text-zinc-500"
                )}
              >
                {matchedInstances.length}
              </span>
            </button>
          </div>
        </div>

        {/* Tab 1: Overview & Rationale */}
        {activeTab === "overview" && (
          <div className="p-6 flex-1 overflow-y-auto space-y-6">
            {/* Prominent Plain-English Thesis Callout */}
            <div className="relative rounded-xl border border-cyan-500/30 bg-gradient-to-br from-cyan-950/40 via-zinc-900/60 to-zinc-950 p-5 shadow-lg shadow-cyan-950/10">
              <div className="flex items-center gap-2 mb-2 text-cyan-400 font-mono text-xs uppercase tracking-wider font-semibold">
                <Quote className="size-4 shrink-0" />
                <span>Strategic Investment Thesis</span>
              </div>
              <blockquote className="text-sm md:text-base text-zinc-200 italic leading-relaxed font-sans pl-3 border-l-2 border-cyan-400/80">
                "{strategy.thesis || strategy.description || "Autonomous quantitative alpha strategy generating risk-adjusted returns across targeted regime conditions."}"
              </blockquote>
            </div>

            {/* Description & Strategy Profile */}
            <div className="bg-zinc-900/40 border border-zinc-800/80 rounded-xl p-5 space-y-4">
              <h3 className="text-xs font-bold uppercase tracking-wider text-zinc-300 flex items-center gap-2">
                <BookOpen className="w-4 h-4 text-cyan-400" />
                <span>Strategy Summary & Market Context</span>
              </h3>
              <p className="text-xs text-zinc-300 leading-relaxed font-sans">
                {strategy.description || "No detailed overview provided for this strategy."}
              </p>

              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-2 font-mono text-xs">
                <div className="bg-zinc-950/70 border border-zinc-800 rounded-lg p-3">
                  <span className="text-zinc-500 text-2xs block uppercase">Category</span>
                  <span className="font-semibold text-zinc-200 capitalize">
                    {strategy.category?.replace(/_/g, " ") || "Trend Following"}
                  </span>
                </div>
                <div className="bg-zinc-950/70 border border-zinc-800 rounded-lg p-3">
                  <span className="text-zinc-500 text-2xs block uppercase">Market Regime</span>
                  <span className="font-semibold text-zinc-200">
                    {strategy.market_regime || "High Volatility Trends"}
                  </span>
                </div>
                <div className="bg-zinc-950/70 border border-zinc-800 rounded-lg p-3">
                  <span className="text-zinc-500 text-2xs block uppercase">Default Timeframe</span>
                  <span className="font-semibold text-zinc-200">
                    {strategy.timeframe_default || "15m"}
                  </span>
                </div>
                <div className="bg-zinc-950/70 border border-zinc-800 rounded-lg p-3">
                  <span className="text-zinc-500 text-2xs block uppercase">Deployments</span>
                  <span className="font-semibold text-zinc-200">
                    {matchedInstances.length} live instance{matchedInstances.length === 1 ? "" : "s"}
                  </span>
                </div>
              </div>
            </div>

            {/* Indicator Logic & Entry/Exit Conditions */}
            <div className="bg-zinc-900/40 border border-zinc-800/80 rounded-xl p-5 space-y-4">
              <h3 className="text-xs font-bold uppercase tracking-wider text-zinc-300 flex items-center gap-2">
                <Sliders className="w-4 h-4 text-cyan-400" />
                <span>Signal Generation & Execution Logic</span>
              </h3>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {/* Entry Rules */}
                <div className="bg-zinc-950/80 border border-emerald-900/40 rounded-lg p-4 space-y-2">
                  <div className="flex items-center gap-2 text-emerald-400 text-xs font-semibold uppercase tracking-wider font-mono">
                    <TrendingUp className="w-4 h-4" />
                    <span>Entry Signal Conditions</span>
                  </div>
                  <ul className="text-xs text-zinc-300 space-y-1.5 list-disc list-inside leading-relaxed font-sans">
                    {strategy.slug === "ema-cross" && (
                      <>
                        <li>Fast EMA ({parameters.fast_period || 9}) crosses above Slow EMA ({parameters.slow_period || 21}).</li>
                        <li>Bar closed and committed by external data feed without lookahead bias.</li>
                        <li>Opens LONG position with size {parameters.trade_size || 0.01} units.</li>
                      </>
                    )}
                    {strategy.slug === "rsi-reversal" && (
                      <>
                        <li>RSI ({parameters.rsi_period || 14}) prints below oversold threshold ({parameters.oversold_threshold || 30.0}).</li>
                        <li>Subsequent bar prints higher close confirming oversold mean-reversion exhaustion.</li>
                        <li>Opens LONG position with size {parameters.trade_size || 0.01} units.</li>
                      </>
                    )}
                    {strategy.slug === "supertrend" && (
                      <>
                        <li>Close price breaks above upper dynamic ATR trailing band (period {parameters.atr_period || 10}, mult {parameters.multiplier || 3.0}).</li>
                        <li>Trend direction indicator flips from bearish to bullish.</li>
                        <li>Opens LONG breakout position with size {parameters.trade_size || 0.01} units.</li>
                      </>
                    )}
                    {!["ema-cross", "rsi-reversal", "supertrend"].includes(strategy.slug) && (
                      <>
                        <li>Evaluates quantitative multi-factor signals against parameter bounds.</li>
                        <li>Executes market orders upon regime and indicator trigger confirmation.</li>
                      </>
                    )}
                  </ul>
                </div>

                {/* Exit Rules */}
                <div className="bg-zinc-950/80 border border-rose-900/40 rounded-lg p-4 space-y-2">
                  <div className="flex items-center gap-2 text-rose-400 text-xs font-semibold uppercase tracking-wider font-mono">
                    <AlertTriangle className="w-4 h-4" />
                    <span>Exit & Risk Rules</span>
                  </div>
                  <ul className="text-xs text-zinc-300 space-y-1.5 list-disc list-inside leading-relaxed font-sans">
                    {strategy.slug === "ema-cross" && (
                      <>
                        <li>Fast EMA crosses below Slow EMA (reversal exit).</li>
                        <li>Immediate position liquidation or optional flip.</li>
                        <li>Respects portfolio risk limits and intraday drawdown latches.</li>
                      </>
                    )}
                    {strategy.slug === "rsi-reversal" && (
                      <>
                        <li>RSI recovers above exit threshold ({parameters.exit_threshold || 50.0}).</li>
                        <li>Mean reversion target reached; locks in realized profit.</li>
                        <li>Stops out on extreme volatility continuation.</li>
                      </>
                    )}
                    {strategy.slug === "supertrend" && (
                      <>
                        <li>Price closes below lower ATR trailing baseline.</li>
                        <li>Dynamic trailing stop tracks trend progression.</li>
                        <li>Closes position on trend exhaustion.</li>
                      </>
                    )}
                    {!["ema-cross", "rsi-reversal", "supertrend"].includes(strategy.slug) && (
                      <>
                        <li>Opposing signal trigger or indicator exit threshold reached.</li>
                        <li>Automated order liquidation via paper execution engine.</li>
                      </>
                    )}
                  </ul>
                </div>
              </div>
            </div>

            {/* Parameter Schema Inspector */}
            <div className="bg-zinc-900/40 border border-zinc-800/80 rounded-xl p-5 space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-bold uppercase tracking-wider text-zinc-300 flex items-center gap-2">
                  <Sliders className="w-4 h-4 text-cyan-400" />
                  <span>Configurable Parameter Schema</span>
                </h3>
                <span className="text-2xs text-zinc-500 font-mono">
                  {Object.keys(strategy.parameter_schema || {}).length} Parameters Defined
                </span>
              </div>

              {Object.keys(strategy.parameter_schema || {}).length === 0 ? (
                <div className="p-4 rounded-lg bg-zinc-950/60 border border-zinc-800 text-center text-xs text-zinc-500 font-mono">
                  No parameter schema defined for this strategy.
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left font-mono text-xs border-collapse">
                    <thead>
                      <tr className="border-b border-zinc-800 text-zinc-400 bg-zinc-950/60 text-2xs uppercase">
                        <th className="py-2.5 px-3">Parameter</th>
                        <th className="py-2.5 px-3">Type</th>
                        <th className="py-2.5 px-3">Default</th>
                        <th className="py-2.5 px-3">Bounds</th>
                        <th className="py-2.5 px-3">Description</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-zinc-800/60">
                      {Object.entries(strategy.parameter_schema || {}).map(([key, schema]: [string, any]) => (
                        <tr key={key} className="hover:bg-zinc-800/30">
                          <td className="py-2.5 px-3 font-bold text-cyan-300">
                            {schema.title || key}
                            <span className="block text-2xs font-normal text-zinc-500">{key}</span>
                          </td>
                          <td className="py-2.5 px-3 text-zinc-400 text-2xs uppercase">
                            {schema.type || "unknown"}
                          </td>
                          <td className="py-2.5 px-3 text-emerald-400 font-bold">
                            {String(schema.default ?? strategy.default_parameters?.[key] ?? "—")}
                          </td>
                          <td className="py-2.5 px-3 text-zinc-400 text-2xs">
                            {schema.minimum !== undefined || schema.maximum !== undefined ? (
                              <span>
                                [{schema.minimum ?? "—"} .. {schema.maximum ?? "—"}]
                              </span>
                            ) : (
                              "—"
                            )}
                          </td>
                          <td className="py-2.5 px-3 text-zinc-300 font-sans text-xs">
                            {schema.description || "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Tab 2: Quantitative Backtest & Tearsheet */}
        {activeTab === "backtest" && (
          <div className="p-6 flex-1 overflow-y-auto space-y-6">
            {/* Backtest Controls Bar */}
            <div className="bg-zinc-900/60 border border-zinc-800 rounded-xl p-4 space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Activity className="w-4 h-4 text-cyan-400" />
                  <span className="text-xs font-bold text-zinc-200 uppercase tracking-wider font-mono">
                    Vector Backtest Controls
                  </span>
                </div>
                <button
                  onClick={handleRunBacktest}
                  disabled={running}
                  className={cn(
                    "flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-bold uppercase tracking-wider font-mono shadow-md transition-all",
                    running
                      ? "bg-zinc-800 text-zinc-500 cursor-not-allowed"
                      : "bg-cyan-600 hover:bg-cyan-500 text-white shadow-cyan-900/40"
                  )}
                >
                  {running ? (
                    <>
                      <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                      <span>Backtesting...</span>
                    </>
                  ) : (
                    <>
                      <Play className="w-3.5 h-3.5 fill-current" />
                      <span>Run Backtest</span>
                    </>
                  )}
                </button>
              </div>

              {/* Form Controls */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono">
                <div>
                  <label className="block text-2xs text-zinc-400 mb-1 uppercase font-semibold">
                    Instrument
                  </label>
                  <select
                    value={instrumentId}
                    onChange={(e) => setInstrumentId(e.target.value)}
                    disabled={running}
                    className="w-full bg-zinc-950 border border-zinc-800 rounded px-2.5 py-1.5 text-zinc-200 text-xs focus:border-cyan-500 outline-none"
                  >
                    {instruments.length > 0 ? (
                      instruments.map((inst) => (
                        <option key={inst.id} value={inst.id}>
                          {inst.symbol || inst.id}
                        </option>
                      ))
                    ) : (
                      <>
                        <option value="BTCUSDT.BINANCE">BTCUSDT</option>
                        <option value="ETHUSDT.BINANCE">ETHUSDT</option>
                        <option value="SOLUSDT.BINANCE">SOLUSDT</option>
                      </>
                    )}
                  </select>
                </div>

                <div>
                  <label className="block text-2xs text-zinc-400 mb-1 uppercase font-semibold">
                    Timeframe
                  </label>
                  <select
                    value={timeframe}
                    onChange={(e) => setTimeframe(e.target.value)}
                    disabled={running}
                    className="w-full bg-zinc-950 border border-zinc-800 rounded px-2.5 py-1.5 text-zinc-200 text-xs focus:border-cyan-500 outline-none"
                  >
                    {TIMEFRAMES.map((tf) => (
                      <option key={tf} value={tf}>
                        {tf}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block text-2xs text-zinc-400 mb-1 uppercase font-semibold">
                    Lookback
                  </label>
                  <select
                    value={lookbackDays}
                    onChange={(e) => setLookbackDays(Number(e.target.value))}
                    disabled={running}
                    className="w-full bg-zinc-950 border border-zinc-800 rounded px-2.5 py-1.5 text-zinc-200 text-xs focus:border-cyan-500 outline-none"
                  >
                    <option value={1}>1 Day</option>
                    <option value={3}>3 Days</option>
                    <option value={7}>7 Days</option>
                    <option value={14}>14 Days</option>
                    <option value={30}>30 Days</option>
                    <option value={90}>90 Days</option>
                  </select>
                </div>

                <div>
                  <label className="block text-2xs text-zinc-400 mb-1 uppercase font-semibold">
                    Execution Profile
                  </label>
                  <select
                    value={executionProfile}
                    onChange={(e) => setExecutionProfile(e.target.value)}
                    disabled={running}
                    className="w-full bg-zinc-950 border border-zinc-800 rounded px-2.5 py-1.5 text-zinc-200 text-xs focus:border-cyan-500 outline-none"
                  >
                    <option value="conservative">Conservative (Slippage + Fees)</option>
                    <option value="optimistic">Optimistic (Costless)</option>
                  </select>
                </div>
              </div>

              {/* Error Alert */}
              {runError && (
                <div className="p-3 rounded-lg bg-rose-950/70 border border-rose-800 text-rose-300 text-xs flex items-center gap-2">
                  <AlertCircle className="w-4 h-4 shrink-0 text-rose-400" />
                  <span>{runError}</span>
                </div>
              )}
            </div>

            {/* Backtest Results Area */}
            {activeRun ? (
              <div className="space-y-6">
                {/* Run Info Banner */}
                <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 rounded-lg bg-zinc-900/60 border border-zinc-800 text-xs font-mono">
                  <div className="flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-emerald-400" />
                    <span className="text-zinc-400">Run ID:</span>
                    <span className="text-zinc-200 font-bold">{activeRun.id}</span>
                  </div>
                  <div className="flex items-center gap-3 text-zinc-400 text-2xs">
                    <span>
                      Instrument: <span className="text-zinc-200">{activeRun.instrument_id}</span>
                    </span>
                    <span>•</span>
                    <span>
                      Timeframe: <span className="text-zinc-200">{activeRun.timeframe}</span>
                    </span>
                    <span>•</span>
                    <span>
                      Profile: <span className="text-cyan-400 uppercase">{activeRun.execution_profile}</span>
                    </span>
                  </div>
                </div>

                {/* Tearsheet Cards */}
                {activeRun.results?.metrics && (
                  <TearsheetCards
                    metrics={activeRun.results.metrics}
                    executionProfile={activeRun.execution_profile}
                  />
                )}

                {/* Equity Curve Chart */}
                {activeRun.results?.downsampled_equity && activeRun.results.downsampled_equity.length > 0 && (
                  <div className="bg-zinc-900/40 border border-zinc-800 rounded-xl p-4 space-y-2">
                    <div className="flex items-center justify-between text-xs font-mono font-semibold text-zinc-300">
                      <span>Equity Curve vs Benchmark</span>
                      <span className="text-2xs text-zinc-500">
                        {activeRun.results.downsampled_equity.length} points
                      </span>
                    </div>
                    <div className="h-72 w-full">
                      <EquityCurveChart
                        equityPoints={activeRun.results.downsampled_equity}
                        selectedTrade={selectedTrade}
                      />
                    </div>
                  </div>
                )}

                {/* Sub-tabs: Heatmap & Trades */}
                <div className="space-y-4">
                  <div className="flex items-center gap-2 border-b border-zinc-800 pb-2">
                    <button
                      onClick={() => setBacktestSubTab("tearsheet")}
                      className={cn(
                        "text-xs font-mono font-semibold px-3 py-1 rounded transition-colors",
                        backtestSubTab === "tearsheet"
                          ? "bg-zinc-800 text-zinc-100"
                          : "text-zinc-500 hover:text-zinc-300"
                      )}
                    >
                      Monthly Heatmap
                    </button>
                    <button
                      onClick={() => setBacktestSubTab("trades")}
                      className={cn(
                        "text-xs font-mono font-semibold px-3 py-1 rounded transition-colors",
                        backtestSubTab === "trades"
                          ? "bg-zinc-800 text-zinc-100"
                          : "text-zinc-500 hover:text-zinc-300"
                      )}
                    >
                      Trades Table
                    </button>
                  </div>

                  {backtestSubTab === "tearsheet" && activeRun.results?.monthly_returns && (
                    <MonthlyHeatmap monthlyReturns={activeRun.results.monthly_returns} />
                  )}

                  {backtestSubTab === "trades" && (
                    <TradesTable
                      runId={activeRun.id}
                      onSelectTrade={setSelectedTrade}
                      selectedTrade={selectedTrade}
                    />
                  )}
                </div>
              </div>
            ) : (
              <div className="p-8 rounded-xl bg-zinc-900/30 border border-zinc-800/80 text-center space-y-3">
                <LineChart className="w-10 h-10 mx-auto text-zinc-600" />
                <h4 className="text-sm font-bold text-zinc-300">No Backtest Run Loaded</h4>
                <p className="text-xs text-zinc-500 max-w-md mx-auto">
                  Run a quantitative backtest using the controls above to inspect equity curves, Sharpe/Sortino ratios, drawdown tearsheet, and trade history.
                </p>
                <button
                  onClick={handleRunBacktest}
                  disabled={running}
                  className="px-4 py-2 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white font-mono text-xs font-bold uppercase tracking-wider inline-flex items-center gap-2 shadow-md"
                >
                  <Play className="w-3.5 h-3.5 fill-current" />
                  <span>Execute First Backtest</span>
                </button>
              </div>
            )}
          </div>
        )}

        {/* Tab 3: Live Paper Instances */}
        {activeTab === "live" && (
          <div className="p-6 flex-1 overflow-y-auto space-y-6">
            {/* Live Instances Header */}
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <h3 className="text-sm font-bold text-zinc-200 uppercase tracking-wider font-mono flex items-center gap-2">
                  <Cpu className="w-4 h-4 text-emerald-400" />
                  <span>Active Paper Instances</span>
                </h3>
                <p className="text-xs text-zinc-400 mt-0.5 font-sans">
                  Autonomous trading instances running in the execution paper engine.
                </p>
              </div>

              <button
                onClick={() => setShowDeployForm((prev) => !prev)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-mono font-bold tracking-wider uppercase transition-colors shadow-sm"
              >
                <Plus className="w-4 h-4" />
                <span>Deploy New Instance</span>
              </button>
            </div>

            {/* Deploy New Instance Form Drawer/Card */}
            {showDeployForm && (
              <form
                onSubmit={handleDeployInstance}
                className="p-5 rounded-xl border border-emerald-500/40 bg-zinc-900/90 shadow-xl space-y-4 animate-in fade-in duration-150 font-mono text-xs"
              >
                <div className="flex items-center justify-between pb-2 border-b border-zinc-800">
                  <span className="font-bold text-emerald-400 uppercase tracking-wider flex items-center gap-2">
                    <Plus className="w-4 h-4" />
                    <span>Deploy Paper Instance: {strategy.name}</span>
                  </span>
                  <button
                    type="button"
                    onClick={() => setShowDeployForm(false)}
                    className="text-zinc-500 hover:text-zinc-300"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div>
                    <label className="block text-2xs text-zinc-400 mb-1 uppercase font-semibold">
                      Instrument
                    </label>
                    <select
                      value={deployInstrument}
                      onChange={(e) => setDeployInstrument(e.target.value)}
                      className="w-full bg-zinc-950 border border-zinc-800 rounded px-2.5 py-1.5 text-zinc-200 text-xs focus:border-emerald-500 outline-none"
                    >
                      {instruments.length > 0 ? (
                        instruments.map((inst) => (
                          <option key={inst.id} value={inst.id}>
                            {inst.symbol || inst.id}
                          </option>
                        ))
                      ) : (
                        <>
                          <option value="BTCUSDT.BINANCE">BTCUSDT</option>
                          <option value="ETHUSDT.BINANCE">ETHUSDT</option>
                          <option value="SOLUSDT.BINANCE">SOLUSDT</option>
                        </>
                      )}
                    </select>
                  </div>

                  <div>
                    <label className="block text-2xs text-zinc-400 mb-1 uppercase font-semibold">
                      Timeframe
                    </label>
                    <select
                      value={deployTimeframe}
                      onChange={(e) => setDeployTimeframe(e.target.value)}
                      className="w-full bg-zinc-950 border border-zinc-800 rounded px-2.5 py-1.5 text-zinc-200 text-xs focus:border-emerald-500 outline-none"
                    >
                      {TIMEFRAMES.map((tf) => (
                        <option key={tf} value={tf}>
                          {tf}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div>
                    <label className="block text-2xs text-zinc-400 mb-1 uppercase font-semibold">
                      Allocation USD
                    </label>
                    <div className="relative">
                      <span className="absolute left-2.5 top-1.5 text-zinc-500">$</span>
                      <input
                        type="number"
                        min={100}
                        max={1000000}
                        step={100}
                        value={deployAllocation}
                        onChange={(e) => setDeployAllocation(Number(e.target.value))}
                        className="w-full bg-zinc-950 border border-zinc-800 rounded pl-6 pr-2.5 py-1.5 text-zinc-200 text-xs focus:border-emerald-500 outline-none"
                      />
                    </div>
                  </div>
                </div>

                <div className="flex items-center justify-end gap-2 pt-2">
                  <button
                    type="button"
                    onClick={() => setShowDeployForm(false)}
                    className="px-3 py-1.5 rounded border border-zinc-800 text-zinc-400 hover:bg-zinc-800 text-xs"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={isDeploying}
                    className="px-4 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs flex items-center gap-2 shadow-sm"
                  >
                    {isDeploying ? (
                      <>
                        <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                        <span>Deploying...</span>
                      </>
                    ) : (
                      <>
                        <CheckCircle2 className="w-3.5 h-3.5" />
                        <span>Confirm Deploy</span>
                      </>
                    )}
                  </button>
                </div>
              </form>
            )}

            {/* Instances List */}
            {matchedInstances.length === 0 ? (
              <div className="p-8 rounded-xl bg-zinc-900/30 border border-zinc-800/80 text-center space-y-3 font-mono">
                <Cpu className="w-10 h-10 mx-auto text-zinc-600" />
                <h4 className="text-sm font-bold text-zinc-300">No Instances Currently Deployed</h4>
                <p className="text-xs text-zinc-500 max-w-md mx-auto font-sans">
                  Deploy a live paper instance of this strategy to allow autonomous order generation, real-time position tracking, and execution testing.
                </p>
                <button
                  onClick={() => setShowDeployForm(true)}
                  className="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-mono text-xs font-bold uppercase tracking-wider inline-flex items-center gap-2 shadow-md"
                >
                  <Plus className="w-3.5 h-3.5" />
                  <span>Deploy First Instance</span>
                </button>
              </div>
            ) : (
              <div className="space-y-4">
                {matchedInstances.map((st) => {
                  const stats = strategyStats(positions, st.instance_id);
                  const isAi = isAiMandate(st.instance_id);
                  const instPositions = positions.filter(
                    (p) => p.instance_id === st.instance_id && p.status === "OPEN"
                  );

                  return (
                    <div
                      key={st.instance_id}
                      className={cn(
                        "rounded-xl border bg-zinc-900/50 p-4 space-y-3 font-mono text-xs transition-all",
                        st.status === "RUNNING"
                          ? "border-emerald-500/30 shadow-xs shadow-emerald-950/20"
                          : st.status === "PAUSED"
                          ? "border-amber-500/30"
                          : "border-zinc-800"
                      )}
                    >
                      {/* Top row: Status, ID, Instrument, Actions */}
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="flex items-center gap-2.5">
                          <span
                            className={cn(
                              "w-2.5 h-2.5 rounded-full shrink-0",
                              st.status === "RUNNING"
                                ? "bg-emerald-400 animate-pulse ring-2 ring-emerald-400/30"
                                : st.status === "PAUSED"
                                ? "bg-amber-400 ring-2 ring-amber-400/30"
                                : "bg-zinc-600"
                            )}
                          />
                          <span className="font-bold text-zinc-100">{st.instance_id}</span>
                          <span
                            className={cn(
                              "px-2 py-0.5 rounded text-2xs font-extrabold uppercase",
                              st.status === "RUNNING"
                                ? "bg-emerald-950/80 text-emerald-300 border border-emerald-800/60"
                                : st.status === "PAUSED"
                                ? "bg-amber-950/80 text-amber-300 border border-amber-800/60"
                                : "bg-zinc-800 text-zinc-400"
                            )}
                          >
                            {st.status}
                          </span>
                        </div>

                        {/* Controls: Pause / Resume / Stop */}
                        <div className="flex items-center gap-1.5">
                          {isAi ? (
                            <span
                              className="text-2xs text-purple-400 border border-purple-800/50 bg-purple-950/40 px-2 py-1 rounded"
                              title="Governed via Mission Control"
                            >
                              AI Mandate Control
                            </span>
                          ) : (
                            <>
                              {st.status === "RUNNING" && (
                                <button
                                  onClick={() => handleInstanceCommand(st.instance_id, "pause")}
                                  className="p-1.5 rounded border border-amber-800/60 bg-amber-950/30 text-amber-300 hover:bg-amber-900/50 transition-colors"
                                  title="Pause strategy execution"
                                >
                                  <Pause className="w-3.5 h-3.5" />
                                </button>
                              )}
                              {st.status === "PAUSED" && (
                                <button
                                  onClick={() => handleInstanceCommand(st.instance_id, "resume")}
                                  className="p-1.5 rounded border border-emerald-800/60 bg-emerald-950/30 text-emerald-300 hover:bg-emerald-900/50 transition-colors"
                                  title="Resume strategy execution"
                                >
                                  <Play className="w-3.5 h-3.5 fill-current" />
                                </button>
                              )}
                              {st.status !== "STOPPED" && (
                                <button
                                  onClick={() => handleInstanceCommand(st.instance_id, "stop")}
                                  className="p-1.5 rounded border border-rose-800/60 bg-rose-950/30 text-rose-300 hover:bg-rose-900/50 transition-colors"
                                  title="Stop and terminate instance"
                                >
                                  <Square className="w-3.5 h-3.5 fill-current" />
                                </button>
                              )}
                            </>
                          )}
                        </div>
                      </div>

                      {/* Specs and Metrics Row */}
                      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-zinc-950/60 border border-zinc-800/80 rounded-lg p-3 text-2xs">
                        <div>
                          <span className="text-zinc-500 block">Instrument</span>
                          <span className="font-bold text-zinc-200">{st.instrument_id}</span>
                        </div>
                        <div>
                          <span className="text-zinc-500 block">Timeframe</span>
                          <span className="font-bold text-zinc-200">{st.timeframe}</span>
                        </div>
                        <div>
                          <span className="text-zinc-500 block">Allocation USD</span>
                          <span className="font-bold text-zinc-200">
                            {fmtCompactUsd(Number(st.allocation_usd))}
                          </span>
                        </div>
                        <div>
                          <span className="text-zinc-500 block">Open Positions</span>
                          <span className="font-bold text-zinc-200">
                            {instPositions.length} active
                          </span>
                        </div>
                      </div>

                      {/* 7-day Realized Performance */}
                      <div className="flex items-center justify-between pt-1">
                        <div className="flex items-center gap-3">
                          <Sparkline values={stats.curve7d} label={`${st.instance_id} 7d PnL`} />
                          <div className="text-2xs">
                            <span className="text-zinc-500">7d Realized: </span>
                            <span
                              className={cn(
                                "font-bold tabular-nums",
                                stats.pnl7d > 0
                                  ? "text-emerald-400"
                                  : stats.pnl7d < 0
                                  ? "text-rose-400"
                                  : "text-zinc-400"
                              )}
                            >
                              {fmtSignedUsd(stats.pnl7d)}
                            </span>
                          </div>
                        </div>

                        <div className="text-2xs text-zinc-400 flex items-center gap-3">
                          <span>
                            Win Rate:{" "}
                            <span className="font-bold text-zinc-200">
                              {stats.winRate === null ? "—" : fmtPct(stats.winRate, 0)}
                            </span>
                          </span>
                          <span>•</span>
                          <span>
                            Closed Trades:{" "}
                            <span className="font-bold text-zinc-200">{stats.closedCount}</span>
                          </span>
                        </div>
                      </div>

                      {/* Open Positions List for this instance */}
                      {instPositions.length > 0 && (
                        <div className="pt-2 border-t border-zinc-800 space-y-1.5">
                          <span className="text-2xs text-zinc-400 block font-semibold uppercase">
                            Open Positions
                          </span>
                          {instPositions.map((p) => (
                            <div
                              key={p.position_id}
                              className="flex items-center justify-between p-2 rounded bg-zinc-950 border border-zinc-800 text-2xs"
                            >
                              <div className="flex items-center gap-2">
                                <span
                                  className={cn(
                                    "px-1.5 py-0.2 rounded font-extrabold uppercase",
                                    p.side === "LONG"
                                      ? "bg-emerald-950 text-emerald-400 border border-emerald-800"
                                      : "bg-rose-950 text-rose-400 border border-rose-800"
                                  )}
                                >
                                  {p.side}
                                </span>
                                <span className="font-bold text-zinc-200">{p.instrument_id}</span>
                                <span className="text-zinc-400">Qty: {p.quantity}</span>
                                <span className="text-zinc-400">Entry: ${p.entry_price}</span>
                              </div>

                              <div className="flex items-center gap-3">
                                <div>
                                  <span className="text-zinc-500">Unrealized: </span>
                                  <span
                                    className={cn(
                                      "font-bold tabular-nums",
                                      p.unrealized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"
                                    )}
                                  >
                                    {fmtSignedUsd(p.unrealized_pnl)}
                                  </span>
                                </div>
                                <button
                                  onClick={() => closePosition(st.instance_id, p.instrument_id)}
                                  className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-rose-900/60 hover:text-rose-200 text-zinc-300 transition-colors"
                                >
                                  Close
                                </button>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
