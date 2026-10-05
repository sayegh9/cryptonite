"use client";

import { REGIME_LABEL, sortRegimes } from "@/lib/backtestMetrics";
import type { OutcomesByRegime as Data } from "@/lib/mission/types";

const pct = (v: number | null | undefined, digits = 2) =>
  v == null || !Number.isFinite(Number(v)) ? "—" : `${Number(v) >= 0 ? "+" : ""}${(Number(v) * 100).toFixed(digits)}%`;

/**
 * Outcomes by regime (F1, reporting only): labelled decisions of the open mandate per market regime at
 * decision time, scored WIN / LOSS / NEUTRAL after 24h, with the mean signed return. "—" when absent.
 */
export default function OutcomesByRegime({ data }: { data: Data | null }) {
  const rows = data ? sortRegimes(data.regimes.map((r) => [r.regime, r] as [string, typeof r])) : [];
  return (
    <div data-testid="outcomes-by-regime">
      <h3 className="mb-1 text-2xs font-bold uppercase tracking-wider text-zinc-400">
        Outcomes by regime{data ? ` (${data.horizon})` : ""}
      </h3>
      {rows.length === 0 ? (
        <p className="text-zinc-500">—</p>
      ) : (
        <table className="w-full text-left tabular-nums" aria-label="Outcomes by regime">
          <thead className="text-2xs text-zinc-500">
            <tr>
              <th className="py-1 font-normal">Regime</th>
              <th className="py-1 font-normal">n</th>
              <th className="py-1 font-normal">Wins</th>
              <th className="py-1 font-normal">Losses</th>
              <th className="py-1 font-normal">Neutral</th>
              <th className="py-1 font-normal">Mean signed return</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([regime, r]) => (
              <tr key={regime} className="border-t border-zinc-900" data-testid={`regime-outcome-${regime}`}>
                <td className="py-1">{REGIME_LABEL[regime] ?? regime}</td>
                <td className="py-1">{r.n ?? "—"}</td>
                <td className="py-1 text-emerald-300">{r.wins ?? "—"}</td>
                <td className="py-1 text-red-300">{r.losses ?? "—"}</td>
                <td className="py-1 text-zinc-400">{r.neutral ?? "—"}</td>
                <td className="py-1">{pct(r.mean_signed_return)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="mt-1 text-2xs text-zinc-500">Regime at decision time from 1h ATR% and ADX. Reporting only: no effect on trading.</p>
    </div>
  );
}
