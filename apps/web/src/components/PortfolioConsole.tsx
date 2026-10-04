"use client";

import React, { useState } from "react";
import {
  ShieldAlert,
  ShieldCheck,
  AlertTriangle,
  XCircle,
  Plus,
  TrendingUp,
  Layers,
  ArrowUpRight,
} from "lucide-react";
import { usePortfolioStore, usePortfolioSync } from "@/lib/stores/usePortfolioStore";
import { closePosition, postCommand } from "@/lib/portfolio/actions";
import { fmtPct, fmtPrice, fmtQty, fmtSignedUsd, fmtUsd, liqDistanceOf, notionalOf } from "@/lib/portfolio/metrics";
import { ActivePositionsTable } from "@/components/portfolio/ActivePositionsTable";
import { StrategyDashboard } from "@/components/portfolio/StrategyDashboard";
import { ExecutionLedger } from "@/components/portfolio/ExecutionLedger";
import { ExposureBreakdown } from "@/components/portfolio/ExposureBreakdown";
import { DrawdownMeter } from "@/components/portfolio/DrawdownMeter";

function MetricCards() {
  const account = usePortfolioStore((s) => s.account);
  const positions = usePortfolioStore((s) => s.positions);
  const latched = usePortfolioStore((s) => s.killSwitch.is_latched);
  const positionsKnown = usePortfolioStore((s) => s.positionsAt !== null);
  const killSwitchKnown = usePortfolioStore((s) => s.killSwitchAt !== null);
  const open = positions.filter((p) => p.status === "OPEN");
  const longs = open.filter((p) => p.side !== "SHORT").length;
  const daily = account ? Number(account.daily_realized_pnl) : null;
  const upnl = open.reduce((a, p) => a + p.unrealized_pnl, 0);

  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
      <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
        <div className="text-zinc-500 text-2xs uppercase font-bold tracking-wider">Total Net Equity</div>
        <div className="text-lg font-black text-white mt-1 tabular-nums">{account ? fmtUsd(account.total_equity) : "—"}</div>
        <div className="text-2xs text-zinc-400 mt-0.5">Base: {account?.currency || "USDT"}</div>
      </div>

      <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
        <div className="text-zinc-500 text-2xs uppercase font-bold tracking-wider">Available Cash</div>
        <div className="text-lg font-black text-cyan-400 mt-1 tabular-nums">{account ? fmtUsd(account.cash_balance) : "—"}</div>
        <div className="text-2xs text-zinc-400 mt-0.5">Spot Cash Invariant</div>
      </div>

      <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
        <div className="text-zinc-500 text-2xs uppercase font-bold tracking-wider">Daily Realized PnL</div>
        <div className={`text-lg font-black mt-1 tabular-nums ${daily === null ? "text-zinc-500" : daily >= 0 ? "text-emerald-400" : "text-red-400"}`}>{daily === null ? "—" : fmtSignedUsd(daily)}</div>
        <div className="text-2xs text-zinc-400 mt-0.5">UTC Trading Day</div>
      </div>

      <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
        <div className="text-zinc-500 text-2xs uppercase font-bold tracking-wider">Open Positions</div>
        <div className="text-lg font-black text-zinc-200 mt-1">{positionsKnown ? `${open.length} Active` : "—"}</div>
        <div className="text-2xs text-zinc-400 mt-0.5 tabular-nums">
          {positionsKnown ? (
            <>
              <span className="text-emerald-400">{longs}L</span> / <span className="text-red-400">{open.length - longs}S</span>
              <span className={upnl >= 0 ? "text-emerald-400/80" : "text-red-400/80"}> · {fmtSignedUsd(upnl)} unrealized</span>
            </>
          ) : (
            "positions unavailable"
          )}
        </div>
      </div>

      <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
        <div className="text-zinc-500 text-2xs uppercase font-bold tracking-wider">Execution Guard</div>
        <div className="text-lg font-black mt-1 flex items-center space-x-1.5">
          {!killSwitchKnown ? (
            <span className="text-zinc-400">UNKNOWN</span>
          ) : latched ? (
            <span className="text-red-400 flex items-center space-x-1">
              <ShieldAlert className="w-4 h-4" />
              <span>LATCHED</span>
            </span>
          ) : (
            <span className="text-emerald-400 flex items-center space-x-1">
              <ShieldCheck className="w-4 h-4" />
              <span>ACTIVE</span>
            </span>
          )}
        </div>
        <div className="text-2xs text-zinc-400 mt-0.5">
          {!account ? "Account status unavailable" : account.daily_loss_latched ? "Daily Drawdown Tripped" : "Daily loss latch not tripped"}
        </div>
      </div>
    </div>
  );
}

function Drawer({ title, icon, onClose, children }: { title: string; icon: React.ReactNode; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 bg-black/60 z-40 flex justify-end" onClick={onClose}>
      <div
        role="dialog"
        aria-label={title}
        className="w-96 max-w-full bg-zinc-950 border-l border-zinc-800 h-full flex flex-col shadow-2xl animate-in slide-in-from-right duration-200"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/50">
          <h2 className="font-black text-white flex items-center space-x-2">
            {icon}
            <span>{title}</span>
          </h2>
          <button onClick={onClose} aria-label="Close panel" className="text-zinc-500 hover:text-white transition-colors">
            <XCircle className="w-5 h-5" />
          </button>
        </div>
        <div className="p-6 flex-1 overflow-y-auto space-y-6 text-sm">{children}</div>
      </div>
    </div>
  );
}

export function PortfolioConsole() {
  usePortfolioSync();

  const account = usePortfolioStore((s) => s.account);
  const positions = usePortfolioStore((s) => s.positions);
  const strategies = usePortfolioStore((s) => s.strategies);
  const killSwitch = usePortfolioStore((s) => s.killSwitch);
  const availableStrategies = usePortfolioStore((s) => s.availableStrategies);

  // Selections hold ids so the drawers keep showing live rows
  const [selectedPositionId, setSelectedPositionId] = useState<string | null>(null);
  const [selectedStrategyId, setSelectedStrategyId] = useState<string | null>(null);
  const selectedPosition = positions.find((p) => p.position_id === selectedPositionId) ?? null;
  const selectedStrategy = strategies.find((s) => s.instance_id === selectedStrategyId) ?? null;

  const [showDeployModal, setShowDeployModal] = useState<boolean>(false);
  const [showKillModal, setShowKillModal] = useState<boolean>(false);
  const [showFlattenModal, setShowFlattenModal] = useState<boolean>(false);
  const [killReason, setKillReason] = useState<string>("Manual operator trigger");

  // Strategy Deploy Form State ("" = first available strategy)
  const [deployStrategyName, setDeployStrategyName] = useState<string>("");
  const [deployInstrument, setDeployInstrument] = useState<string>("BTCUSDT.BINANCE");
  const [deployTimeframe, setDeployTimeframe] = useState<string>("1m");
  const [deployAllocation, setDeployAllocation] = useState<number>(5000);
  const deploySlug = deployStrategyName || availableStrategies[0]?.slug || "";

  const openPositions = positions.filter((p) => p.status === "OPEN");

  const handleTripKillSwitch = async () => {
    setShowKillModal(false);
    await postCommand("/paper/kill-switch", { reason: killReason, operator: "web-ui" }, "Kill switch trip failed");
  };

  const handleResetKillSwitch = () => postCommand("/paper/kill-switch/reset", { operator: "web-ui" }, "Kill switch reset failed");

  const handleFlattenAll = async () => {
    setShowFlattenModal(false);
    await Promise.all(openPositions.map((pos) => closePosition(pos.instance_id, pos.instrument_id)));
  };

  const handleDeploy = async (e: React.FormEvent) => {
    e.preventDefault();
    const strat = availableStrategies.find((s) => s.slug === deploySlug);
    if (!strat) return;
    setShowDeployModal(false);
    await postCommand("/paper/strategies/deploy", {
      strategy_name: strat.slug,
      strategy_version_id: strat.active_version_id,
      instrument_id: deployInstrument,
      timeframe: deployTimeframe,
      allocation_usd: deployAllocation,
    }, "Deploy failed");
  };

  return (
    <div className="flex-1 flex flex-col h-full bg-zinc-950 text-zinc-100 overflow-y-auto p-6 font-mono text-xs">
      {/* Top Banner if Kill Switch is Latched */}
      {killSwitch.is_latched && (
        <div className="mb-6 p-4 rounded bg-red-950/80 border border-red-500/80 flex items-center justify-between text-red-200" role="alert">
          <div className="flex items-center space-x-3">
            <AlertTriangle className="w-6 h-6 text-red-400 animate-pulse" />
            <div>
              <div className="font-bold text-sm text-red-300 tracking-wide">
                EMERGENCY LATCH ENGAGED — EXECUTION HALTED
              </div>
              <div className="text-xs text-red-400">
                Reason: {killSwitch.reason || "Manual engagement"} | Latched by: {killSwitch.latched_by || "operator"}
              </div>
            </div>
          </div>
          <button
            onClick={handleResetKillSwitch}
            className="px-4 py-2 bg-red-800 hover:bg-red-700 text-white font-bold rounded shadow transition-colors"
          >
            Reset Kill Switch (Audited)
          </button>
        </div>
      )}

      {/* Header with Title & Top Actions */}
      <div className="flex flex-wrap gap-4 items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-black tracking-wider text-white flex items-center space-x-2">
            <span>PORTFOLIO & PAPER EXECUTION CONSOLE</span>
            <span className="px-2 py-0.5 rounded text-2xs bg-cyan-950 text-cyan-400 border border-cyan-800">
              M4 SIMULATOR
            </span>
          </h1>
          <p className="text-zinc-400 text-xs mt-0.5">
            Real-time paper trading engine backed by Nautilus 2.0.0rc4 LiveNode and authoritative risk guardrails.
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={() => setShowDeployModal(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold tracking-wide transition-colors"
          >
            <Plus className="w-4 h-4" />
            <span>Deploy Strategy</span>
          </button>

          {!killSwitch.is_latched ? (
            <>
              <button
                onClick={() => setShowFlattenModal(true)}
                className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-orange-600 hover:bg-orange-500 text-white font-bold tracking-wide transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                disabled={openPositions.length === 0}
              >
                <XCircle className="w-4 h-4" />
                <span>Emergency Flatten All</span>
              </button>
              <button
                onClick={() => setShowKillModal(true)}
                className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-red-700 hover:bg-red-600 text-white font-bold tracking-wide transition-colors"
              >
                <ShieldAlert className="w-4 h-4" />
                <span>Trip Kill Switch</span>
              </button>
            </>
          ) : (
            <button
              onClick={handleResetKillSwitch}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-zinc-950 font-bold tracking-wide transition-colors"
            >
              <ShieldCheck className="w-4 h-4" />
              <span>Reset Switch</span>
            </button>
          )}
        </div>
      </div>

      <MetricCards />

      {/* Positions + risk rail */}
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-6 mb-6">
        <div className="xl:col-span-8 min-w-0">
          <ActivePositionsTable onSelect={setSelectedPositionId} />
        </div>
        <div className="xl:col-span-4 flex flex-col gap-6 min-w-0">
          <DrawdownMeter />
          <ExposureBreakdown />
        </div>
      </div>

      <div className="mb-6">
        <StrategyDashboard onSelect={setSelectedStrategyId} />
      </div>

      <ExecutionLedger />

      {/* Emergency Flatten All Modal */}
      {showFlattenModal && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50">
          <div role="alertdialog" aria-label="Confirm emergency flatten" className="bg-zinc-900 border border-orange-600 rounded p-6 max-w-md w-full">
            <div className="flex items-center space-x-3 text-orange-400 mb-4">
              <AlertTriangle className="w-6 h-6" />
              <h2 className="text-base font-black">CONFIRM EMERGENCY FLATTEN</h2>
            </div>
            <p className="text-zinc-300 text-xs mb-4">
              This action will aggressively liquidate all open positions concurrently via market orders. It does not pause running strategies, which may re-enter positions if not halted.
            </p>
            <div className="flex justify-end space-x-3 mt-6">
              <button
                onClick={() => setShowFlattenModal(false)}
                className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded font-bold text-xs"
              >
                Cancel
              </button>
              <button
                onClick={handleFlattenAll}
                className="px-4 py-1.5 bg-orange-600 hover:bg-orange-500 text-white rounded font-bold text-xs"
              >
                FLATTEN ALL POSITIONS
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Kill Switch Modal */}
      {showKillModal && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50">
          <div role="alertdialog" aria-label="Confirm kill switch" className="bg-zinc-900 border border-red-600 rounded p-6 max-w-md w-full">
            <div className="flex items-center space-x-3 text-red-400 mb-4">
              <ShieldAlert className="w-6 h-6" />
              <h2 className="text-base font-black">CONFIRM EMERGENCY KILL SWITCH</h2>
            </div>
            <p className="text-zinc-300 text-xs mb-4">
              Tripping the kill switch immediately latches the execution engine, cancels all resting orders, and rejects new entry intents.
            </p>
            <div className="mb-4">
              <label className="block text-zinc-400 text-xs mb-1" htmlFor="kill-reason">Reason for Emergency Halt:</label>
              <input
                id="kill-reason"
                type="text"
                value={killReason}
                onChange={(e) => setKillReason(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none focus:border-red-500"
              />
            </div>
            <div className="flex justify-end space-x-3">
              <button
                onClick={() => setShowKillModal(false)}
                className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded font-bold text-xs"
              >
                Cancel
              </button>
              <button
                onClick={handleTripKillSwitch}
                className="px-4 py-1.5 bg-red-600 hover:bg-red-500 text-white rounded font-bold text-xs"
              >
                ENGAGE KILL SWITCH
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Deploy Strategy Modal */}
      {showDeployModal && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50">
          <div role="dialog" aria-label="Deploy strategy" className="bg-zinc-900 border border-zinc-700 rounded p-6 max-w-md w-full">
            <h2 className="text-base font-black text-white mb-4 flex items-center space-x-2">
              <Plus className="w-5 h-5 text-cyan-400" />
              <span>Deploy Strategy to Paper Engine</span>
            </h2>
            <form onSubmit={handleDeploy} className="space-y-4">
              <div>
                <label className="block text-zinc-400 text-xs mb-1" htmlFor="deploy-strategy">Strategy Name:</label>
                <select
                  id="deploy-strategy"
                  value={deploySlug}
                  onChange={(e) => setDeployStrategyName(e.target.value)}
                  className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none"
                >
                  {availableStrategies.map((s) => (
                    <option key={s.slug} value={s.slug}>{s.name}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-zinc-400 text-xs mb-1" htmlFor="deploy-instrument">Instrument:</label>
                <select
                  id="deploy-instrument"
                  value={deployInstrument}
                  onChange={(e) => setDeployInstrument(e.target.value)}
                  className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none"
                >
                  <option value="BTCUSDT.BINANCE">BTCUSDT.BINANCE</option>
                  <option value="ETHUSDT.BINANCE">ETHUSDT.BINANCE</option>
                  <option value="SOLUSDT.BINANCE">SOLUSDT.BINANCE</option>
                </select>
              </div>

              <div>
                <label className="block text-zinc-400 text-xs mb-1" htmlFor="deploy-timeframe">Timeframe:</label>
                <select
                  id="deploy-timeframe"
                  value={deployTimeframe}
                  onChange={(e) => setDeployTimeframe(e.target.value)}
                  className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none"
                >
                  <option value="1m">1 Minute</option>
                  <option value="5m">5 Minutes</option>
                  <option value="15m">15 Minutes</option>
                  <option value="1h">1 Hour</option>
                </select>
              </div>

              <div>
                <label className="block text-zinc-400 text-xs mb-1" htmlFor="deploy-allocation">USD Capital Allocation:</label>
                <input
                  id="deploy-allocation"
                  type="number"
                  value={deployAllocation}
                  onChange={(e) => setDeployAllocation(Number(e.target.value))}
                  min={100}
                  max={50000}
                  step={100}
                  className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none"
                />
              </div>

              <div className="flex justify-end space-x-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowDeployModal(false)}
                  className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded font-bold text-xs"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-4 py-1.5 bg-cyan-600 hover:bg-cyan-500 text-zinc-950 rounded font-bold text-xs"
                >
                  Deploy
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Attribution Drawer */}
      {selectedPosition && (
        <Drawer title="POSITION ATTRIBUTION" icon={<ArrowUpRight className="w-5 h-5 text-cyan-400" />} onClose={() => setSelectedPositionId(null)}>
          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Instrument</div>
            <div className="text-xl font-black text-white">{selectedPosition.instrument_id}</div>
            <div className={`mt-1 font-bold ${selectedPosition.side === "SHORT" ? "text-red-400" : "text-emerald-400"}`}>
              {selectedPosition.side} • {fmtQty(selectedPosition.quantity)}
              {selectedPosition.leverage ? ` • ${selectedPosition.leverage}x ${selectedPosition.margin_mode ?? "CROSS"}` : ""}
            </div>
            {selectedPosition.status !== "OPEN" && <div className="mt-1 text-2xs text-zinc-500 uppercase">Closed</div>}
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Entry Price</div>
              <div className="font-mono font-bold">{fmtPrice(selectedPosition.entry_price)}</div>
            </div>
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Mark Price</div>
              <div className="font-mono font-bold text-cyan-400">{fmtPrice(selectedPosition.current_price || selectedPosition.entry_price)}</div>
            </div>
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Unrealized</div>
              <div className={`font-mono font-bold ${selectedPosition.unrealized_pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>{fmtSignedUsd(selectedPosition.unrealized_pnl)}</div>
            </div>
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Liquidation</div>
              <div className="font-mono font-bold text-amber-300">
                {selectedPosition.liquidation_price ? fmtPrice(selectedPosition.liquidation_price) : "—"}
              </div>
            </div>
          </div>

          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Originating Intent</div>
            <div className="p-3 bg-indigo-950/30 border border-indigo-900/50 rounded flex items-start space-x-3">
              <div className="p-1.5 bg-indigo-900/50 rounded">
                <Layers className="w-4 h-4 text-indigo-400" />
              </div>
              <div>
                <div className="font-bold text-indigo-200">{selectedPosition.instance_id}</div>
                <div className="text-xs text-indigo-400 mt-1">Authorized by Paper Engine Router</div>
              </div>
            </div>
          </div>

          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Risk Assessment</div>
            <div className="text-xs text-zinc-400 bg-zinc-900 p-3 rounded border border-zinc-800 leading-relaxed">
              Execution verified against {account?.currency || "USDT"} free cash invariant. Capital concentration at{" "}
              {account?.total_equity ? fmtPct(notionalOf(selectedPosition) / account.total_equity) : "—"}, against the 25% single-asset cap.
              {liqDistanceOf(selectedPosition) !== null && ` Mark is ${fmtPct(liqDistanceOf(selectedPosition) as number)} from liquidation.`}
            </div>
          </div>
        </Drawer>
      )}

      {/* Strategy Drawer */}
      {selectedStrategy && (
        <Drawer title="STRATEGY DETAILS" icon={<TrendingUp className="w-5 h-5 text-emerald-400" />} onClose={() => setSelectedStrategyId(null)}>
          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Configuration</div>
            <div className="text-xl font-black text-white">{selectedStrategy.strategy_name}</div>
            <div className="mt-1 font-bold text-zinc-300">
              {selectedStrategy.instrument_id} • {selectedStrategy.timeframe}
            </div>
            <div className="mt-2 text-xs text-zinc-400 font-mono bg-zinc-900 p-2 rounded border border-zinc-800">
              {selectedStrategy.instance_id}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Status</div>
              <div className={`font-mono font-bold ${selectedStrategy.status === "RUNNING" ? "text-emerald-400" : "text-yellow-400"}`}>
                {selectedStrategy.status}
              </div>
            </div>
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Allocation</div>
              <div className="font-mono font-bold text-cyan-400">{fmtUsd(Number(selectedStrategy.allocation_usd))}</div>
            </div>
          </div>

          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Resolved Parameters</div>
            <div className="bg-zinc-900 rounded border border-zinc-800 p-3 overflow-x-auto">
              {selectedStrategy.resolved_parameters ? (
                <pre className="text-2xs text-zinc-400 font-mono">
                  {JSON.stringify(
                    typeof selectedStrategy.resolved_parameters === "string"
                      ? JSON.parse(selectedStrategy.resolved_parameters)
                      : selectedStrategy.resolved_parameters,
                    null,
                    2
                  )}
                </pre>
              ) : (
                <div className="text-xs text-zinc-500 italic">No parameters available or using defaults.</div>
              )}
            </div>
          </div>

          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Operator Actions</div>
            <div className="space-y-2">
              <button
                onClick={() => {
                  window.location.href = `/operator?strategy=${selectedStrategy.instance_id}`;
                }}
                className="w-full flex items-center justify-center space-x-2 p-2 bg-indigo-900/30 hover:bg-indigo-900/50 border border-indigo-500/30 text-indigo-300 rounded transition-colors text-xs font-bold"
              >
                <ArrowUpRight className="w-4 h-4" />
                <span>Open Strategy Dashboard</span>
              </button>
              <button
                onClick={() => {
                  if (window.confirm("Are you sure you want to halt this strategy and liquidate its holdings?")) {
                    usePortfolioStore.getState().setStrategyStatusOptimistic(selectedStrategy.instance_id, "STOPPED");
                    void postCommand(`/paper/strategies/${selectedStrategy.instance_id}/stop`, undefined, "Stop failed");
                    void closePosition(selectedStrategy.instance_id, selectedStrategy.instrument_id);
                    setSelectedStrategyId(null);
                  }
                }}
                className="w-full flex items-center justify-center space-x-2 p-2 bg-red-900/30 hover:bg-red-900/50 border border-red-500/30 text-red-300 rounded transition-colors text-xs font-bold"
              >
                <ShieldAlert className="w-4 h-4" />
                <span>Halt & Flatten Strategy</span>
              </button>
            </div>
          </div>
        </Drawer>
      )}
    </div>
  );
}
