"use client";

import React, { useMemo, useState } from "react";
import { PieChart } from "lucide-react";
import { usePortfolioStore } from "@/lib/stores/usePortfolioStore";
import { exposureBy, fmtCompactUsd, fmtPct } from "@/lib/portfolio/metrics";

/** Single-asset concentration cap enforced by the risk engine. */
const CONCENTRATION_CAP = 0.25;
const MAX_SLICES = 6;
const PALETTE = ["#22d3ee", "#a78bfa", "#fbbf24", "#34d399", "#f472b6", "#60a5fa"];
const OTHER_COLOR = "#52525b";

const R = 38;
const C = 2 * Math.PI * R;

export function ExposureBreakdown() {
  const positions = usePortfolioStore((s) => s.positions);
  const equity = usePortfolioStore((s) => s.account?.total_equity ?? 0);
  const [by, setBy] = useState<"asset" | "strategy">("asset");

  const slices = useMemo(() => {
    const all = exposureBy(positions.filter((p) => p.status === "OPEN"), by);
    if (all.length <= MAX_SLICES) return all.map((s, i) => ({ ...s, color: PALETTE[i] }));
    const head = all.slice(0, MAX_SLICES - 1).map((s, i) => ({ ...s, color: PALETTE[i] }));
    const rest = all.slice(MAX_SLICES - 1);
    return [...head, {
      key: `Other (${rest.length})`,
      gross: rest.reduce((a, s) => a + s.gross, 0),
      net: rest.reduce((a, s) => a + s.net, 0),
      color: OTHER_COLOR,
    }];
  }, [positions, by]);

  const gross = slices.reduce((a, s) => a + s.gross, 0);
  const net = slices.reduce((a, s) => a + s.net, 0);
  let offset = 0;

  return (
    <section className="p-4 bg-zinc-900/50 border border-zinc-800 rounded min-w-0" aria-labelledby="exposure-title">
      <div className="flex justify-between items-center mb-3">
        <h2 id="exposure-title" className="flex items-center gap-2 text-zinc-400 text-xs font-bold uppercase tracking-wider">
          <PieChart className="w-4 h-4 text-violet-400" /> Capital Exposure
        </h2>
        <div className="flex rounded border border-zinc-700 overflow-hidden text-2xs" role="group" aria-label="Group exposure by">
          {(["asset", "strategy"] as const).map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={by === k}
              onClick={() => setBy(k)}
              className={`px-2 py-0.5 capitalize ${by === k ? "bg-zinc-700 text-white" : "text-zinc-400 hover:text-zinc-200"}`}
            >
              {k}
            </button>
          ))}
        </div>
      </div>

      {gross === 0 ? (
        <div className="flex items-center gap-4">
          <svg width={96} height={96} viewBox="0 0 100 100" aria-hidden>
            <circle cx={50} cy={50} r={R} fill="none" stroke="#27272a" strokeWidth={12} />
          </svg>
          <p className="text-zinc-500 italic">Fully in cash. No market exposure.</p>
        </div>
      ) : (
        <div className="flex items-center gap-4">
          <svg width={96} height={96} viewBox="0 0 100 100" role="img" aria-label={`Gross exposure ${fmtCompactUsd(gross)} across ${slices.length} ${by === "asset" ? "assets" : "strategies"}`} className="shrink-0 -rotate-90">
            <circle cx={50} cy={50} r={R} fill="none" stroke="#27272a" strokeWidth={12} />
            {slices.map((s) => {
              const len = (s.gross / gross) * C;
              const el = (
                <circle
                  key={s.key}
                  cx={50} cy={50} r={R} fill="none" stroke={s.color} strokeWidth={12}
                  strokeDasharray={`${Math.max(len - 1, 0.5)} ${C}`} strokeDashoffset={-offset}
                  className="transition-[stroke-dasharray,stroke-dashoffset] duration-500"
                >
                  <title>{`${s.key}: ${fmtCompactUsd(s.gross)}`}</title>
                </circle>
              );
              offset += len;
              return el;
            })}
          </svg>
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-3 mb-2 tabular-nums">
              <div>
                <div className="text-2xs uppercase tracking-wider text-zinc-500">Gross</div>
                <div className="text-sm font-black text-white">{fmtCompactUsd(gross)}</div>
              </div>
              <div>
                <div className="text-2xs uppercase tracking-wider text-zinc-500">Net</div>
                <div className={`text-sm font-black ${net >= 0 ? "text-emerald-400" : "text-red-400"}`}>{net >= 0 ? "" : "-"}{fmtCompactUsd(Math.abs(net))}</div>
              </div>
              <div>
                <div className="text-2xs uppercase tracking-wider text-zinc-500">Gross Lev.</div>
                <div className="text-sm font-black text-zinc-200">{equity > 0 ? `${(gross / equity).toFixed(2)}x` : "—"}</div>
              </div>
            </div>
            <ul className="space-y-1">
              {slices.map((s) => {
                const share = equity > 0 ? s.gross / equity : 0;
                const flag = by === "asset" && share >= CONCENTRATION_CAP ? "text-red-400" : by === "asset" && share >= 0.2 ? "text-amber-400" : "text-zinc-400";
                return (
                  <li key={s.key} className="flex items-center gap-2 text-2xs tabular-nums">
                    <span className="h-2 w-2 rounded-sm shrink-0" style={{ background: s.color }} aria-hidden />
                    <span className="truncate text-zinc-200 font-bold">{s.key}</span>
                    {s.net < 0 && <span className="text-red-300 text-2xs">net short</span>}
                    <span className="ml-auto text-zinc-500">{fmtCompactUsd(s.gross)}</span>
                    <span className={`w-12 text-right font-bold ${flag}`} title={by === "asset" ? "Share of equity; 25% single-asset cap" : "Share of equity"}>
                      {fmtPct(share)}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}
      <p className="mt-2 text-2xs text-zinc-500">Max 25% Single-Asset Concentration · % of equity</p>
    </section>
  );
}
