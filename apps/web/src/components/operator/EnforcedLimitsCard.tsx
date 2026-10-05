"use client";

import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { Fresh } from "@/components/Fresh";
import { LIMITS_MAX_AGE_MS, useEnforcedLimits, type EnforcedPaperLimits } from "@/lib/limits";
import { RiskLimitsForm } from "./RiskLimitsForm";
import { listMandates } from "@/lib/mission/client";
import type { Mandate } from "@/lib/mission/types";

const ROWS: { key: keyof EnforcedPaperLimits; label: string; unit: string }[] = [
  { key: "max_daily_loss_pct", label: "Max daily loss", unit: "% of equity" },
  { key: "min_order_equity_pct", label: "Min order size", unit: "% of equity" },
  { key: "max_order_equity_pct", label: "Max order size", unit: "% of equity" },
  { key: "cross_mandate_exposure_pct", label: "Cross-mandate BTC exposure (beta-weighted)", unit: "% of equity" },
  { key: "account_backstop_drawdown_pct", label: "Account backstop drawdown", unit: "%" },
  { key: "taker_fee_bps", label: "Paper taker fee", unit: "bps" },
];

/**
 * The limits the running system enforces (R1.8). Paper limits come from the orchestrator's published
 * dispatcher settings, mandate limits from /ai/mandates; nothing here is a UI constant. The paper limits
 * can be tightened below the code defaults (F7, `RiskLimitsForm`); loosening asks for TOTP.
 */
export function EnforcedLimitsCard() {
  const limits = useEnforcedLimits();
  const [mandates, setMandates] = useState<Mandate[] | null>(null);
  const [mandateError, setMandateError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    listMandates()
      .then((m) => active && setMandates(m))
      .catch((err: { status?: number; message?: string }) => active && setMandateError(err?.status ? `HTTP ${err.status}` : err?.message || "unreachable"));
    return () => {
      active = false;
    };
  }, []);

  const paper = limits.status === "ok" ? limits.data.paper : null;
  const asOf = limits.status === "ok" ? limits.data.as_of : null;
  const why =
    limits.status === "loading" ? "loading…"
    : limits.status === "error" ? `unknown (enforced-limits unavailable: ${limits.error})`
    : paper === null ? "unknown (the orchestrator has not published its limits yet)"
    : null;

  return (
    <section className="bg-zinc-950 border border-white/5 rounded p-6" aria-labelledby="enforced-limits-title" data-testid="enforced-limits">
      <div className="flex items-center justify-between mb-4">
        <h2 id="enforced-limits-title" className="text-sm font-bold flex items-center space-x-2 uppercase tracking-wider text-zinc-200">
          <ShieldAlert className="w-4 h-4 text-amber-500" aria-hidden="true" />
          <span>Enforced limits</span>
        </h2>
        <span className="text-2xs font-mono text-zinc-500">tighten-only</span>
      </div>

      <h3 className="text-2xs font-mono font-bold uppercase tracking-wider text-zinc-400 mb-2">Paper engine (dispatcher)</h3>
      {why ? (
        <p className="text-xs font-mono text-zinc-400 mb-4" role="status" data-testid="enforced-limits-unknown">{why}</p>
      ) : (
        <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1.5 text-xs font-mono mb-1">
          {ROWS.map((r) => (
            <div key={r.key} className="contents">
              <dt className="text-zinc-400">{r.label}</dt>
              <dd className="text-right tabular-nums text-zinc-100">
                <Fresh value={paper?.[r.key] ?? null} asOf={asOf} maxAgeMs={LIMITS_MAX_AGE_MS} format={(v) => `${v} ${r.unit}`} />
              </dd>
            </div>
          ))}
        </dl>
      )}
      {!why && <p className="text-2xs font-mono text-zinc-500 mb-4">Published by the orchestrator{asOf ? ` at ${new Date(asOf).toLocaleString()}` : ""}.</p>}

      <h3 className="text-2xs font-mono font-bold uppercase tracking-wider text-zinc-400 mb-2">Configure (tighten only)</h3>
      <div className="mb-4">
        <RiskLimitsForm />
      </div>

      <h3 className="text-2xs font-mono font-bold uppercase tracking-wider text-zinc-400 mb-2">Mandates</h3>
      {mandateError ? (
        <p className="text-xs font-mono text-zinc-400">unknown (mandates unavailable: {mandateError})</p>
      ) : mandates === null ? (
        <p className="text-xs font-mono text-zinc-500">loading…</p>
      ) : mandates.length === 0 ? (
        <p className="text-xs font-mono text-zinc-500">No mandates: the AI cannot place orders.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs font-mono tabular-nums">
            <thead>
              <tr className="text-zinc-500 text-left">
                <th className="font-normal pb-1">Mandate</th>
                <th className="font-normal pb-1 text-right">Budget</th>
                <th className="font-normal pb-1 text-right">Max position</th>
                <th className="font-normal pb-1 text-right">Max drawdown</th>
                <th className="font-normal pb-1 text-right">Leverage</th>
              </tr>
            </thead>
            <tbody>
              {mandates.map((m) => (
                <tr key={m.id} className="border-t border-white/5 text-zinc-200">
                  <td className="py-1">{m.name} <span className="text-zinc-500">({m.status})</span></td>
                  <td className="py-1 text-right">${Number(m.capital_budget_usd).toLocaleString()}</td>
                  <td className="py-1 text-right">{m.max_position_pct}%</td>
                  <td className="py-1 text-right">{m.max_drawdown_pct}%</td>
                  <td className="py-1 text-right">{m.allow_perps ? `${m.max_leverage}x` : "spot"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-4 text-2xs text-zinc-500">
        Per-mandate limits are edited on the mandate (Mission Control → Mandates).
      </p>
    </section>
  );
}
