"use client";

import React from "react";
import { ShieldCheck, ShieldAlert, Lock, ExternalLink, Scale, CheckCircle2, AlertTriangle } from "lucide-react";
import { dispatchSwitchWorkspace } from "@/lib/navigation";
import { Button } from "@/components/ui/button";
import { SettingCard, type RegistryEntry } from "../controls/SettingCard";

export interface RiskGuardrailsSectionProps {
  entries: RegistryEntry[];
  onReload: () => Promise<void>;
  highlightedKey?: string;
}

export const RiskGuardrailsSection: React.FC<RiskGuardrailsSectionProps> = ({
  entries,
  onReload,
  highlightedKey,
}) => {
  const getEntry = (key: string): RegistryEntry | undefined => {
    return entries.find((e) => e.key === key);
  };

  const dynamicRiskKeys = [
    "risk.max_daily_loss_pct",
    "risk.max_order_equity_pct",
    "risk.cross_mandate_exposure_pct",
  ];

  const fixedGuardKeys = [
    "guard.position_clamp_pct",
    "guard.account_backstop_pct",
    "guard.min_order_pct",
  ];

  // ADR 0009: the grade ceilings and the slippage allowance are the operator's; the leverage guardrails are fixed
  const leverageKeys = [
    "leverage.grade_ceiling_a",
    "leverage.grade_ceiling_b",
    "leverage.grade_ceiling_c",
    "leverage.slippage_allowance_pct",
    // ADR 0010 amendment (auto-trade Phase B): scanner debates routed to a perp mandate per UTC day
    "debate.perp_mandate_daily_cap",
  ];
  const leverageGuardKeys = [
    "guard.mandate_max_leverage",
    "guard.account_max_leverage",
    "guard.perps_level",
    "guard.max_risk_per_trade_pct",
    "guard.liquidation_buffer_pct",
    "guard.perp_notional_pct",
  ];
  const leverageEntries = leverageKeys.map((k) => getEntry(k)).filter((e): e is RegistryEntry => Boolean(e));
  const leverageGuardEntries = leverageGuardKeys
    .map((k) => getEntry(k))
    .filter((e): e is RegistryEntry => Boolean(e));

  const dynamicEntries = dynamicRiskKeys
    .map((k) => getEntry(k))
    .filter((e): e is RegistryEntry => Boolean(e));

  const fixedEntries = fixedGuardKeys
    .map((k) => getEntry(k))
    .filter((e): e is RegistryEntry => Boolean(e));

  return (
    <div className="space-y-8 max-w-5xl">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-white/10 pb-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
            <Scale className="w-5 h-5 text-cyan-400" aria-hidden="true" />
            Risk Limits & Platform Guardrails
          </h2>
          <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
            Capital allocation boundaries, position size caps, and hard-coded mathematical invariants protecting equity.
          </p>
        </div>
        <Button
          type="button"
          onClick={() => dispatchSwitchWorkspace({ mode: "operator" })}
          className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold text-xs gap-1.5"
        >
          <ExternalLink className="w-3.5 h-3.5" aria-hidden="true" />
          Open Operator Risk Limits
        </Button>
      </div>

      {/* Part 1: Dynamic Risk Limit Overrides */}
      <section className="space-y-4">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-2">
            <ShieldAlert className="w-4 h-4" aria-hidden="true" />
            Dynamic Risk Limit Overrides (ADR 0006)
          </h3>
          <p className="text-xs text-zinc-400 mt-0.5">
            Account-level ceilings configured for the paper trading environment. Tighter is free, loosening requires TOTP.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {dynamicEntries.map((entry) => (
            <SettingCard
              key={entry.key}
              entry={entry}
              allEntries={entries}
              onSaved={() => void onReload()}
              highlight={highlightedKey === entry.key}
            />
          ))}
        </div>
      </section>

      {/* Part 2: Fixed Invariant Guardrails */}
      <section className="space-y-4">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-purple-400 flex items-center gap-2">
            <Lock className="w-4 h-4" aria-hidden="true" />
            Fixed Invariant Guardrails (ADR 0004)
          </h3>
          <p className="text-xs text-zinc-400 mt-0.5">
            Hardcoded safeguards in <span className="font-mono text-zinc-300">risk_limits.py</span>. Read-only by design; cannot be modified by any runtime operator.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {fixedEntries.map((entry) => (
            <SettingCard
              key={entry.key}
              entry={entry}
              allEntries={entries}
              onSaved={() => void onReload()}
              highlight={highlightedKey === entry.key}
            />
          ))}
        </div>
      </section>

      {/* Part 3: Leverage (ADR 0009) */}
      <section className="space-y-4" data-testid="leverage-settings">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-2">
            <Scale className="w-4 h-4" aria-hidden="true" />
            Leverage (ADR 0009)
          </h3>
          <p className="text-xs text-zinc-400 mt-0.5">
            Setup-grade ceilings and the stop slippage allowance feed every leverage plan (card, sizing, dispatcher).
            Lowering a ceiling or raising the allowance is free; the other way needs TOTP. The guardrails below are fixed,
            each with its source.
          </p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          {leverageEntries.map((entry) => (
            <SettingCard
              key={entry.key}
              entry={entry}
              allEntries={entries}
              onSaved={() => void onReload()}
              highlight={highlightedKey === entry.key}
            />
          ))}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {leverageGuardEntries.map((entry) => (
            <SettingCard
              key={entry.key}
              entry={entry}
              allEntries={entries}
              onSaved={() => void onReload()}
              highlight={highlightedKey === entry.key}
            />
          ))}
        </div>
      </section>

      {/* Part 4: Mandate & Execution Safety Invariants Reference Card */}
      <section className="rounded-xl border border-white/10 bg-zinc-950/60 p-5 space-y-4">
        <div className="flex items-center gap-2 text-zinc-200">
          <ShieldCheck className="w-5 h-5 text-emerald-400" aria-hidden="true" />
          <h3 className="text-sm font-bold uppercase tracking-wider">
            Core Mandate & Execution Safety Invariants (ADR 0004 / ADR 0007 §7)
          </h3>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-1">
          <div className="rounded-lg border border-white/5 bg-zinc-900/40 p-3.5 space-y-2">
            <div className="flex items-center gap-2 text-xs font-semibold text-emerald-300">
              <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" aria-hidden="true" />
              Protected Roles & Fail-Closed Gateways
            </div>
            <p className="text-[11px] text-zinc-400 leading-relaxed">
              Trader and Risk Auditor roles are permanently certified with <span className="font-mono text-zinc-200">FAIL_CLOSED</span> fallback. Any provider disconnect immediately halts new candidate proposals rather than executing unhedged.
            </p>
          </div>

          <div className="rounded-lg border border-white/5 bg-zinc-900/40 p-3.5 space-y-2">
            <div className="flex items-center gap-2 text-xs font-semibold text-emerald-300">
              <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" aria-hidden="true" />
              Asymmetric Approval Timeouts
            </div>
            <p className="text-[11px] text-zinc-400 leading-relaxed">
              Operator approvals follow strict risk directionality: proposals that <em>increase risk</em> expire to <strong>REJECT</strong> after 300 seconds. Proposals that <em>reduce risk</em> (deleveraging, emergency stop) proceed immediately.
            </p>
          </div>

          <div className="rounded-lg border border-white/5 bg-zinc-900/40 p-3.5 space-y-2">
            <div className="flex items-center gap-2 text-xs font-semibold text-emerald-300">
              <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" aria-hidden="true" />
              Leveraged Perpetuals (ADR 0009)
            </div>
            <p className="text-[11px] text-zinc-400 leading-relaxed">
              Perps only at L1, opted in per mandate with TOTP, so every entry is approved with TOTP. ISOLATED only, a
              stop on every entry, at most 10x (each mandate&apos;s own maximum, default 3x), the loss at the stop within
              1% of the budget (cap 2%), and notional within 25% of equity.
            </p>
          </div>

          <div className="rounded-lg border border-white/5 bg-zinc-900/40 p-3.5 space-y-2">
            <div className="flex items-center gap-2 text-xs font-semibold text-rose-300">
              <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0" aria-hidden="true" />
              Account Backstop Circuit Breaker
            </div>
            <p className="text-[11px] text-zinc-400 leading-relaxed">
              If portfolio drawdown breaches <span className="font-mono text-zinc-200">guard.account_backstop_pct</span> (20%), the engine latches the emergency kill switch, cancels all open working orders, and halts automated execution.
            </p>
          </div>
        </div>
      </section>
    </div>
  );
};
