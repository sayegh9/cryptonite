"use client";

import React, { useEffect, useState } from "react";
import { Clock } from "lucide-react";
import { cn } from "@/lib/utils";

export interface DurationTimerProps {
  openedAt: string | null | undefined;
  className?: string;
}

/**
 * Formats elapsed seconds into concise duration string:
 * - < 60s: "< 1m"
 * - < 1h: "14m 32s"
 * - < 24h: "2h 14m 32s"
 * - >= 24h: "3d 4h 12m"
 */
function formatDuration(diffSec: number): string {
  if (diffSec < 60) {
    return "< 1m";
  }

  const d = Math.floor(diffSec / 86400);
  const h = Math.floor((diffSec % 86400) / 3600);
  const m = Math.floor((diffSec % 3600) / 60);
  const s = diffSec % 60;

  if (d > 0) {
    return `${d}d ${h}h ${m}m`;
  }
  if (h > 0) {
    return `${h}h ${m}m ${s}s`;
  }
  return `${m}m ${s}s`;
}

export function DurationTimer({ openedAt, className }: DurationTimerProps) {
  const [elapsedText, setElapsedText] = useState<string>("—");
  const [isoUtc, setIsoUtc] = useState<string>("");

  useEffect(() => {
    if (!openedAt) {
      setElapsedText("—");
      setIsoUtc("");
      return;
    }

    const date = new Date(openedAt);
    if (Number.isNaN(date.getTime())) {
      setElapsedText("—");
      setIsoUtc("");
      return;
    }

    const iso = date.toISOString();
    setIsoUtc(iso);

    const updateTimer = () => {
      const now = Date.now();
      const diffSec = Math.max(0, Math.floor((now - date.getTime()) / 1000));
      setElapsedText(formatDuration(diffSec));
    };

    updateTimer();
    const intervalId = setInterval(updateTimer, 1000);
    return () => clearInterval(intervalId);
  }, [openedAt]);

  if (!openedAt || elapsedText === "—") {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-2xs font-mono text-zinc-500 border border-zinc-800 bg-zinc-900/50",
          className
        )}
        title="Open time not available"
      >
        <Clock className="size-3 text-zinc-600 shrink-0" aria-hidden="true" />
        <span>—</span>
      </span>
    );
  }

  return (
    <time
      dateTime={isoUtc}
      title={`${isoUtc} UTC`}
      aria-label={`Position active duration: ${elapsedText}`}
      className={cn(
        "inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-2xs font-mono font-medium tabular-nums",
        "border border-zinc-800 bg-zinc-900/80 text-zinc-300 shadow-xs transition-colors hover:border-zinc-700 hover:text-zinc-100",
        className
      )}
    >
      <Clock className="size-3 text-zinc-400 shrink-0" aria-hidden="true" />
      <span>{elapsedText}</span>
    </time>
  );
}
