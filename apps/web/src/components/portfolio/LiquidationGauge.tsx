import React from "react";
import { fmtPct, fmtPrice } from "@/lib/portfolio/metrics";

/** Distance to liquidation at which the gauge reads fully safe. */
const SAFE_AT = 0.25;

export function liqTone(distance: number): { label: string; text: string } {
  if (distance < 0.02) return { label: "Danger", text: "text-red-400" };
  if (distance < 0.05) return { label: "Warning", text: "text-amber-400" };
  if (distance < 0.1) return { label: "Watch", text: "text-yellow-300" };
  return { label: "Safe", text: "text-emerald-400" };
}

interface LiquidationGaugeProps {
  distance: number | null; // fraction of mark; null = not applicable (spot) or unknown
  liquidationPrice?: number | null;
  isPerp: boolean;
}

/** Horizontal Danger -> Safe track; the marker sits at the current distance to liquidation. */
export const LiquidationGauge = React.memo(function LiquidationGauge({ distance, liquidationPrice, isPerp }: LiquidationGaugeProps) {
  if (!isPerp) return <span className="text-2xs text-zinc-600">Spot · no liquidation</span>;
  if (distance === null) return <span className="text-2xs text-zinc-500">Liq. price pending</span>;

  const pos = Math.min(distance / SAFE_AT, 1) * 100;
  const tone = liqTone(distance);
  return (
    <div className="w-32" title={liquidationPrice ? `Liquidation at ${fmtPrice(liquidationPrice)}` : undefined}>
      <div
        role="meter"
        aria-label="Liquidation proximity"
        aria-valuemin={0}
        aria-valuemax={SAFE_AT * 100}
        aria-valuenow={Math.min(distance, SAFE_AT) * 100}
        aria-valuetext={`${fmtPct(distance)} from liquidation, ${tone.label}`}
        className="relative h-1.5 rounded-full bg-gradient-to-r from-red-600 via-amber-500 to-emerald-500/70"
      >
        <div className="absolute inset-y-0 right-0 rounded-r-full bg-zinc-950/70" style={{ left: `${pos}%` }} />
        <div
          className="absolute top-1/2 h-3 w-0.5 -translate-y-1/2 rounded bg-white shadow-[0_0_4px_rgba(255,255,255,0.6)] transition-[left] duration-500"
          style={{ left: `calc(${pos}% - 1px)` }}
        />
      </div>
      <div className="mt-1 flex justify-between text-2xs leading-none">
        <span className={`font-bold ${tone.text}`}>{fmtPct(distance)}</span>
        <span className="text-zinc-500">{tone.label}</span>
      </div>
    </div>
  );
});
