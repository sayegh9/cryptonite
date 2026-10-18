"use client";

import React from "react";
import { Clock } from "lucide-react";
import { cn } from "@/lib/utils";

export type StrategyStatus = "RUNNING" | "PAUSED" | "STOPPED" | "IDLE" | string;
export type StrategyBadgeSize = "sm" | "md" | "lg";

export interface StrategyIdentityBadgeProps {
  name: string;
  category?: string;
  status: StrategyStatus;
  timeframe?: string;
  instrument?: string;
  size?: StrategyBadgeSize;
  className?: string;
}

const sizeConfig: Record<
  StrategyBadgeSize,
  {
    container: string;
    name: string;
    dotWrapper: string;
    dotSize: string;
    chip: string;
    timeframeIcon: string;
  }
> = {
  sm: {
    container: "gap-1.5",
    name: "text-xs font-bold leading-tight",
    dotWrapper: "h-2 w-2",
    dotSize: "h-1.5 w-1.5",
    chip: "text-[10px] px-1.5 py-0.2 rounded font-medium",
    timeframeIcon: "size-2.5",
  },
  md: {
    container: "gap-2",
    name: "text-sm font-bold leading-snug",
    dotWrapper: "h-2.5 w-2.5",
    dotSize: "h-2 w-2",
    chip: "text-2xs px-2 py-0.5 rounded-md font-medium",
    timeframeIcon: "size-3",
  },
  lg: {
    container: "gap-2.5",
    name: "text-base font-extrabold leading-normal tracking-tight",
    dotWrapper: "h-3 w-3",
    dotSize: "h-2.5 w-2.5",
    chip: "text-xs px-2.5 py-1 rounded-md font-semibold",
    timeframeIcon: "size-3.5",
  },
};

/**
 * Status pulse dot with motion-safe ping animation
 */
function StatusPulseDot({
  status,
  sizeClass,
  wrapperClass,
}: {
  status: StrategyStatus;
  sizeClass: string;
  wrapperClass: string;
}) {
  const norm = status?.toUpperCase() ?? "IDLE";

  if (norm === "RUNNING") {
    return (
      <span className={cn("relative flex items-center justify-center shrink-0", wrapperClass)} aria-hidden="true">
        <span className="absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75 motion-safe:animate-ping" />
        <span className={cn("relative inline-flex rounded-full bg-emerald-500 ring-2 ring-emerald-400/40", sizeClass)} />
      </span>
    );
  }

  if (norm === "PAUSED") {
    return (
      <span className={cn("relative flex items-center justify-center shrink-0", wrapperClass)} aria-hidden="true">
        <span className={cn("inline-flex rounded-full bg-amber-400 ring-2 ring-amber-400/30", sizeClass)} />
      </span>
    );
  }

  if (norm === "STOPPED") {
    return (
      <span className={cn("relative flex items-center justify-center shrink-0", wrapperClass)} aria-hidden="true">
        <span className={cn("inline-flex rounded-full bg-zinc-500 ring-2 ring-zinc-600/30", sizeClass)} />
      </span>
    );
  }

  if (norm === "IDLE") {
    return (
      <span className={cn("relative flex items-center justify-center shrink-0", wrapperClass)} aria-hidden="true">
        <span className={cn("inline-flex rounded-full bg-sky-400 ring-2 ring-sky-400/30", sizeClass)} />
      </span>
    );
  }

  // Error / Alert fallback
  return (
    <span className={cn("relative flex items-center justify-center shrink-0", wrapperClass)} aria-hidden="true">
      <span className={cn("inline-flex rounded-full bg-red-500 ring-2 ring-red-400/30", sizeClass)} />
    </span>
  );
}

/**
 * Maps category name to high contrast semantic chip classes
 */
function getCategoryColor(category?: string): string {
  if (!category) return "bg-zinc-800/80 text-zinc-300 border-zinc-700/60";

  const catLower = category.toLowerCase();
  if (catLower.includes("trend")) {
    return "bg-emerald-950/40 text-emerald-300 border-emerald-500/30";
  }
  if (catLower.includes("mean") || catLower.includes("reversion")) {
    return "bg-indigo-950/40 text-indigo-300 border-indigo-500/30";
  }
  if (catLower.includes("breakout") || catLower.includes("momentum")) {
    return "bg-amber-950/40 text-amber-300 border-amber-500/30";
  }
  if (catLower.includes("arbitrage") || catLower.includes("market making")) {
    return "bg-cyan-950/40 text-cyan-300 border-cyan-500/30";
  }
  if (catLower.includes("ai") || catLower.includes("mandate") || catLower.includes("copilot")) {
    return "bg-purple-950/40 text-purple-300 border-purple-500/30";
  }

  return "bg-zinc-800/80 text-zinc-300 border-zinc-700/60";
}

export function StrategyIdentityBadge({
  name,
  category,
  status,
  timeframe,
  instrument,
  size = "md",
  className,
}: StrategyIdentityBadgeProps) {
  const sz = sizeConfig[size] ?? sizeConfig.md;
  const statusUpper = status?.toUpperCase() ?? "IDLE";

  return (
    <div
      role="group"
      aria-label={`Strategy: ${name}, Status: ${status}, ${category ? `Category: ${category}, ` : ""}${
        timeframe ? `Timeframe: ${timeframe}` : ""
      }`}
      className={cn("inline-flex flex-wrap items-center", sz.container, className)}
    >
      {/* Status Pulse Dot and Strategy Name */}
      <div className="flex items-center gap-1.5 min-w-0">
        <StatusPulseDot status={status} sizeClass={sz.dotSize} wrapperClass={sz.dotWrapper} />
        <span
          className={cn("text-zinc-100 truncate tracking-tight font-sans", sz.name)}
          title={`Strategy: ${name} (${statusUpper})`}
        >
          {name}
        </span>
      </div>

      {/* Timeframe Chip */}
      {timeframe && (
        <span
          className={cn(
            "inline-flex items-center gap-1 font-mono uppercase tracking-wider border select-none",
            "bg-zinc-900/90 text-zinc-300 border-zinc-700/60 shadow-xs",
            sz.chip
          )}
          title={`Timeframe: ${timeframe}`}
        >
          <Clock className={cn("text-zinc-400 shrink-0", sz.timeframeIcon)} aria-hidden="true" />
          <span>{timeframe}</span>
        </span>
      )}

      {/* Category Badge */}
      {category && (
        <span
          className={cn(
            "inline-flex items-center font-medium border select-none shadow-xs",
            getCategoryColor(category),
            sz.chip
          )}
          title={`Category: ${category}`}
        >
          {category}
        </span>
      )}

      {/* Instrument Chip (if provided) */}
      {instrument && (
        <span
          className={cn(
            "inline-flex items-center font-mono font-medium uppercase border select-none",
            "bg-zinc-900/60 text-zinc-400 border-zinc-800",
            sz.chip
          )}
          title={`Instrument: ${instrument}`}
        >
          {instrument}
        </span>
      )}
    </div>
  );
}
