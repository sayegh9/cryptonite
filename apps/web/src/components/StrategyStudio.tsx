"use client";

import React, { useState, useEffect, useMemo, useCallback } from "react";
import {
  FlaskConical,
  Plus,
  RefreshCw,
  Search,
  X,
  ArrowUpRight,
} from "lucide-react";
import { Strategy } from "@/lib/types";
import {
  StrategyIdentityBadge,
  MarketRegimePill,
  StrategyInspectorDrawer,
} from "./strategies";
import {
  usePortfolioStore,
  usePortfolioSync,
  refreshPortfolio,
} from "@/lib/stores/usePortfolioStore";
import {
  strategyStats,
  fmtSignedUsd,
  fmtPct,
  fmtCompactUsd,
  notionalOf,
} from "@/lib/portfolio/metrics";
import { Sparkline } from "./portfolio/Sparkline";
import { StrategyManagerModal } from "./ResearchLab/StrategyManagerModal";
import { fetchApi } from "@/lib/api";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

type FilterTab = "all" | "running" | "trend" | "mean_reversion" | "breakout";

export function StrategyStudio() {
  // Sync live portfolio & paper strategies at 3s interval
  usePortfolioSync();

  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [selectedFilter, setSelectedFilter] = useState<FilterTab>("all");

  // Selection for drawer
  const [selectedStrategyForDrawer, setSelectedStrategyForDrawer] = useState<Strategy | null>(null);
  const [drawerInitialTab, setDrawerInitialTab] = useState<"overview" | "backtest" | "live">("overview");

  // Strategy manager modal
  const [isManagerModalOpen, setIsManagerModalOpen] = useState<boolean>(false);

  // Live instances & positions from store
  const liveInstances = usePortfolioStore((s) => s.strategies);
  const positions = usePortfolioStore((s) => s.positions);

  // Fetch strategies from GET /api/v1/strategies
  const fetchStrategies = useCallback(async (isUserRefresh = false) => {
    if (isUserRefresh) setRefreshing(true);
    try {
      const data: Strategy[] = await fetchApi("/strategies");
      if (Array.isArray(data)) {
        // Deduplicate by slug
        const unique = Array.from(new Map(data.map((s) => [s.slug, s])).values());
        setStrategies(unique);
        if (isUserRefresh) {
          toast.success("Strategies refreshed");
        }
      }
    } catch (err: any) {
      console.error("Failed to fetch strategies:", err);
      toast.error("Failed to load strategies", {
        description: err.message || "Could not connect to API",
      });
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchStrategies();
  }, [fetchStrategies]);

  const handleRefreshAll = async () => {
    setRefreshing(true);
    await Promise.all([fetchStrategies(true), refreshPortfolio()]);
    setRefreshing(false);
  };

  // Pre-calculate per-strategy instance map and running status
  const strategyStatsMap = useMemo(() => {
    const map = new Map<
      string,
      {
        instances: typeof liveInstances;
        status: "RUNNING" | "PAUSED" | "STOPPED" | "IDLE";
        runningCount: number;
        totalAllocated: number;
        capitalInUse: number;
        totalPnl7d: number;
        curve7d: number[];
        winRate: number | null;
      }
    >();

    for (const strat of strategies) {
      const slugLower = strat.slug.toLowerCase();
      const nameLower = strat.name.toLowerCase();
      const matched = liveInstances.filter(
        (st) =>
          st.strategy_name.toLowerCase() === slugLower ||
          st.strategy_name.toLowerCase() === nameLower
      );

      const runningCount = matched.filter((i) => i.status === "RUNNING").length;
      const status: "RUNNING" | "PAUSED" | "STOPPED" | "IDLE" =
        matched.length === 0
          ? "IDLE"
          : runningCount > 0
          ? "RUNNING"
          : matched.some((i) => i.status === "PAUSED")
          ? "PAUSED"
          : "STOPPED";

      const totalAllocated = matched.reduce((acc, i) => acc + Number(i.allocation_usd || 0), 0);

      const instIds = new Set(matched.map((i) => i.instance_id));
      const openPositions = positions.filter((p) => instIds.has(p.instance_id) && p.status === "OPEN");
      const capitalInUse = openPositions.reduce((acc, p) => acc + notionalOf(p), 0);

      let totalPnl7d = 0;
      let totalWins = 0;
      let totalClosed = 0;
      const combined7d = [0, 0, 0, 0, 0, 0, 0];

      for (const inst of matched) {
        const st = strategyStats(positions, inst.instance_id);
        totalPnl7d += st.pnl7d;
        for (let d = 0; d < 7; d++) {
          combined7d[d] += st.curve7d[d] || 0;
        }
        if (st.winRate !== null) {
          totalWins += Math.round(st.winRate * st.closedCount);
          totalClosed += st.closedCount;
        }
      }

      const winRate = totalClosed > 0 ? totalWins / totalClosed : null;

      map.set(strat.slug, {
        instances: matched,
        status,
        runningCount,
        totalAllocated,
        capitalInUse,
        totalPnl7d,
        curve7d: combined7d,
        winRate,
      });
    }

    return map;
  }, [strategies, liveInstances, positions]);

  // Filter counts
  const counts = useMemo(() => {
    let running = 0;
    let trend = 0;
    let mean = 0;
    let breakout = 0;

    for (const s of strategies) {
      const stats = strategyStatsMap.get(s.slug);
      if (stats && stats.runningCount > 0) running++;

      const catLower = (s.category || "").toLowerCase();
      if (catLower.includes("trend")) trend++;
      if (catLower.includes("mean") || catLower.includes("revert")) mean++;
      if (catLower.includes("breakout") || catLower.includes("momentum")) breakout++;
    }

    return {
      all: strategies.length,
      running,
      trend,
      mean,
      breakout,
    };
  }, [strategies, strategyStatsMap]);

  // Filtered strategies
  const filteredStrategies = useMemo(() => {
    return strategies.filter((s) => {
      // Search query filter
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim();
        const nameMatch = s.name.toLowerCase().includes(q);
        const slugMatch = s.slug.toLowerCase().includes(q);
        const thesisMatch = (s.thesis || "").toLowerCase().includes(q);
        const descMatch = (s.description || "").toLowerCase().includes(q);
        const catMatch = (s.category || "").toLowerCase().includes(q);
        const regimeMatch = (s.market_regime || "").toLowerCase().includes(q);
        const paramMatch = Object.keys(s.default_parameters || {}).some((k) =>
          k.toLowerCase().includes(q)
        );

        if (!nameMatch && !slugMatch && !thesisMatch && !descMatch && !catMatch && !regimeMatch && !paramMatch) {
          return false;
        }
      }

      // Tab filter
      if (selectedFilter === "running") {
        const stats = strategyStatsMap.get(s.slug);
        return stats && stats.runningCount > 0;
      }
      if (selectedFilter === "trend") {
        return (s.category || "").toLowerCase().includes("trend");
      }
      if (selectedFilter === "mean_reversion") {
        const cat = (s.category || "").toLowerCase();
        return cat.includes("mean") || cat.includes("revert");
      }
      if (selectedFilter === "breakout") {
        const cat = (s.category || "").toLowerCase();
        return cat.includes("breakout") || cat.includes("momentum");
      }

      return true;
    });
  }, [strategies, searchQuery, selectedFilter, strategyStatsMap]);

  const openDrawer = (strategy: Strategy, tab: "overview" | "backtest" | "live" = "overview") => {
    setSelectedStrategyForDrawer(strategy);
    setDrawerInitialTab(tab);
  };

  return (
    <div className="flex flex-col h-full bg-zinc-950 text-zinc-100 overflow-y-auto">
      {/* Top Header */}
      <div className="border-b border-zinc-800 bg-zinc-900/40 px-6 py-5">
        <div className="max-w-7xl mx-auto flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-xl bg-cyan-950/60 border border-cyan-500/40 text-cyan-400 shadow-sm">
                <FlaskConical className="w-5 h-5" />
              </div>
              <div>
                <h1 className="text-xl md:text-2xl font-black tracking-tight text-zinc-100">
                  Strategy Lifecycle Studio
                </h1>
                <p className="text-xs text-zinc-400 mt-0.5">
                  Design, backtest, inspect, and deploy autonomous trading strategies
                </p>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2.5 shrink-0">
            <button
              onClick={handleRefreshAll}
              disabled={refreshing}
              className="flex items-center gap-2 px-3 py-2 rounded-lg bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-300 hover:text-zinc-100 font-mono text-xs font-semibold transition-all shadow-xs"
              title="Refresh strategies and live portfolio instances"
            >
              <RefreshCw className={cn("w-3.5 h-3.5", refreshing && "animate-spin text-cyan-400")} />
              <span>Refresh</span>
            </button>

            <button
              onClick={() => setIsManagerModalOpen(true)}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white font-mono text-xs font-bold uppercase tracking-wider transition-all shadow-md shadow-cyan-950/40 hover:shadow-cyan-900/60"
            >
              <Plus className="w-4 h-4" />
              <span>New Strategy</span>
            </button>
          </div>
        </div>
      </div>

      {/* Filter and Search Bar */}
      <div className="border-b border-zinc-800/80 bg-zinc-900/20 px-6 py-3.5">
        <div className="max-w-7xl mx-auto flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          {/* Filter Pills */}
          <div className="flex items-center gap-1.5 overflow-x-auto pb-1 sm:pb-0 font-mono text-xs">
            <button
              onClick={() => setSelectedFilter("all")}
              className={cn(
                "px-3 py-1.5 rounded-lg font-semibold transition-all select-none whitespace-nowrap",
                selectedFilter === "all"
                  ? "bg-zinc-800 text-zinc-100 border border-zinc-700 shadow-xs"
                  : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
              )}
            >
              All <span className="text-2xs text-zinc-500">({counts.all})</span>
            </button>

            <button
              onClick={() => setSelectedFilter("running")}
              className={cn(
                "px-3 py-1.5 rounded-lg font-semibold transition-all select-none whitespace-nowrap flex items-center gap-1.5",
                selectedFilter === "running"
                  ? "bg-emerald-950/70 text-emerald-300 border border-emerald-700/60 shadow-xs"
                  : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
              )}
            >
              <span className="w-2 h-2 rounded-full bg-emerald-400" />
              <span>Running Live</span>
              <span className="text-2xs opacity-75">({counts.running})</span>
            </button>

            <button
              onClick={() => setSelectedFilter("trend")}
              className={cn(
                "px-3 py-1.5 rounded-lg font-semibold transition-all select-none whitespace-nowrap",
                selectedFilter === "trend"
                  ? "bg-emerald-950/50 text-emerald-300 border border-emerald-600/50 shadow-xs"
                  : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
              )}
            >
              Trend Following <span className="text-2xs opacity-75">({counts.trend})</span>
            </button>

            <button
              onClick={() => setSelectedFilter("mean_reversion")}
              className={cn(
                "px-3 py-1.5 rounded-lg font-semibold transition-all select-none whitespace-nowrap",
                selectedFilter === "mean_reversion"
                  ? "bg-indigo-950/70 text-indigo-300 border border-indigo-700/60 shadow-xs"
                  : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
              )}
            >
              Mean Reversion <span className="text-2xs opacity-75">({counts.mean})</span>
            </button>

            <button
              onClick={() => setSelectedFilter("breakout")}
              className={cn(
                "px-3 py-1.5 rounded-lg font-semibold transition-all select-none whitespace-nowrap",
                selectedFilter === "breakout"
                  ? "bg-amber-950/70 text-amber-300 border border-amber-700/60 shadow-xs"
                  : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
              )}
            >
              Breakout <span className="text-2xs opacity-75">({counts.breakout})</span>
            </button>
          </div>

          {/* Search Box */}
          <div className="relative w-full sm:w-72">
            <Search className="w-4 h-4 text-zinc-500 absolute left-3 top-2.5 pointer-events-none" />
            <input
              type="text"
              aria-label="Search strategies by name, thesis, or indicator"
              placeholder="Search strategy, thesis, indicator..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-9 pr-8 py-1.5 text-xs text-zinc-200 placeholder:text-zinc-500 focus:outline-none focus:border-cyan-500 transition-colors"
            />
            {searchQuery && (
              <button
                onClick={() => setSearchQuery("")}
                className="absolute right-2.5 top-2.5 text-zinc-500 hover:text-zinc-300"
                aria-label="Clear search"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Main Content Area */}
      <div className="flex-1 p-6 max-w-7xl mx-auto w-full">
        {loading ? (
          /* Skeleton Loading Cards */
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5 animate-pulse">
            {[1, 2, 3].map((i) => (
              <div
                key={i}
                className="h-64 rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-5 space-y-4"
              >
                <div className="flex items-center justify-between">
                  <div className="h-5 w-32 bg-zinc-800 rounded" />
                  <div className="h-5 w-20 bg-zinc-800 rounded-full" />
                </div>
                <div className="h-10 w-full bg-zinc-800/50 rounded" />
                <div className="h-16 w-full bg-zinc-800/30 rounded" />
                <div className="h-4 w-28 bg-zinc-800 rounded" />
              </div>
            ))}
          </div>
        ) : filteredStrategies.length === 0 ? (
          /* Empty State */
          <div className="p-12 text-center rounded-2xl border border-zinc-800/80 bg-zinc-900/30 max-w-md mx-auto my-12 space-y-4">
            <div className="w-12 h-12 rounded-xl bg-zinc-900 border border-zinc-800 flex items-center justify-center mx-auto text-zinc-500">
              <FlaskConical className="w-6 h-6" />
            </div>
            <div>
              <h3 className="text-base font-bold text-zinc-200">
                {searchQuery || selectedFilter !== "all"
                  ? "No matching strategies"
                  : "No strategies registered"}
              </h3>
              <p className="text-xs text-zinc-400 mt-1 max-w-xs mx-auto">
                {searchQuery || selectedFilter !== "all"
                  ? "Try adjusting your search terms or filter selection to find registered strategies."
                  : "No strategies found in the registry. Create a new quantitative trading strategy to begin."}
              </p>
            </div>
            {searchQuery || selectedFilter !== "all" ? (
              <button
                onClick={() => {
                  setSearchQuery("");
                  setSelectedFilter("all");
                }}
                className="px-4 py-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-xs font-mono font-semibold"
              >
                Reset Filters
              </button>
            ) : (
              <button
                onClick={() => setIsManagerModalOpen(true)}
                className="px-4 py-2 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-mono font-bold uppercase tracking-wider inline-flex items-center gap-2 shadow-md"
              >
                <Plus className="w-4 h-4" />
                <span>Create First Strategy</span>
              </button>
            )}
          </div>
        ) : (
          /* Strategy Cards Grid */
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
            {filteredStrategies.map((strat) => {
              const stats = strategyStatsMap.get(strat.slug) || {
                instances: [],
                status: "IDLE" as const,
                runningCount: 0,
                totalAllocated: 0,
                capitalInUse: 0,
                totalPnl7d: 0,
                curve7d: [0, 0, 0, 0, 0, 0, 0],
                winRate: null,
              };

              return (
                <div
                  key={strat.slug}
                  role="button"
                  tabIndex={0}
                  aria-label={`Inspect ${strat.name} strategy`}
                  onClick={() => openDrawer(strat, "overview")}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      openDrawer(strat, "overview");
                    }
                  }}
                  className={cn(
                    "group relative rounded-xl border bg-zinc-900/50 hover:bg-zinc-900/80 p-5 cursor-pointer transition-all duration-200 flex flex-col justify-between shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400 focus-visible:ring-offset-2 focus-visible:ring-offset-zinc-950",
                    stats.status === "RUNNING"
                      ? "border-emerald-500/30 hover:border-emerald-400/60 hover:shadow-emerald-950/20"
                      : stats.status === "PAUSED"
                      ? "border-amber-500/30 hover:border-amber-400/60"
                      : "border-zinc-800/80 hover:border-cyan-500/50 hover:shadow-cyan-950/20",
                    "hover:-translate-y-0.5"
                  )}
                >
                  {/* Card Top Row */}
                  <div className="space-y-3">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <StrategyIdentityBadge
                        name={strat.name}
                        category={strat.category}
                        status={stats.status}
                        timeframe={strat.timeframe_default || "15m"}
                        size="md"
                      />
                      <MarketRegimePill
                        regime={strat.market_regime || "High Volatility Trends"}
                      />
                    </div>

                    {/* Plain-English Thesis Snippet (2 lines truncated) */}
                    <div className="relative pl-3 border-l-2 border-cyan-500/50 py-0.5">
                      <p className="line-clamp-2 text-xs italic text-zinc-300 font-sans leading-relaxed group-hover:text-zinc-100 transition-colors">
                        "{strat.thesis || strat.description || "Autonomous quantitative alpha strategy."}"
                      </p>
                    </div>

                    {/* Metrics Section: Live / Historical Performance */}
                    <div className="pt-2 pb-1 bg-zinc-950/60 border border-zinc-800/60 rounded-lg p-3 font-mono text-2xs space-y-2">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <Sparkline
                            values={stats.curve7d}
                            width={72}
                            height={20}
                            label={`${strat.slug} 7d cumulative PnL`}
                          />
                          <div>
                            <span className="text-zinc-500 block text-[10px]">7d Realized</span>
                            <span
                              className={cn(
                                "font-bold tabular-nums",
                                stats.totalPnl7d > 0
                                  ? "text-emerald-400"
                                  : stats.totalPnl7d < 0
                                  ? "text-rose-400"
                                  : "text-zinc-400"
                              )}
                            >
                              {fmtSignedUsd(stats.totalPnl7d)}
                            </span>
                          </div>
                        </div>

                        <div className="text-right">
                          <span className="text-zinc-500 block text-[10px]">Win Rate</span>
                          <span
                            className={cn(
                              "font-bold",
                              stats.winRate === null
                                ? "text-zinc-500"
                                : stats.winRate >= 0.5
                                ? "text-emerald-400"
                                : "text-amber-400"
                            )}
                          >
                            {stats.winRate === null ? "—" : fmtPct(stats.winRate, 0)}
                          </span>
                        </div>
                      </div>

                      {/* Capital Allocation Bar */}
                      <div className="flex items-center justify-between pt-1 border-t border-zinc-850 text-[10px] text-zinc-400">
                        <span>
                          Capital:{" "}
                          <strong className="text-zinc-200">
                            {fmtCompactUsd(stats.capitalInUse)}
                          </strong>{" "}
                          in use
                        </span>
                        <span>
                          Allocated:{" "}
                          <strong className="text-zinc-200">
                            {fmtCompactUsd(stats.totalAllocated)}
                          </strong>
                        </span>
                      </div>
                    </div>
                  </div>

                  {/* Card Bottom / Footer Row */}
                  <div className="pt-4 border-t border-zinc-800/70 flex items-center justify-between text-2xs font-mono mt-4">
                    <div className="flex items-center gap-2">
                      {stats.runningCount > 0 ? (
                        <span className="inline-flex items-center gap-1.5 text-emerald-400 font-semibold">
                          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                          <span>{stats.runningCount} instance{stats.runningCount === 1 ? "" : "s"} running</span>
                        </span>
                      ) : stats.instances.length > 0 ? (
                        <span className="inline-flex items-center gap-1.5 text-zinc-400">
                          <span className="w-2 h-2 rounded-full bg-zinc-500" />
                          <span>{stats.instances.length} deployed ({stats.instances[0].status.toLowerCase()})</span>
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 text-zinc-500">
                          <span className="w-1.5 h-1.5 rounded-full bg-zinc-600" />
                          <span>Ready to backtest & deploy</span>
                        </span>
                      )}
                    </div>

                    <span className="inline-flex items-center gap-1 font-bold text-cyan-400 group-hover:text-cyan-300 transition-colors">
                      <span>Inspect</span>
                      <ArrowUpRight className="w-3.5 h-3.5 group-hover:translate-x-0.5 group-hover:-translate-y-0.5 transition-transform" />
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Slide-over Strategy Inspector Drawer */}
      <StrategyInspectorDrawer
        strategy={selectedStrategyForDrawer}
        isOpen={selectedStrategyForDrawer !== null}
        onClose={() => setSelectedStrategyForDrawer(null)}
        initialTab={drawerInitialTab}
        onStrategyUpdated={() => {
          fetchStrategies();
          void refreshPortfolio();
        }}
      />

      {/* Strategy Manager Modal (when clicking "+ New Strategy") */}
      {isManagerModalOpen && (
        <StrategyManagerModal
          onClose={() => setIsManagerModalOpen(false)}
          onStrategiesChanged={() => {
            fetchStrategies();
            void refreshPortfolio();
          }}
        />
      )}
    </div>
  );
}
