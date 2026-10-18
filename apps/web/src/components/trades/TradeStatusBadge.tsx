"use client";

import React from "react";
import {
  AlertOctagon,
  ArrowDownRight,
  ArrowUpRight,
  Ban,
  CheckCircle2,
  CircleDot,
  Minus,
  PieChart,
} from "lucide-react";
import { cn } from "@/lib/utils";

export type TradeSide = "LONG" | "SHORT" | "FLAT" | string;
export type TradeStatus = "FILLED" | "PARTIAL" | "CANCELLED" | "REJECTED" | "OPEN" | string;
export type BadgeSize = "sm" | "md" | "lg";

export interface SideBadgeProps {
  side: TradeSide;
  size?: BadgeSize;
  showIcon?: boolean;
  className?: string;
}

export interface OrderStatusBadgeProps {
  status: TradeStatus;
  size?: BadgeSize;
  showIcon?: boolean;
  className?: string;
}

export interface TradeStatusBadgeProps {
  status?: TradeStatus;
  side?: TradeSide;
  size?: BadgeSize;
  showIcon?: boolean;
  className?: string;
}

const sizeClasses: Record<BadgeSize, { badge: string; icon: string }> = {
  sm: {
    badge: "text-2xs px-1.5 py-0.5 gap-1 tracking-wider",
    icon: "size-2.5",
  },
  md: {
    badge: "text-xs px-2 py-0.5 gap-1.5 tracking-wider",
    icon: "size-3",
  },
  lg: {
    badge: "text-sm px-2.5 py-1 gap-1.5 tracking-wide",
    icon: "size-3.5",
  },
};

/**
 * Dedicated Side Badge (LONG / SHORT / FLAT) with WCAG AA compliance
 */
export function SideBadge({ side, size = "sm", showIcon = true, className }: SideBadgeProps) {
  const norm = side?.toUpperCase() ?? "FLAT";
  const sz = sizeClasses[size] ?? sizeClasses.sm;

  if (norm === "LONG") {
    return (
      <span
        role="status"
        aria-label="Side: Long"
        className={cn(
          "inline-flex items-center rounded font-black uppercase border select-none transition-colors",
          "bg-emerald-50 text-emerald-900 border-emerald-300",
          "dark:bg-emerald-950/60 dark:text-emerald-300 dark:border-emerald-500/40",
          sz.badge,
          className
        )}
      >
        {showIcon && <ArrowUpRight className={sz.icon} aria-hidden="true" />}
        <span>LONG</span>
      </span>
    );
  }

  if (norm === "SHORT") {
    return (
      <span
        role="status"
        aria-label="Side: Short"
        className={cn(
          "inline-flex items-center rounded font-black uppercase border select-none transition-colors",
          "bg-rose-50 text-rose-900 border-rose-300",
          "dark:bg-rose-950/60 dark:text-rose-300 dark:border-rose-500/40",
          sz.badge,
          className
        )}
      >
        {showIcon && <ArrowDownRight className={sz.icon} aria-hidden="true" />}
        <span>SHORT</span>
      </span>
    );
  }

  return (
    <span
      role="status"
      aria-label="Side: Flat"
      className={cn(
        "inline-flex items-center rounded font-bold uppercase border select-none transition-colors",
        "bg-zinc-100 text-zinc-700 border-zinc-300",
        "dark:bg-zinc-900/60 dark:text-zinc-400 dark:border-zinc-700/50",
        sz.badge,
        className
      )}
    >
      {showIcon && <Minus className={sz.icon} aria-hidden="true" />}
      <span>FLAT</span>
    </span>
  );
}

/**
 * Dedicated Order/Trade Status Badge (FILLED, PARTIAL, OPEN, CANCELLED, REJECTED)
 */
export function OrderStatusBadge({ status, size = "sm", showIcon = true, className }: OrderStatusBadgeProps) {
  const norm = status?.toUpperCase() ?? "UNKNOWN";
  const sz = sizeClasses[size] ?? sizeClasses.sm;

  switch (norm) {
    case "FILLED":
      return (
        <span
          role="status"
          aria-label="Order Status: Filled"
          className={cn(
            "inline-flex items-center rounded font-bold uppercase border select-none transition-colors",
            "bg-emerald-50 text-emerald-900 border-emerald-300",
            "dark:bg-emerald-950/60 dark:text-emerald-300 dark:border-emerald-600/40",
            sz.badge,
            className
          )}
        >
          {showIcon && <CheckCircle2 className={sz.icon} aria-hidden="true" />}
          <span>FILLED</span>
        </span>
      );

    case "PARTIAL":
      return (
        <span
          role="status"
          aria-label="Order Status: Partially Filled"
          className={cn(
            "inline-flex items-center rounded font-bold uppercase border select-none transition-colors",
            "bg-amber-50 text-amber-900 border-amber-300",
            "dark:bg-amber-950/60 dark:text-amber-300 dark:border-amber-500/40",
            sz.badge,
            className
          )}
        >
          {showIcon && <PieChart className={sz.icon} aria-hidden="true" />}
          <span>PARTIAL</span>
        </span>
      );

    case "OPEN":
      return (
        <span
          role="status"
          aria-label="Order Status: Open"
          className={cn(
            "inline-flex items-center rounded font-bold uppercase border select-none transition-colors",
            "bg-sky-50 text-sky-900 border-sky-300",
            "dark:bg-sky-950/60 dark:text-sky-300 dark:border-sky-500/40",
            sz.badge,
            className
          )}
        >
          {showIcon && <CircleDot className={sz.icon} aria-hidden="true" />}
          <span>OPEN</span>
        </span>
      );

    case "CANCELLED":
    case "CANCELED":
      return (
        <span
          role="status"
          aria-label="Order Status: Cancelled"
          className={cn(
            "inline-flex items-center rounded font-bold uppercase border select-none transition-colors",
            "bg-zinc-100 text-zinc-700 border-zinc-300",
            "dark:bg-zinc-900/60 dark:text-zinc-400 dark:border-zinc-700/50",
            sz.badge,
            className
          )}
        >
          {showIcon && <Ban className={sz.icon} aria-hidden="true" />}
          <span>CANCELLED</span>
        </span>
      );

    case "REJECTED":
      return (
        <span
          role="status"
          aria-label="Order Status: Rejected"
          className={cn(
            "inline-flex items-center rounded font-bold uppercase border select-none transition-colors",
            "bg-red-50 text-red-900 border-red-300",
            "dark:bg-red-950/70 dark:text-red-200 dark:border-red-600/50",
            sz.badge,
            className
          )}
        >
          {showIcon && <AlertOctagon className={sz.icon} aria-hidden="true" />}
          <span>REJECTED</span>
        </span>
      );

    default:
      return (
        <span
          role="status"
          aria-label={`Order Status: ${status}`}
          className={cn(
            "inline-flex items-center rounded font-bold uppercase border select-none transition-colors",
            "bg-zinc-100 text-zinc-700 border-zinc-300",
            "dark:bg-zinc-900/60 dark:text-zinc-400 dark:border-zinc-700/50",
            sz.badge,
            className
          )}
        >
          <span>{status}</span>
        </span>
      );
  }
}

/**
 * Universal Trade Status Badge supporting side, status, or both
 */
export function TradeStatusBadge({
  status,
  side,
  size = "sm",
  showIcon = true,
  className,
}: TradeStatusBadgeProps) {
  if (side && status) {
    return (
      <div className={cn("inline-flex items-center gap-1.5", className)}>
        <SideBadge side={side} size={size} showIcon={showIcon} />
        <OrderStatusBadge status={status} size={size} showIcon={showIcon} />
      </div>
    );
  }

  if (side) {
    return <SideBadge side={side} size={size} showIcon={showIcon} className={className} />;
  }

  if (status) {
    return <OrderStatusBadge status={status} size={size} showIcon={showIcon} className={className} />;
  }

  return null;
}
