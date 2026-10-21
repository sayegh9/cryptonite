"use client";

import React, { useEffect, useRef } from "react";
import { Lock } from "lucide-react";
import { cn } from "@/lib/utils";
import { displayEntryValue, type RegistryEntry } from "./SettingCard";

export interface FixedGuardrailsProps {
  entries: RegistryEntry[];
  /** Key to ring and scroll to (a jump from global search) */
  highlightedKey?: string | null;
  /** Accessible name of the table */
  label: string;
  className?: string;
}

const COLUMNS = "@[40rem]:grid-cols-[minmax(0,2.4fr)_minmax(6rem,0.8fr)_minmax(0,1.3fr)]";

const GuardRow: React.FC<{ entry: RegistryEntry; highlight: boolean }> = ({ entry, highlight }) => {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (highlight) ref.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [highlight]);

  return (
    <div
      ref={ref}
      role="row"
      data-testid={`setting-card-${entry.key}`}
      className={cn(
        "grid gap-x-6 gap-y-1 px-3 py-2.5 @[40rem]:items-baseline @xl:px-4",
        COLUMNS,
        highlight && "ring-2 ring-inset ring-cyan-500/80 bg-cyan-950/20"
      )}
    >
      <div role="cell" className="min-w-0">
        <div className="text-sm font-medium leading-snug text-zinc-100 [overflow-wrap:anywhere]">{entry.label}</div>
        <div title={entry.key} className="truncate font-mono text-2xs text-zinc-400">
          {entry.key}
        </div>
        {entry.note && <p className="mt-0.5 text-xs leading-relaxed text-zinc-400 [overflow-wrap:anywhere]">{entry.note}</p>}
      </div>
      <div role="cell" className="font-mono text-sm font-semibold text-zinc-100 [overflow-wrap:anywhere] @[40rem]:text-right">
        {displayEntryValue(entry, entry.value ?? entry.default)}
      </div>
      <div role="cell" className="min-w-0 text-2xs text-zinc-400 [overflow-wrap:anywhere]">
        <span className="font-mono">{entry.store}</span>
        {entry.adr ? <span> · ADR {entry.adr}</span> : null}
      </div>
    </div>
  );
};

/** Read-only guardrails as a compact name / value / source table; rows stack below 640px of width. */
export const FixedGuardrails: React.FC<FixedGuardrailsProps> = ({ entries, highlightedKey, label, className }) => {
  if (entries.length === 0) return null;
  return (
    <div
      role="table"
      aria-label={label}
      className={cn("@container overflow-hidden rounded-lg border border-white/10 bg-zinc-950/60", className)}
    >
      <div role="rowgroup" className="hidden border-b border-white/10 bg-zinc-900/60 @[40rem]:block">
        <div role="row" className={cn("grid gap-x-6 px-3 py-1.5 text-2xs font-medium text-zinc-400 @xl:px-4", COLUMNS)}>
          <div role="columnheader" className="flex items-center gap-1.5">
            <Lock className="size-3" aria-hidden="true" />
            Guardrail
          </div>
          <div role="columnheader" className="text-right">
            Value
          </div>
          <div role="columnheader">Source</div>
        </div>
      </div>
      <div role="rowgroup" className="divide-y divide-white/5">
        {entries.map((entry) => (
          <GuardRow key={entry.key} entry={entry} highlight={highlightedKey === entry.key} />
        ))}
      </div>
    </div>
  );
};

export default FixedGuardrails;
