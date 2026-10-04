"use client";

import React, { useMemo } from "react";
import { Pause, Play, Square, TrendingUp } from "lucide-react";
import { usePortfolioStore, type StrategyInstance } from "@/lib/stores/usePortfolioStore";
import { postCommand } from "@/lib/portfolio/actions";
import { fmtCompactUsd, fmtPct, fmtSignedUsd, strategyStats, type StrategyStats } from "@/lib/portfolio/metrics";
import { Sparkline } from "./Sparkline";

const SYSTEM_INSTANCES = new Set(["MANUAL_TRADE", "AI_DEBATE_WORKER"]);
const SEGMENTS = 10;

function StatusDot({ status }: { status: string }) {
  if (status === "RUNNING") {
    return (
      <span className="relative flex h-2 w-2" aria-hidden>
        <span className="absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-60 animate-ping" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" />
      </span>
    );
  }
  const color = status === "PAUSED" ? "bg-amber-400" : status === "STOPPED" ? "bg-zinc-600" : "bg-red-500";
  return <span className={`inline-flex h-2 w-2 rounded-full ${color}`} aria-hidden />;
}

const statusText = (s: string) =>
  s === "RUNNING" ? "text-emerald-400" : s === "PAUSED" ? "text-amber-400" : s === "STOPPED" ? "text-zinc-500" : "text-red-400";

/** Capital in use vs. allocated, as discrete segments (over-deployment turns the bar red). */
function AllocationBar({ used, allocated }: { used: number; allocated: number }) {
  const ratio = allocated > 0 ? used / allocated : 0;
  const filled = Math.min(SEGMENTS, Math.round(ratio * SEGMENTS));
  const over = ratio > 1.001;
  return (
    <div
      role="meter"
      aria-label="Allocation in use"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(Math.min(ratio, 1) * 100)}
      className="flex gap-[2px]"
    >
      {Array.from({ length: SEGMENTS }, (_, i) => (
        <span
          key={i}
          className={`h-1.5 flex-1 rounded-[1px] ${i < filled ? (over ? "bg-red-500" : ratio > 0.8 ? "bg-amber-400" : "bg-cyan-400") : "bg-zinc-800"}`}
        />
      ))}
    </div>
  );
}

interface CardProps {
  st: StrategyInstance;
  stats: StrategyStats;
  equity: number;
  onSelect: (id: string) => void;
}

const iconBtn = "p-1 rounded border outline-none transition-all focus-visible:ring-2";

const StrategyRow = React.memo(function StrategyRow({ st, stats, equity, onSelect }: CardProps) {
  const command = (verb: "pause" | "resume" | "stop", next: string) => {
    usePortfolioStore.getState().setStrategyStatusOptimistic(st.instance_id, next);
    void postCommand(`/paper/strategies/${st.instance_id}/${verb}`, undefined, `Could not ${verb} ${st.strategy_name}`);
  };
  const alloc = Number(st.allocation_usd);
  const pf = stats.profitFactor;

  return (
    <li
      className="grid grid-cols-[minmax(0,1.6fr)_auto_7rem_minmax(0,1.4fr)_auto] items-center gap-3 px-3 py-2.5 hover:bg-zinc-800/30 cursor-pointer"
      onClick={() => onSelect(st.instance_id)}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <StatusDot status={st.status} />
          <span className="font-bold text-zinc-200 truncate">{st.instance_id}</span>
          <span className={`text-2xs font-black tracking-wider ${statusText(st.status)}`}>{st.status}</span>
        </div>
        <div className="mt-0.5 pl-4 text-2xs text-zinc-500 truncate">
          {st.strategy_name} · {st.instrument_id} · {st.timeframe}
        </div>
      </div>

      <div className="flex flex-col items-end">
        <Sparkline values={stats.curve7d} label={`${st.strategy_name} 7-day cumulative realized PnL`} />
        <span className={`text-2xs font-bold tabular-nums ${stats.pnl7d > 0 ? "text-emerald-400" : stats.pnl7d < 0 ? "text-red-400" : "text-zinc-500"}`}>
          7d {fmtSignedUsd(stats.pnl7d)}
        </span>
      </div>

      <dl className="grid grid-cols-2 gap-x-2 text-2xs tabular-nums">
        <dt className="text-zinc-500">Win</dt>
        <dd className={`text-right font-bold ${stats.winRate === null ? "text-zinc-600" : stats.winRate >= 0.5 ? "text-emerald-400" : "text-amber-400"}`}>
          {stats.winRate === null ? "—" : fmtPct(stats.winRate, 0)}
        </dd>
        <dt className="text-zinc-500">PF</dt>
        <dd className={`text-right font-bold ${pf === null ? "text-zinc-600" : pf >= 1 ? "text-emerald-400" : "text-red-400"}`}>
          {pf === null ? "—" : pf === Infinity ? "∞" : pf.toFixed(2)}
        </dd>
        <dt className="text-zinc-500">Trades</dt>
        <dd className="text-right text-zinc-400">{stats.closedCount}</dd>
      </dl>

      <div className="min-w-0">
        <div className="flex justify-between text-2xs mb-1 tabular-nums">
          <span className="text-zinc-400">{fmtCompactUsd(stats.openNotional)} / {fmtCompactUsd(alloc)}</span>
          <span className="text-zinc-500">{equity > 0 ? fmtPct(alloc / equity) : "—"} eq</span>
        </div>
        <AllocationBar used={stats.openNotional} allocated={alloc} />
      </div>

      <div className="flex gap-1 justify-end" onClick={(e) => e.stopPropagation()}>
        {st.status === "RUNNING" ? (
          <button
            onClick={() => command("pause", "PAUSED")}
            className={`${iconBtn} bg-yellow-950 hover:bg-yellow-900 border-yellow-800 text-yellow-300 focus-visible:ring-yellow-500/50`}
            title="Pause"
            aria-label={`Pause strategy ${st.strategy_name}`}
          >
            <Pause className="w-3 h-3" />
          </button>
        ) : (
          <button
            onClick={() => command("resume", "RUNNING")}
            className={`${iconBtn} bg-emerald-950 hover:bg-emerald-900 border-emerald-800 text-emerald-300 focus-visible:ring-emerald-500/50`}
            title="Resume"
            aria-label={`Resume strategy ${st.strategy_name}`}
          >
            <Play className="w-3 h-3" />
          </button>
        )}
        <button
          onClick={() => command("stop", "STOPPED")}
          className={`${iconBtn} bg-red-950 hover:bg-red-900 border-red-800 text-red-300 focus-visible:ring-red-500/50`}
          title="Stop"
          aria-label={`Stop strategy ${st.strategy_name}`}
        >
          <Square className="w-3 h-3" />
        </button>
      </div>
    </li>
  );
});

export function StrategyDashboard({ onSelect }: { onSelect: (instanceId: string) => void }) {
  const strategies = usePortfolioStore((s) => s.strategies);
  const positions = usePortfolioStore((s) => s.positions);
  const equity = usePortfolioStore((s) => s.account?.total_equity ?? 0);
  const loaded = usePortfolioStore((s) => s.loaded);

  const visible = useMemo(() => strategies.filter((st) => !SYSTEM_INSTANCES.has(st.instance_id)), [strategies]);
  const stats = useMemo(() => {
    const now = Date.now();
    return new Map(visible.map((st) => [st.instance_id, strategyStats(positions, st.instance_id, now)]));
  }, [visible, positions]);
  const live = visible.filter((s) => s.status === "RUNNING").length;
  const paused = visible.filter((s) => s.status === "PAUSED").length;

  return (
    <section className="bg-zinc-900/50 border border-zinc-800 rounded flex flex-col min-w-0" aria-labelledby="strategy-instances-title">
      <div className="p-3 border-b border-zinc-800 flex items-center justify-between font-bold text-xs uppercase tracking-wider text-zinc-300">
        <span className="flex items-center gap-2">
          <TrendingUp className="w-4 h-4 text-emerald-400" />
          <span id="strategy-instances-title">Strategy Instances</span>
        </span>
        {visible.length > 0 && (
          <span className="text-2xs font-normal normal-case tracking-normal text-zinc-500">
            {live} live · {paused} paused · {visible.length - live - paused} halted
          </span>
        )}
      </div>
      {!loaded ? (
        <div className="p-3 space-y-2" aria-busy="true" aria-label="Loading strategies">
          {[0, 1].map((i) => <div key={i} className="h-10 rounded bg-zinc-800/40 animate-pulse" />)}
        </div>
      ) : visible.length === 0 ? (
        <div className="text-center py-8 text-zinc-500 italic">No deployed strategy instances.</div>
      ) : (
        <ul className="divide-y divide-zinc-800/50">
          {visible.map((st) => (
            <StrategyRow key={st.instance_id} st={st} stats={stats.get(st.instance_id)!} equity={equity} onSelect={onSelect} />
          ))}
        </ul>
      )}
    </section>
  );
}
