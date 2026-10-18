import React from "react";
import { Zap, RotateCcw, Package } from "lucide-react";
import { cn } from "@/lib/utils";

export interface ApplyModeBadgeProps {
  apply: "hot" | "restart" | "deploy";
  compact?: boolean;
  className?: string;
}

interface ApplyConfig {
  label: string;
  shortLabel: string;
  icon: React.ComponentType<{ className?: string }>;
  badgeClass: string;
  iconClass: string;
}

const APPLY_CONFIGS: Record<ApplyModeBadgeProps["apply"], ApplyConfig> = {
  hot: {
    label: "Applies within seconds",
    shortLabel: "hot",
    icon: Zap,
    badgeClass: "border-sky-500/30 bg-sky-950/50 text-sky-200 hover:border-sky-400/50",
    iconClass: "text-sky-300",
  },
  restart: {
    label: "Applies on service restart",
    shortLabel: "restart",
    icon: RotateCcw,
    badgeClass: "border-amber-500/30 bg-amber-950/50 text-amber-200 hover:border-amber-400/50",
    iconClass: "text-amber-300",
  },
  deploy: {
    label: "Applies on deploy",
    shortLabel: "deploy",
    icon: Package,
    badgeClass: "border-violet-500/30 bg-violet-950/50 text-violet-200 hover:border-violet-400/50",
    iconClass: "text-violet-300",
  },
};

export const ApplyModeBadge: React.FC<ApplyModeBadgeProps> = ({
  apply,
  compact = false,
  className,
}) => {
  const config = APPLY_CONFIGS[apply] ?? {
    label: apply,
    shortLabel: apply,
    icon: Zap,
    badgeClass: "border-zinc-700/50 bg-zinc-900/60 text-zinc-300",
    iconClass: "text-zinc-400",
  };

  const Icon = config.icon;
  const displayText = compact ? config.shortLabel : config.label;

  return (
    <span
      role="status"
      aria-label={config.label}
      title={config.label}
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-medium tracking-wide transition-colors select-none",
        config.badgeClass,
        className
      )}
    >
      <Icon className={cn("size-3.5 shrink-0", config.iconClass)} aria-hidden="true" />
      <span>{displayText}</span>
    </span>
  );
};

export default ApplyModeBadge;
