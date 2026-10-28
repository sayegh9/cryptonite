"use client";

import React, { useMemo, useState } from "react";
import {
  AlertTriangle,
  Layers,
  ShieldAlert,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { usePortfolioStore, usePortfolioSync } from "@/lib/stores/usePortfolioStore";
import { closePosition, postCommand } from "@/lib/portfolio/actions";
import {
  fmtPct,
  fmtPrice,
  fmtQty,
  fmtSignedUsd,
  fmtUsd,
  isHeldPosition,
  isPerp,
  liqDistanceOf,
  notionalOf,
} from "@/lib/portfolio/metrics";
import { ActivePositionsTable } from "@/components/portfolio/ActivePositionsTable";
import { ExecutionLedger } from "@/components/portfolio/ExecutionLedger";
import { ExposureBreakdown } from "@/components/portfolio/ExposureBreakdown";
import { DrawdownMeter } from "@/components/portfolio/DrawdownMeter";
import { PositionLifecycleGauge, SideBadge } from "@/components/trades";

function TradesMetricCards() {
  const account = usePortfolioStore((s) => s.account);
  const positions = usePortfolioStore((s) => s.positions);
  const positionsKnown = usePortfolioStore((s) => s.positionsAt !== null);

  const open = useMemo(() => positions.filter(isHeldPosition), [positions]);
  const longs = open.filter((p) => p.side !== "SHORT").length;
  const shorts = open.length - longs;
  const totalUpnl = open.reduce((sum, p) => sum + p.unrealized_pnl, 0);
  const daily = account ? Number(account.daily_realized_pnl) : null;

  const marginUsed = useMemo(() => {
    return open.reduce((sum, p) => {
      const costBasis = Math.abs(p.quantity * p.entry_price);
      const perp = isPerp(p.instrument_id);
      const margin = perp && p.leverage ? costBasis / p.leverage : costBasis;
      return sum + margin;
    }, 0);
  }, [open]);

  const marginRatio =
    account && account.total_equity > 0 ? (marginUsed / account.total_equity) * 100 : 0;

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
      {/* 1. Total Equity */}
      <div className="p-4 bg-zinc-900/80 border border-zinc-800/90 rounded-2xl shadow-xs">
        <div className="text-zinc-400 text-2xs uppercase font-bold tracking-wider">Total Equity</div>
        <div className="text-lg font-black text-white mt-1 tabular-nums">
          {account ? fmtUsd(account.total_equity) : "—"}
        </div>
        <div className="text-2xs text-zinc-400 mt-0.5">Base: {account?.currency || "USDT"}</div>
      </div>

      {/* 2. Unrealized PnL */}
      <div className="p-4 bg-zinc-900/80 border border-zinc-800/90 rounded-2xl shadow-xs">
        <div className="text-zinc-400 text-2xs uppercase font-bold tracking-wider">Unrealized PnL</div>
        <div
          className={`text-lg font-black mt-1 tabular-nums ${
            !positionsKnown ? "text-zinc-500" : totalUpnl >= 0 ? "text-emerald-400" : "text-red-400"
          }`}
        >
          {positionsKnown ? fmtSignedUsd(totalUpnl) : "—"}
        </div>
        <div className="text-2xs text-zinc-400 mt-0.5 tabular-nums">
          {positionsKnown ? (
            <>
              <span className="text-emerald-400">{longs}L</span> /{" "}
              <span className="text-red-400">{shorts}S</span> · {open.length} active
            </>
          ) : (
            "Loading positions…"
          )}
        </div>
      </div>

      {/* 3. Day Realized PnL */}
      <div className="p-4 bg-zinc-900/80 border border-zinc-800/90 rounded-2xl shadow-xs">
        <div className="text-zinc-400 text-2xs uppercase font-bold tracking-wider">Day Realized PnL</div>
        <div
          className={`text-lg font-black mt-1 tabular-nums ${
            daily === null ? "text-zinc-500" : daily >= 0 ? "text-emerald-400" : "text-red-400"
          }`}
        >
          {daily === null ? "—" : fmtSignedUsd(daily)}
        </div>
        <div className="text-2xs text-zinc-400 mt-0.5">UTC Trading Day</div>
      </div>

      {/* 4. Margin Used */}
      <div className="p-4 bg-zinc-900/80 border border-zinc-800/90 rounded-2xl shadow-xs">
        <div className="text-zinc-400 text-2xs uppercase font-bold tracking-wider">Margin Used</div>
        <div className="text-lg font-black text-cyan-400 mt-1 tabular-nums">
          {positionsKnown ? fmtUsd(marginUsed) : "—"}
        </div>
        <div className="text-2xs text-zinc-400 mt-0.5 tabular-nums">
          {account && account.total_equity > 0
            ? `${marginRatio.toFixed(1)}% of Net Equity`
            : "Total Position Margin"}
        </div>
      </div>
    </div>
  );
}

function PositionDrawer({
  positionId,
  onClose,
}: {
  positionId: string | null;
  onClose: () => void;
}) {
  const positions = usePortfolioStore((s) => s.positions);
  const account = usePortfolioStore((s) => s.account);
  const pos = positions.find((p) => p.position_id === positionId) ?? null;

  if (!pos) return null;

  const perp = isPerp(pos.instrument_id);
  const costBasis = Math.abs(pos.quantity * pos.entry_price);
  const margin = perp && pos.leverage ? costBasis / pos.leverage : costBasis;
  const roe = margin > 0 ? pos.unrealized_pnl / margin : 0;

  return (
    <div className="fixed inset-0 bg-black/60 z-40 flex justify-end" onClick={onClose}>
      <div
        role="dialog"
        aria-label="Position Details"
        className="w-96 max-w-full bg-zinc-950 border-l border-zinc-800 h-full flex flex-col shadow-2xl animate-in slide-in-from-right duration-200"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/50">
          <h2 className="font-black text-white flex items-center space-x-2">
            <Layers className="w-5 h-5 text-cyan-400" />
            <span>POSITION DETAILS</span>
          </h2>
          <button
            onClick={onClose}
            aria-label="Close panel"
            className="text-zinc-500 hover:text-white transition-colors cursor-pointer"
          >
            <XCircle className="w-5 h-5" />
          </button>
        </div>

        <div className="p-6 flex-1 overflow-y-auto space-y-6 text-sm">
          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-1">Instrument</div>
            <div className="text-xl font-black text-white">{pos.instrument_id}</div>
            <div className="mt-1 flex items-center gap-2">
              <SideBadge side={pos.side} size="sm" />
              <span className="font-mono text-zinc-300 font-bold">{fmtQty(pos.quantity)} units</span>
              {pos.leverage ? (
                <span className="text-xs text-zinc-400 font-mono">
                  {pos.leverage}x {pos.margin_mode ?? "CROSS"}
                </span>
              ) : null}
            </div>
          </div>

          {/* Pricing & PnL Matrix */}
          <div className="grid grid-cols-2 gap-3">
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Entry Price</div>
              <div className="font-mono font-bold">{fmtPrice(pos.entry_price)}</div>
            </div>
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Current Mark</div>
              <div className="font-mono font-bold text-cyan-400">
                {fmtPrice(pos.current_price || pos.entry_price)}
              </div>
            </div>
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Unrealized PnL</div>
              <div
                className={`font-mono font-bold ${
                  pos.unrealized_pnl >= 0 ? "text-emerald-400" : "text-red-400"
                }`}
              >
                {fmtSignedUsd(pos.unrealized_pnl)}
                <span className="block text-2xs opacity-80">{`${roe >= 0 ? "+" : ""}${(roe * 100).toFixed(2)}% ROE`}</span>
              </div>
            </div>
            <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
              <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Liquidation</div>
              <div className="font-mono font-bold text-amber-300">
                {pos.liquidation_price ? fmtPrice(pos.liquidation_price) : "—"}
              </div>
            </div>
          </div>

          {/* Lifecycle Gauge in Detail Drawer */}
          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">
              Lifecycle & Bracket Targets
            </div>
            <PositionLifecycleGauge
              side={pos.side}
              entryPrice={pos.entry_price}
              markPrice={pos.current_price || pos.entry_price}
              stopLossPrice={pos.stop_loss_price}
              takeProfitPrice={pos.take_profit_price}
              liquidationPrice={pos.liquidation_price}
            />
          </div>

          {/* Originating Strategy */}
          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">
              Strategy Origin
            </div>
            <div className="p-3 bg-indigo-950/30 border border-indigo-900/50 rounded flex items-start space-x-3">
              <div className="p-1.5 bg-indigo-900/50 rounded shrink-0">
                <Layers className="w-4 h-4 text-indigo-400" />
              </div>
              <div>
                <div className="font-bold text-indigo-200">
                  {pos.strategy_name || pos.instance_id}
                </div>
                {pos.strategy_name && (
                  <div className="text-2xs font-mono text-indigo-400/80 mt-0.5 truncate max-w-[220px]">
                    ID: {pos.instance_id}
                  </div>
                )}
                <div className="text-xs text-indigo-400 mt-1">Authorized by Paper Engine Router</div>
              </div>
            </div>
          </div>

          {/* Risk Assessment */}
          <div>
            <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">
              Risk Assessment
            </div>
            <div className="text-xs text-zinc-400 bg-zinc-900 p-3 rounded border border-zinc-800 leading-relaxed">
              Execution verified against {account?.currency || "USDT"} free cash invariant. Capital concentration at{" "}
              {account?.total_equity ? fmtPct(notionalOf(pos) / account.total_equity) : "—"}, against the 25% single-asset cap.
              {liqDistanceOf(pos) !== null && ` Mark is ${fmtPct(liqDistanceOf(pos) as number)} from liquidation.`}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function TradesConsole() {
  usePortfolioSync();

  const positions = usePortfolioStore((s) => s.positions);
  const killSwitch = usePortfolioStore((s) => s.killSwitch);
  const openPositions = useMemo(() => positions.filter(isHeldPosition), [positions]);

  const [selectedPositionId, setSelectedPositionId] = useState<string | null>(null);
  const [showKillModal, setShowKillModal] = useState<boolean>(false);
  const [showFlattenModal, setShowFlattenModal] = useState<boolean>(false);
  const [killReason, setKillReason] = useState<string>("Manual operator trigger");

  const handleTripKillSwitch = async () => {
    setShowKillModal(false);
    await postCommand(
      "/paper/kill-switch",
      { reason: killReason, operator: "web-ui" },
      "Kill switch trip failed"
    );
  };

  const handleResetKillSwitch = () =>
    postCommand("/paper/kill-switch/reset", { operator: "web-ui" }, "Kill switch reset failed");

  const handleFlattenAll = async () => {
    setShowFlattenModal(false);
    await Promise.all(openPositions.map((pos) => closePosition(pos.instance_id, pos.instrument_id)));
  };

  return (
    <div className="flex-1 flex flex-col h-full bg-zinc-950 text-zinc-100 overflow-y-auto p-6 font-mono text-xs">
      {/* Top Banner if Kill Switch is Latched */}
      {killSwitch.is_latched && (
        <div
          className="mb-6 p-4 rounded bg-red-950/80 border border-red-500/80 flex items-center justify-between text-red-200"
          role="alert"
        >
          <div className="flex items-center space-x-3">
            <AlertTriangle className="w-6 h-6 text-red-400 animate-pulse shrink-0" />
            <div>
              <div className="font-bold text-sm text-red-300 tracking-wide">
                EMERGENCY LATCH ENGAGED — EXECUTION HALTED
              </div>
              <div className="text-xs text-red-400">
                Reason: {killSwitch.reason || "Manual engagement"} | Latched by:{" "}
                {killSwitch.latched_by || "operator"}
              </div>
            </div>
          </div>
          <button
            onClick={handleResetKillSwitch}
            className="px-4 py-2 bg-red-800 hover:bg-red-700 text-white font-bold rounded shadow transition-colors cursor-pointer"
          >
            Reset Kill Switch (Audited)
          </button>
        </div>
      )}

      {/* Header with Title & Top Controls */}
      <div className="flex flex-wrap gap-4 items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-black tracking-wider text-white flex items-center space-x-2">
            <span>Trades & Execution Central</span>
            <span className="px-2 py-0.5 rounded text-2xs bg-cyan-950 text-cyan-400 border border-cyan-800 font-bold">
              PAPER ENGINE
            </span>
          </h1>
          <p className="text-zinc-400 text-xs mt-0.5">
            Active positions, real-time risk gauges, resting orders & execution journal
          </p>
        </div>

        <div className="flex items-center space-x-3">
          {!killSwitch.is_latched ? (
            <div
              role="group"
              aria-label="Emergency controls"
              className="flex flex-col items-end gap-1"
            >
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setShowFlattenModal(true)}
                  className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-orange-800 hover:bg-orange-700 text-white font-bold tracking-wide transition-colors disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
                  disabled={openPositions.length === 0}
                  title={
                    openPositions.length === 0
                      ? "Off: there are no open positions to close"
                      : "Close every open position at market"
                  }
                >
                  <XCircle className="w-4 h-4" />
                  <span>Flatten All Positions</span>
                </button>
                <button
                  type="button"
                  onClick={() => setShowKillModal(true)}
                  className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-red-700 hover:bg-red-600 text-white font-bold tracking-wide transition-colors cursor-pointer"
                  title="Halt all paper trading until an operator resets the switch"
                >
                  <ShieldAlert className="w-4 h-4" />
                  <span>Trip Kill Switch</span>
                </button>
              </div>
              <p className="text-right text-2xs text-zinc-500">
                Flatten closes every open position at market
                {openPositions.length === 0 ? " (none open now)" : ""}. Trip halts all trading until reset.
              </p>
            </div>
          ) : (
            <button
              type="button"
              onClick={handleResetKillSwitch}
              className="flex items-center space-x-1.5 px-3.5 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-zinc-950 font-bold tracking-wide transition-colors cursor-pointer"
            >
              <ShieldCheck className="w-4 h-4" />
              <span>Reset Kill Switch</span>
            </button>
          )}
        </div>
      </div>

      {/* MetricCards: Total Equity, uPnL, Day Realized, Margin Used */}
      <TradesMetricCards />

      {/* Active Positions section + Risk Rail (DrawdownMeter & ExposureBreakdown) */}
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-6 mb-6">
        <div className="xl:col-span-8 min-w-0">
          <ActivePositionsTable onSelect={setSelectedPositionId} />
        </div>
        <div className="xl:col-span-4 flex flex-col gap-6 min-w-0">
          <DrawdownMeter />
          <ExposureBreakdown />
        </div>
      </div>

      {/* Unified Order & Execution Journal section */}
      <ExecutionLedger />

      {/* Slide-out Position Detail Drawer */}
      <PositionDrawer
        positionId={selectedPositionId}
        onClose={() => setSelectedPositionId(null)}
      />

      {/* Emergency Flatten All Modal */}
      {showFlattenModal && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50">
          <div
            role="alertdialog"
            aria-label="Confirm emergency flatten"
            className="bg-zinc-900 border border-orange-600 rounded p-6 max-w-md w-full"
          >
            <div className="flex items-center space-x-3 text-orange-400 mb-4">
              <AlertTriangle className="w-6 h-6" />
              <h2 className="text-base font-black">CONFIRM EMERGENCY FLATTEN</h2>
            </div>
            <p className="text-zinc-300 text-xs mb-4">
              This action will aggressively liquidate all {openPositions.length} open position
              {openPositions.length === 1 ? "" : "s"} concurrently via market orders. It does not
              pause running strategies, which may re-enter positions if not halted.
            </p>
            <div className="flex justify-end space-x-3 mt-6">
              <button
                type="button"
                onClick={() => setShowFlattenModal(false)}
                className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded font-bold text-xs cursor-pointer"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleFlattenAll}
                className="px-4 py-1.5 bg-orange-800 hover:bg-orange-700 text-white rounded font-bold text-xs cursor-pointer"
              >
                FLATTEN ALL POSITIONS
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Emergency Kill Switch Modal */}
      {showKillModal && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50">
          <div
            role="alertdialog"
            aria-label="Confirm kill switch"
            className="bg-zinc-900 border border-red-600 rounded p-6 max-w-md w-full"
          >
            <div className="flex items-center space-x-3 text-red-400 mb-4">
              <ShieldAlert className="w-6 h-6" />
              <h2 className="text-base font-black">CONFIRM EMERGENCY KILL SWITCH</h2>
            </div>
            <p className="text-zinc-300 text-xs mb-4">
              Tripping the kill switch immediately latches the execution engine, cancels all
              resting orders, and rejects new entry intents.
            </p>
            <div className="mb-4">
              <label className="block text-zinc-400 text-xs mb-1" htmlFor="kill-reason">
                Reason for Emergency Halt:
              </label>
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
                type="button"
                onClick={() => setShowKillModal(false)}
                className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded font-bold text-xs cursor-pointer"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleTripKillSwitch}
                className="px-4 py-1.5 bg-red-600 hover:bg-red-500 text-white rounded font-bold text-xs cursor-pointer"
              >
                ENGAGE KILL SWITCH
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


export default TradesConsole;
