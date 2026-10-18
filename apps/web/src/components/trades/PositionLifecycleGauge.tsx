"use client";

import React, { useId, useMemo, useState } from "react";
import { AlertTriangle, ArrowDownRight, ArrowUpRight, ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";

export interface PositionLifecycleGaugeProps {
  side: "LONG" | "SHORT" | "FLAT";
  entryPrice: number;
  markPrice: number;
  stopLossPrice?: number | null;
  takeProfitPrice?: number | null;
  trailingStopPrice?: number | null;
  liquidationPrice?: number | null;
  className?: string;
  showLabels?: boolean;
}

/** Formats price with appropriate decimals for crypto / perp trading */
function formatPrice(val: number): string {
  if (!Number.isFinite(val)) return "—";
  const abs = Math.abs(val);
  if (abs >= 1000) {
    return val.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  if (abs >= 1) {
    return val.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  }
  return val.toLocaleString("en-US", { minimumFractionDigits: 4, maximumFractionDigits: 6 });
}

/** Formats dollar delta with +/- prefix */
function formatDeltaUsd(delta: number): string {
  if (!Number.isFinite(delta)) return "—";
  const sign = delta > 0 ? "+" : delta < 0 ? "-" : "";
  return `${sign}$${formatPrice(Math.abs(delta))}`;
}

/** Formats percentage delta with +/- prefix */
function formatPct(pct: number): string {
  if (!Number.isFinite(pct)) return "—";
  const sign = pct > 0 ? "+" : pct < 0 ? "-" : "";
  return `${sign}${Math.abs(pct).toFixed(2)}%`;
}

export function PositionLifecycleGauge({
  side,
  entryPrice,
  markPrice,
  stopLossPrice,
  takeProfitPrice,
  trailingStopPrice,
  liquidationPrice,
  className,
  showLabels = true,
}: PositionLifecycleGaugeProps) {
  const [activeTooltip, setActiveTooltip] = useState<string | null>(null);
  const gaugeId = useId();

  const isLong = side === "LONG";
  const isShort = side === "SHORT";
  const isFlat = side === "FLAT" || entryPrice <= 0;

  // Normalized return relative to entry:
  // For LONG: higher price = profit (> 0), lower price = loss (< 0)
  // For SHORT: lower price = profit (> 0), higher price = loss (< 0)
  const calcPnl = (price: number | null | undefined): number | null => {
    if (price === null || price === undefined || !Number.isFinite(price) || entryPrice <= 0) return null;
    return isShort ? entryPrice - price : price - entryPrice;
  };

  const markPnl = calcPnl(markPrice) ?? 0;
  const markPnlPct = entryPrice > 0 ? (markPnl / entryPrice) * 100 : 0;
  const isInProfit = markPnl > 0;
  const isInLoss = markPnl < 0;

  // Distances to Stop Loss and Take Profit
  const slMetrics = useMemo(() => {
    if (stopLossPrice === null || stopLossPrice === undefined || !Number.isFinite(stopLossPrice)) return null;
    const distanceUsd = Math.abs(markPrice - stopLossPrice);
    const distancePct = markPrice > 0 ? (distanceUsd / markPrice) * 100 : 0;
    const fromEntryUsd = calcPnl(stopLossPrice) ?? 0;
    const fromEntryPct = entryPrice > 0 ? (fromEntryUsd / entryPrice) * 100 : 0;
    return {
      price: stopLossPrice,
      distanceUsd,
      distancePct,
      fromEntryUsd,
      fromEntryPct,
    };
  }, [stopLossPrice, markPrice, entryPrice, isShort]);

  const tpMetrics = useMemo(() => {
    if (takeProfitPrice === null || takeProfitPrice === undefined || !Number.isFinite(takeProfitPrice)) return null;
    const distanceUsd = Math.abs(takeProfitPrice - markPrice);
    const distancePct = markPrice > 0 ? (distanceUsd / markPrice) * 100 : 0;
    const fromEntryUsd = calcPnl(takeProfitPrice) ?? 0;
    const fromEntryPct = entryPrice > 0 ? (fromEntryUsd / entryPrice) * 100 : 0;
    return {
      price: takeProfitPrice,
      distanceUsd,
      distancePct,
      fromEntryUsd,
      fromEntryPct,
    };
  }, [takeProfitPrice, markPrice, entryPrice, isShort]);

  const liqMetrics = useMemo(() => {
    if (liquidationPrice === null || liquidationPrice === undefined || !Number.isFinite(liquidationPrice)) return null;
    const distanceUsd = Math.abs(markPrice - liquidationPrice);
    const distancePct = markPrice > 0 ? (distanceUsd / markPrice) * 100 : 0;
    const isNear = distancePct <= 5; // Warning threshold: within 5% of liquidation
    return {
      price: liquidationPrice,
      distanceUsd,
      distancePct,
      isNear,
    };
  }, [liquidationPrice, markPrice]);

  // Compute visual domain [minPnl, maxPnl] mapped to [0%, 100%]
  // 0% on left (Downside / Hard Stop Loss / Risk)
  // Entry at middle / proportional anchor
  // 100% on right (Upside / Take Profit / Reward)
  const { entryPct, markPct, slPct, tpPct, trailPct, liqPct } = useMemo(() => {
    if (isFlat) {
      return {
        minPnl: -1,
        maxPnl: 1,
        entryPct: 50,
        markPct: 50,
        slPct: null,
        tpPct: null,
        trailPct: null,
        liqPct: null,
      };
    }

    const slPnl = calcPnl(stopLossPrice);
    const tpPnl = calcPnl(takeProfitPrice);
    const trailPnl = calcPnl(trailingStopPrice);
    const liqPnl = calcPnl(liquidationPrice);

    // Negative anchor (Risk / Left side)
    const candidatesLeft: number[] = [-entryPrice * 0.02];
    if (slPnl !== null) candidatesLeft.push(slPnl);
    if (markPnl < 0) candidatesLeft.push(markPnl);
    if (trailPnl !== null && trailPnl < 0) candidatesLeft.push(trailPnl);

    let minBound = Math.min(...candidatesLeft);
    // Pad left bound by 12% so SL marker isn't pinned against the exact edge
    minBound = minBound * 1.12;

    // Positive anchor (Reward / Right side)
    const candidatesRight: number[] = [entryPrice * 0.02];
    if (tpPnl !== null) candidatesRight.push(tpPnl);
    if (markPnl > 0) candidatesRight.push(markPnl);
    if (trailPnl !== null && trailPnl > 0) candidatesRight.push(trailPnl);

    let maxBound = Math.max(...candidatesRight);
    // Pad right bound by 12% so TP marker isn't pinned against the exact edge
    maxBound = maxBound * 1.12;

    const span = Math.max(0.0001, maxBound - minBound);
    const toPct = (val: number | null): number | null => {
      if (val === null) return null;
      const raw = ((val - minBound) / span) * 100;
      return Math.min(96, Math.max(4, raw));
    };

    return {
      minPnl: minBound,
      maxPnl: maxBound,
      entryPct: toPct(0) ?? 40,
      markPct: toPct(markPnl) ?? 50,
      slPct: toPct(slPnl),
      tpPct: toPct(tpPnl),
      trailPct: toPct(trailPnl),
      liqPct: liqPnl !== null ? (liqPnl < minBound ? 3 : toPct(liqPnl)) : null,
    };
  }, [entryPrice, markPrice, stopLossPrice, takeProfitPrice, trailingStopPrice, liquidationPrice, isFlat, markPnl]);

  // Compute fill segment from entry to mark
  const fillLeft = Math.min(entryPct, markPct);
  const fillWidth = Math.max(1, Math.abs(markPct - entryPct));

  if (isFlat) {
    return (
      <div className={cn("rounded-lg border border-zinc-800 bg-zinc-950/70 p-3 text-xs text-zinc-400", className)}>
        <div className="flex items-center justify-between text-2xs text-zinc-500 mb-1.5 font-medium">
          <span>FLAT POSITION</span>
          <span>Mark: ${formatPrice(markPrice)}</span>
        </div>
        <div className="h-2 w-full rounded-full bg-zinc-800/80" />
      </div>
    );
  }

  return (
    <div
      role="group"
      aria-label={`${side} Position Lifecycle: Mark $${formatPrice(markPrice)}, Entry $${formatPrice(entryPrice)}`}
      className={cn(
        "rounded-xl border border-zinc-800/80 bg-zinc-950/90 p-3.5 shadow-md transition-all select-none",
        isInProfit ? "hover:border-emerald-500/30" : "hover:border-rose-500/30",
        className
      )}
    >
      {/* Top Header Row: Side, Mark Price, and Current Return */}
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs font-black tracking-wider uppercase border",
              isLong
                ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/30"
                : "bg-rose-500/15 text-rose-300 border-rose-500/30"
            )}
          >
            {isLong ? <ArrowUpRight className="size-3" /> : <ArrowDownRight className="size-3" />}
            {side}
          </span>
          <span className="text-2xs text-zinc-400 font-medium">
            Entry <span className="font-mono text-zinc-200 tabular-nums">${formatPrice(entryPrice)}</span>
          </span>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-2xs text-zinc-400">Mark:</span>
          <span className="font-mono text-xs font-bold text-zinc-100 tabular-nums">${formatPrice(markPrice)}</span>
          <span
            className={cn(
              "rounded px-1.5 py-0.5 text-2xs font-bold tabular-nums border",
              isInProfit
                ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
                : isInLoss
                ? "bg-rose-500/15 text-rose-400 border-rose-500/30"
                : "bg-zinc-800/60 text-zinc-400 border-zinc-700/50"
            )}
          >
            {formatDeltaUsd(markPnl)} ({formatPct(markPnlPct)})
          </span>
        </div>
      </div>

      {/* Main Lifecycle Gauge Bar */}
      <div className="relative my-4 pt-4 pb-3">
        {/* Track container */}
        <div className="relative h-2.5 w-full rounded-full bg-zinc-900 border border-zinc-800/80 overflow-hidden flex">
          {/* Risk Zone (Left of Entry) */}
          <div
            className="h-full bg-rose-950/20 border-r border-dashed border-zinc-700/60 transition-all"
            style={{ width: `${entryPct}%` }}
            title="Risk Zone (Downside)"
          />
          {/* Reward Zone (Right of Entry) */}
          <div
            className="h-full bg-emerald-950/20 transition-all"
            style={{ width: `${100 - entryPct}%` }}
            title="Reward Zone (Upside)"
          />

          {/* Active PnL Progress Fill */}
          <div
            className={cn(
              "absolute top-0 bottom-0 transition-all duration-300",
              isInProfit
                ? "bg-gradient-to-r from-emerald-500/40 to-emerald-400 shadow-[0_0_12px_rgba(52,211,153,0.35)]"
                : isInLoss
                ? "bg-gradient-to-l from-rose-500/40 to-rose-400 shadow-[0_0_12px_rgba(244,63,94,0.35)]"
                : "bg-zinc-700"
            )}
            style={{
              left: `${fillLeft}%`,
              width: `${fillWidth}%`,
            }}
          />
        </div>

        {/* 1. Hard Stop Loss Marker */}
        {slPct !== null && (
          <div
            className="absolute top-1 -translate-x-1/2 flex flex-col items-center group cursor-pointer z-10"
            style={{ left: `${slPct}%` }}
            onMouseEnter={() => setActiveTooltip("sl")}
            onMouseLeave={() => setActiveTooltip(null)}
            tabIndex={0}
            role="button"
            aria-label={`Stop Loss: $${formatPrice(stopLossPrice!)}`}
          >
            <div className="h-4 w-1 rounded-full bg-rose-500 ring-2 ring-rose-500/30 group-hover:scale-125 transition-transform" />
            <span className="mt-1 text-[10px] font-mono font-bold text-rose-400 tracking-tight">SL</span>

            {/* Hover Tooltip */}
            {(activeTooltip === "sl" || activeTooltip === `${gaugeId}-sl`) && slMetrics && (
              <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 w-48 rounded-lg border border-rose-500/30 bg-zinc-900/95 p-2 text-2xs shadow-xl backdrop-blur-xs z-30 pointer-events-none">
                <div className="flex items-center justify-between font-bold text-rose-300 border-b border-zinc-800 pb-1 mb-1">
                  <span>Hard Stop Loss</span>
                  <span className="font-mono tabular-nums">${formatPrice(slMetrics.price)}</span>
                </div>
                <div className="flex justify-between text-zinc-400">
                  <span>From Mark:</span>
                  <span className="font-mono text-zinc-200 tabular-nums">
                    ${formatPrice(slMetrics.distanceUsd)} ({slMetrics.distancePct.toFixed(2)}%)
                  </span>
                </div>
                <div className="flex justify-between text-zinc-400 mt-0.5">
                  <span>Max Risk:</span>
                  <span className="font-mono text-rose-400 tabular-nums">
                    {formatDeltaUsd(slMetrics.fromEntryUsd)} ({formatPct(slMetrics.fromEntryPct)})
                  </span>
                </div>
              </div>
            )}
          </div>
        )}

        {/* 2. Trailing Stop Marker (if defined) */}
        {trailPct !== null && (
          <div
            className="absolute top-1 -translate-x-1/2 flex flex-col items-center group cursor-pointer z-10"
            style={{ left: `${trailPct}%` }}
            onMouseEnter={() => setActiveTooltip("trail")}
            onMouseLeave={() => setActiveTooltip(null)}
            tabIndex={0}
            role="button"
            aria-label={`Trailing Stop: $${formatPrice(trailingStopPrice!)}`}
          >
            <div className="h-4 w-1 rounded-full bg-amber-400 ring-2 ring-amber-400/30 group-hover:scale-125 transition-transform" />
            <span className="mt-1 text-[10px] font-mono font-bold text-amber-400 tracking-tight">TRAIL</span>

            {/* Tooltip */}
            {activeTooltip === "trail" && (
              <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 w-44 rounded-lg border border-amber-500/30 bg-zinc-900/95 p-2 text-2xs shadow-xl backdrop-blur-xs z-30 pointer-events-none">
                <div className="flex items-center justify-between font-bold text-amber-300 border-b border-zinc-800 pb-1 mb-1">
                  <span>Trailing Stop</span>
                  <span className="font-mono tabular-nums">${formatPrice(trailingStopPrice!)}</span>
                </div>
                <div className="flex justify-between text-zinc-400">
                  <span>Distance to Mark:</span>
                  <span className="font-mono text-zinc-200 tabular-nums">
                    ${formatPrice(Math.abs(markPrice - trailingStopPrice!))}
                  </span>
                </div>
              </div>
            )}
          </div>
        )}

        {/* 3. Entry (0%) Anchor */}
        <div
          className="absolute top-1 -translate-x-1/2 flex flex-col items-center z-10"
          style={{ left: `${entryPct}%` }}
          title={`Entry: $${formatPrice(entryPrice)} (0.00%)`}
        >
          <div className="h-4 w-1 rounded-full bg-zinc-400 ring-2 ring-zinc-700/60" />
          <span className="mt-1 text-[10px] font-mono font-semibold text-zinc-400 tracking-tight">ENTRY</span>
        </div>

        {/* 4. Take Profit Marker */}
        {tpPct !== null && (
          <div
            className="absolute top-1 -translate-x-1/2 flex flex-col items-center group cursor-pointer z-10"
            style={{ left: `${tpPct}%` }}
            onMouseEnter={() => setActiveTooltip("tp")}
            onMouseLeave={() => setActiveTooltip(null)}
            tabIndex={0}
            role="button"
            aria-label={`Take Profit: $${formatPrice(takeProfitPrice!)}`}
          >
            <div className="h-4 w-1 rounded-full bg-emerald-400 ring-2 ring-emerald-500/30 group-hover:scale-125 transition-transform" />
            <span className="mt-1 text-[10px] font-mono font-bold text-emerald-400 tracking-tight">TP</span>

            {/* Hover Tooltip */}
            {(activeTooltip === "tp" || activeTooltip === `${gaugeId}-tp`) && tpMetrics && (
              <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 w-48 rounded-lg border border-emerald-500/30 bg-zinc-900/95 p-2 text-2xs shadow-xl backdrop-blur-xs z-30 pointer-events-none">
                <div className="flex items-center justify-between font-bold text-emerald-300 border-b border-zinc-800 pb-1 mb-1">
                  <span>Take Profit</span>
                  <span className="font-mono tabular-nums">${formatPrice(tpMetrics.price)}</span>
                </div>
                <div className="flex justify-between text-zinc-400">
                  <span>From Mark:</span>
                  <span className="font-mono text-zinc-200 tabular-nums">
                    ${formatPrice(tpMetrics.distanceUsd)} ({tpMetrics.distancePct.toFixed(2)}%)
                  </span>
                </div>
                <div className="flex justify-between text-zinc-400 mt-0.5">
                  <span>Target Reward:</span>
                  <span className="font-mono text-emerald-400 tabular-nums">
                    {formatDeltaUsd(tpMetrics.fromEntryUsd)} ({formatPct(tpMetrics.fromEntryPct)})
                  </span>
                </div>
              </div>
            )}
          </div>
        )}

        {/* 5. Liquidation Warning Pip */}
        {liqPct !== null && liqMetrics && (
          <div
            className="absolute top-0 -translate-x-1/2 flex flex-col items-center group cursor-pointer z-20"
            style={{ left: `${liqPct}%` }}
            onMouseEnter={() => setActiveTooltip("liq")}
            onMouseLeave={() => setActiveTooltip(null)}
            tabIndex={0}
            role="button"
            aria-label={`Liquidation Price: $${formatPrice(liqMetrics.price)}`}
          >
            <div
              className={cn(
                "flex items-center justify-center size-4 rounded-full border shadow-sm transition-transform group-hover:scale-125",
                liqMetrics.isNear
                  ? "bg-red-600 text-white border-red-400 motion-safe:animate-pulse ring-2 ring-red-500/50"
                  : "bg-red-950 text-red-400 border-red-700/80"
              )}
            >
              <AlertTriangle className="size-2.5" />
            </div>
            <span className="mt-1 text-[9px] font-mono font-black text-red-400 tracking-tighter">LIQ</span>

            {/* Hover Tooltip */}
            {(activeTooltip === "liq" || activeTooltip === `${gaugeId}-liq`) && (
              <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 w-52 rounded-lg border border-red-600/40 bg-zinc-900/95 p-2 text-2xs shadow-2xl backdrop-blur-xs z-40 pointer-events-none">
                <div className="flex items-center gap-1 font-bold text-red-300 border-b border-zinc-800 pb-1 mb-1">
                  <ShieldAlert className="size-3.5 text-red-400 shrink-0" />
                  <span>Liquidation Warning</span>
                </div>
                <div className="flex justify-between text-zinc-300 mb-0.5 font-mono">
                  <span>Liq Price:</span>
                  <span className="font-bold text-red-400">${formatPrice(liqMetrics.price)}</span>
                </div>
                <div className="flex justify-between text-zinc-400">
                  <span>Buffer to Liq:</span>
                  <span className="font-mono text-zinc-200 tabular-nums">
                    ${formatPrice(liqMetrics.distanceUsd)} ({liqMetrics.distancePct.toFixed(2)}%)
                  </span>
                </div>
                {liqMetrics.isNear && (
                  <div className="mt-1.5 rounded bg-red-950/80 border border-red-700/60 p-1 text-[10px] text-red-200 font-medium text-center">
                    CRITICAL: &lt;5% margin buffer remaining
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* 6. Current Mark Price: Glowing Pulse Pin */}
        <div
          className="absolute top-0 -translate-x-1/2 flex flex-col items-center group cursor-pointer z-20 pointer-events-auto"
          style={{ left: `${markPct}%` }}
          onMouseEnter={() => setActiveTooltip("mark")}
          onMouseLeave={() => setActiveTooltip(null)}
          tabIndex={0}
          role="button"
          aria-label={`Current Mark Price: $${formatPrice(markPrice)}`}
        >
          {/* Glowing pulse ring */}
          <div className="relative flex items-center justify-center size-5">
            <span
              className={cn(
                "absolute size-full rounded-full opacity-75 motion-safe:animate-ping",
                isInProfit ? "bg-emerald-400" : isInLoss ? "bg-rose-400" : "bg-zinc-400"
              )}
            />
            {/* Center bead */}
            <span
              className={cn(
                "relative inline-flex size-3.5 rounded-full border-2 border-zinc-950 shadow-md transition-transform group-hover:scale-125",
                isInProfit
                  ? "bg-emerald-400 ring-2 ring-emerald-400/50"
                  : isInLoss
                  ? "bg-rose-400 ring-2 ring-rose-400/50"
                  : "bg-zinc-200 ring-2 ring-zinc-500/50"
              )}
            />
          </div>

          <span
            className={cn(
              "mt-0.5 text-[10px] font-mono font-bold tracking-tight rounded px-1 py-0.2",
              isInProfit
                ? "text-emerald-300 bg-emerald-950/80 border border-emerald-500/30"
                : isInLoss
                ? "text-rose-300 bg-rose-950/80 border border-rose-500/30"
                : "text-zinc-300 bg-zinc-900 border border-zinc-700"
            )}
          >
            MARK
          </span>

          {/* Mark Tooltip */}
          {activeTooltip === "mark" && (
            <div className="absolute bottom-full mb-2 left-1/2 -translate-x-1/2 w-48 rounded-lg border border-zinc-700 bg-zinc-900/95 p-2 text-2xs shadow-xl backdrop-blur-xs z-30 pointer-events-none">
              <div className="flex items-center justify-between font-bold text-zinc-100 border-b border-zinc-800 pb-1 mb-1">
                <span>Current Mark Price</span>
                <span className="font-mono tabular-nums">${formatPrice(markPrice)}</span>
              </div>
              <div className="flex justify-between text-zinc-400">
                <span>Unrealized Return:</span>
                <span
                  className={cn(
                    "font-mono font-bold tabular-nums",
                    isInProfit ? "text-emerald-400" : isInLoss ? "text-rose-400" : "text-zinc-300"
                  )}
                >
                  {formatDeltaUsd(markPnl)} ({formatPct(markPnlPct)})
                </span>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Bottom Summary Bar: Distances to Stop Loss and Take Profit */}
      {showLabels && (
        <div className="mt-3 pt-2.5 border-t border-zinc-900 flex flex-wrap items-center justify-between gap-2 text-2xs text-zinc-400">
          {/* SL Distance Callout */}
          <div className="flex items-center gap-1.5 font-medium">
            <span className="size-1.5 rounded-full bg-rose-500" />
            <span className="text-zinc-500">To Stop Loss:</span>
            {slMetrics ? (
              <span className="font-mono text-zinc-300 tabular-nums">
                ${formatPrice(slMetrics.distanceUsd)}{" "}
                <span className="text-rose-400 font-semibold">({slMetrics.distancePct.toFixed(2)}%)</span>
              </span>
            ) : (
              <span className="text-zinc-600">None set</span>
            )}
          </div>

          {/* TP Distance Callout */}
          <div className="flex items-center gap-1.5 font-medium">
            <span className="size-1.5 rounded-full bg-emerald-400" />
            <span className="text-zinc-500">To Take Profit:</span>
            {tpMetrics ? (
              <span className="font-mono text-zinc-300 tabular-nums">
                ${formatPrice(tpMetrics.distanceUsd)}{" "}
                <span className="text-emerald-400 font-semibold">({tpMetrics.distancePct.toFixed(2)}%)</span>
              </span>
            ) : (
              <span className="text-zinc-600">None set</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
