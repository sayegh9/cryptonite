import React from "react";
import { cn } from "@/lib/utils";

export interface TierBadgeProps {
  tier: string;
  tighter?: "lower" | "higher";
  showDescription?: boolean;
  className?: string;
}

interface TierConfig {
  label: string;
  badgeClass: string;
  dotClass: string;
  getDescription: (tighter?: "lower" | "higher") => string;
}

const TIER_CONFIGS: Record<string, TierConfig> = {
  "T/L": {
    label: "T/L",
    badgeClass: "border-cyan-500/40 bg-cyan-950/70 text-cyan-200 hover:border-cyan-400/60",
    dotClass: "bg-cyan-400",
    getDescription: (tighter) =>
      tighter === "higher"
        ? "T/L · raising is free, lowering needs TOTP"
        : "T/L · lowering is free, raising needs TOTP",
  },
  TOTP: {
    label: "TOTP",
    badgeClass: "border-amber-500/40 bg-amber-950/70 text-amber-200 hover:border-amber-400/60",
    dotClass: "bg-amber-400",
    getDescription: () => "TOTP · 2FA required for every change",
  },
  Session: {
    label: "Session",
    badgeClass: "border-emerald-500/40 bg-emerald-950/70 text-emerald-200 hover:border-emerald-400/60",
    dotClass: "bg-emerald-400",
    getDescription: () => "Session · operator session only",
  },
  G: {
    label: "Governance",
    badgeClass: "border-purple-500/40 bg-purple-950/70 text-purple-200 hover:border-purple-400/60",
    dotClass: "bg-purple-400",
    getDescription: () => "Governance · changed by ADR only",
  },
  R: {
    label: "Read-only",
    badgeClass: "border-zinc-700/50 bg-zinc-900/80 text-zinc-300 hover:border-zinc-600/70",
    dotClass: "bg-zinc-400",
    getDescription: () => "Read-only",
  },
  Fixed: {
    label: "Fixed",
    badgeClass: "border-slate-700/50 bg-slate-900/80 text-slate-300 hover:border-slate-600/70",
    dotClass: "bg-slate-400",
    getDescription: () => "Fixed in code",
  },
};

export const TierBadge: React.FC<TierBadgeProps> = ({
  tier,
  tighter,
  showDescription = false,
  className,
}) => {
  const config = TIER_CONFIGS[tier] ?? {
    label: tier,
    badgeClass: "border-zinc-700/50 bg-zinc-900/80 text-zinc-300",
    dotClass: "bg-zinc-400",
    getDescription: () => tier,
  };

  const description = config.getDescription(tighter);
  const displayText = showDescription ? description : tier;

  return (
    <span
      role="status"
      aria-label={description}
      title={description}
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-2 py-0.5 font-mono text-xs font-medium tracking-wide transition-colors select-none",
        config.badgeClass,
        className
      )}
    >
      <span
        className={cn("size-1.5 shrink-0 rounded-full", config.dotClass)}
        aria-hidden="true"
      />
      <span>{displayText}</span>
    </span>
  );
};

export default TierBadge;
