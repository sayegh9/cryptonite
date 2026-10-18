"use client";

import React, { useState } from "react";
import { ShieldAlert, Zap } from "lucide-react";
import { ExitConstraintGauge, type ExitValuesSet } from "../controls/ExitConstraintGauge";
import { SettingCard, type RegistryEntry } from "../controls/SettingCard";

export interface ExitRulesSectionProps {
  entries: RegistryEntry[];
  onReload: () => Promise<void>;
  highlightedKey?: string;
  onSelectKey?: (key: string) => void;
}

export const ExitRulesSection: React.FC<ExitRulesSectionProps> = ({
  entries,
  onReload,
  highlightedKey: externalHighlightedKey,
  onSelectKey,
}) => {
  const [internalHighlightedKey, setInternalHighlightedKey] = useState<string | null>(null);
  const activeHighlight = externalHighlightedKey || internalHighlightedKey;

  const getEntry = (key: string): RegistryEntry | undefined => {
    return entries.find((e) => e.key === key);
  };

  const getNumVal = (key: string, fallback: number): number => {
    const e = getEntry(key);
    if (!e) return fallback;
    const v = e.value !== null && e.value !== undefined ? e.value : e.default;
    const parsed = parseFloat(v);
    return isNaN(parsed) ? fallback : parsed;
  };

  const getEffectiveNumVal = (key: string): number | undefined => {
    const e = getEntry(key);
    if (!e || !e.effective) return undefined;
    const parsed = parseFloat(e.effective);
    return isNaN(parsed) ? undefined : parsed;
  };

  const values: ExitValuesSet = {
    stop_loss_pct: getNumVal("exit.stop_loss_pct", 5),
    debate_drawdown_pct: getNumVal("exit.debate_drawdown_pct", 2),
    trail_activate_pct: getNumVal("exit.trail_activate_pct", 3),
    trail_pct: getNumVal("exit.trail_pct", 2),
    funding_spike: getNumVal("exit.funding_spike", 0.0005),
  };

  const appliedValues: Partial<ExitValuesSet> = {
    stop_loss_pct: getEffectiveNumVal("exit.stop_loss_pct"),
    debate_drawdown_pct: getEffectiveNumVal("exit.debate_drawdown_pct"),
    trail_activate_pct: getEffectiveNumVal("exit.trail_activate_pct"),
    trail_pct: getEffectiveNumVal("exit.trail_pct"),
    funding_spike: getEffectiveNumVal("exit.funding_spike"),
  };

  const handleQuickSelect = (key: string) => {
    setInternalHighlightedKey(key);
    if (onSelectKey) {
      onSelectKey(key);
    }
  };

  const exitKeys = [
    "exit.stop_loss_pct",
    "exit.debate_drawdown_pct",
    "exit.trail_activate_pct",
    "exit.trail_pct",
    "exit.funding_spike",
    "exit.monitor_interval_s",
  ];

  const exitEntries = exitKeys
    .map((k) => getEntry(k))
    .filter((e): e is RegistryEntry => Boolean(e));

  return (
    <div className="space-y-6 max-w-5xl">
      {/* Section Header */}
      <div className="border-b border-white/10 pb-4">
        <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
          <ShieldAlert className="w-5 h-5 text-cyan-400" aria-hidden="true" />
          Exit Rules & Constraint Monitor
        </h2>
        <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
          Dynamic exit thresholds monitored continuously by the execution engine (ADR 0002 / ADR 0007). Lowering values (tightening exits) is free; loosening requires operator TOTP. Cross-field invariant rules are verified in real time.
        </p>
      </div>

      {/* Visual Timeline and Constraint Gauge */}
      <div>
        <ExitConstraintGauge
          values={values}
          appliedValues={appliedValues}
          onQuickSelect={handleQuickSelect}
        />
      </div>

      {/* Invariant Reference Notice */}
      <div className="rounded-lg border border-cyan-500/20 bg-cyan-950/20 p-3.5 flex items-start gap-3 text-xs text-zinc-300">
        <Zap className="w-4 h-4 text-cyan-400 shrink-0 mt-0.5" aria-hidden="true" />
        <div className="space-y-1">
          <strong className="text-cyan-300 font-semibold">ADR 0007 Constraint Laws:</strong>
          <p className="text-[11px] text-zinc-400 leading-normal">
            1. <span className="font-mono text-zinc-200">exit.debate_drawdown_pct</span> must always be strictly less than <span className="font-mono text-zinc-200">exit.stop_loss_pct</span> so AI debate evaluation executes before a hard stop liquidation.
            <br />
            2. <span className="font-mono text-zinc-200">exit.trail_pct</span> must not exceed <span className="font-mono text-zinc-200">exit.trail_activate_pct</span> to ensure the trailing stop floor triggers at or above break-even.
          </p>
        </div>
      </div>

      {/* Setting Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {exitEntries.map((entry) => (
          <SettingCard
            key={entry.key}
            entry={entry}
            allEntries={entries}
            onSaved={() => void onReload()}
            highlight={activeHighlight === entry.key}
          />
        ))}
      </div>
    </div>
  );
};
