"use client";

import React, { useEffect, useState } from "react";
import { ageLabel, freshness, type AsOf, type FreshState, UNKNOWN_TEXT } from "@/lib/freshness";

const BADGE: Record<Exclude<FreshState, "live">, string> = {
  delayed: "text-amber-400 border-amber-800/60 bg-amber-950/40",
  stale: "text-red-400 border-red-800/60 bg-red-950/40",
  unknown: "text-zinc-400 border-zinc-700 bg-zinc-900",
};

interface FreshProps<T> {
  /** The value; null/undefined (or a non-finite number) renders as unknown. */
  value: T | null | undefined;
  /** When the value was observed (epoch ms/s, ISO string or Date). */
  asOf: AsOf;
  /** Age up to which the value counts as live. */
  maxAgeMs: number;
  format?: (v: T) => React.ReactNode;
  className?: string;
  /** Hide the "live" dot; delayed/stale/unknown badges always show. */
  quietWhenLive?: boolean;
  /** Text shown instead of "—" when unknown (e.g. "equity unavailable"). */
  unknownText?: string;
  testId?: string;
}

/** Renders a value together with how fresh it is. Unknown values are never shown as numbers. */
export function Fresh<T>({
  value,
  asOf,
  maxAgeMs,
  format,
  className = "",
  quietWhenLive = true,
  unknownText = UNKNOWN_TEXT,
  testId,
}: FreshProps<T>) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const missing = value === null || value === undefined || (typeof value === "number" && !Number.isFinite(value));
  const state: FreshState = missing ? "unknown" : freshness(asOf, maxAgeMs, now);

  if (state === "unknown") {
    return (
      <span className={`inline-flex items-center gap-1 text-zinc-500 ${className}`} data-fresh="unknown" data-testid={testId} title="No data">
        {unknownText}
      </span>
    );
  }

  return (
    <span className={`inline-flex items-center gap-1 ${className}`} data-fresh={state} data-testid={testId}>
      <span className={state === "stale" ? "opacity-60" : undefined}>{format ? format(value as T) : String(value)}</span>
      {state === "live" ? (
        quietWhenLive ? null : <span role="img" className="h-1.5 w-1.5 rounded-full bg-emerald-400" aria-label="live" />
      ) : (
        <span className={`rounded border px-1 text-2xs leading-tight ${BADGE[state]}`} title={`Observed ${ageLabel(asOf, now)}`}>
          {state}
        </span>
      )}
    </span>
  );
}
