"use client";

import React from "react";
import {
  Activity,
  Minimize2,
  MoveHorizontal,
  Repeat,
  TrendingUp,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/utils";

export interface MarketRegimePillProps {
  regime: string;
  className?: string;
}

interface RegimeStyle {
  label: string;
  badgeClass: string;
  icon: React.ComponentType<{ className?: string }>;
}

function resolveRegimeStyle(regime: string): RegimeStyle {
  const norm = regime?.trim() ?? "Unknown";
  const lower = norm.toLowerCase();

  if (lower.includes("high vol") || lower.includes("volatile") || lower.includes("breakout")) {
    return {
      label: norm,
      badgeClass:
        "bg-amber-500/10 text-amber-300 border-amber-500/30 hover:border-amber-500/50 hover:bg-amber-500/15",
      icon: Zap,
    };
  }

  if (lower.includes("trend") || lower.includes("directional") || lower.includes("momentum")) {
    return {
      label: norm,
      badgeClass:
        "bg-emerald-500/10 text-emerald-300 border-emerald-500/30 hover:border-emerald-500/50 hover:bg-emerald-500/15",
      icon: TrendingUp,
    };
  }

  if (lower.includes("range") || lower.includes("consolidation") || lower.includes("sideways") || lower.includes("channel")) {
    return {
      label: norm,
      badgeClass:
        "bg-violet-500/10 text-violet-300 border-violet-500/30 hover:border-violet-500/50 hover:bg-violet-500/15",
      icon: MoveHorizontal,
    };
  }

  if (lower.includes("low vol") || lower.includes("compression") || lower.includes("quiet")) {
    return {
      label: norm,
      badgeClass:
        "bg-sky-500/10 text-sky-300 border-sky-500/30 hover:border-sky-500/50 hover:bg-sky-500/15",
      icon: Minimize2,
    };
  }

  if (lower.includes("mean") || lower.includes("revert")) {
    return {
      label: norm,
      badgeClass:
        "bg-teal-500/10 text-teal-300 border-teal-500/30 hover:border-teal-500/50 hover:bg-teal-500/15",
      icon: Repeat,
    };
  }

  return {
    label: norm,
    badgeClass:
      "bg-zinc-800/60 text-zinc-300 border-zinc-700/50 hover:border-zinc-600 hover:bg-zinc-800/80",
    icon: Activity,
  };
}

export function MarketRegimePill({ regime, className }: MarketRegimePillProps) {
  const { label, badgeClass, icon: Icon } = resolveRegimeStyle(regime);

  return (
    <span
      role="status"
      title={`Market Regime: ${label}`}
      aria-label={`Optimal Market Regime: ${label}`}
      className={cn(
        "inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-2xs font-semibold uppercase tracking-wider",
        "border shadow-xs backdrop-blur-xs select-none transition-all duration-150",
        badgeClass,
        className
      )}
    >
      <Icon className="size-3 shrink-0" aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
