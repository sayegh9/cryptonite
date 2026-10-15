"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, Gauge, Timer, X } from "lucide-react";
import { usePerpetualStore } from "@/lib/stores/usePerpetualStore";

type RiskLevel = "safe" | "caution" | "danger";

/** Liquidation distance bands: > 20% safe, 5–20% caution, < 5% danger. */
export function liquidationRiskLevel(distance: number): RiskLevel {
  if (distance > 0.2) return "safe";
  if (distance >= 0.05) return "caution";
  return "danger";
}

const RISK_STYLES: Record<RiskLevel, { bar: string; text: string; label: string }> = {
  safe: { bar: "bg-emerald-500", text: "text-emerald-400", label: "SAFE" },
  caution: { bar: "bg-amber-500", text: "text-amber-400", label: "CAUTION" },
  danger: { bar: "bg-rose-500 motion-safe:animate-pulse", text: "text-rose-400", label: "DANGER" },
};

const METER_RANGE = 0.3; // the bar spans 0–30% distance; anything further is shown full

function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return [h, m, s].map((v) => String(v).padStart(2, "0")).join(":");
}

const fmt = (v: number, digits = 2) =>
  Number.isFinite(v) ? v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";

export function MarginRiskHUD({ instrumentId }: { instrumentId: string }) {
  const position = usePerpetualStore((s) => s.positions[instrumentId]);
  const mark = usePerpetualStore((s) => s.marks[instrumentId]);
  const alert = usePerpetualStore((s) => s.liquidationAlert);
  const dismissAlert = usePerpetualStore((s) => s.dismissLiquidationAlert);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const distance = position?.liquidationDistance ?? null;
  const risk = distance !== null ? liquidationRiskLevel(distance) : null;
  const ratioPct = position?.marginRatio != null && Number.isFinite(position.marginRatio) ? position.marginRatio * 100 : null;
  const ratioColor = ratioPct === null ? "text-zinc-400" : ratioPct >= 80 ? "text-rose-400" : ratioPct >= 50 ? "text-amber-400" : "text-emerald-400";
  const fundingPct = mark ? mark.fundingRate * 100 : null;

  return (
    <section className="bg-zinc-950 border border-white/10 rounded p-3 space-y-3 font-mono" aria-label="Margin risk">
      <header className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-zinc-300">
          <Gauge className="w-3 h-3 text-cyan-400" aria-hidden />
          <span>Margin Risk · {instrumentId.replace(".BINANCE", "")}</span>
        </div>
        {mark && <span className="text-2xs text-zinc-400">Mark {fmt(mark.markPrice)}</span>}
      </header>

      {alert && (
        <div role="alert" className="flex items-start gap-2 bg-rose-950/50 border border-rose-800/60 rounded p-2 text-2xs text-rose-300">
          <AlertTriangle className="w-4 h-4 flex-none mt-0.5" aria-hidden />
          <div className="flex-1">
            <p className="font-bold">LIQUIDATED {alert.instrumentId.replace(".BINANCE", "")} {alert.side}</p>
            <p>{alert.quantity} @ {fmt(alert.fillPrice)} · PnL {fmt(alert.realizedPnl)} · fees {fmt(alert.fees)}</p>
          </div>
          <button onClick={dismissAlert} className="text-rose-400 hover:text-rose-200" aria-label="Dismiss liquidation alert">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {position ? (
        <>
          <div className="flex items-center justify-between text-2xs">
            <span className={position.side === "LONG" ? "text-emerald-400 font-bold" : "text-rose-400 font-bold"}>
              {position.side} {position.size}
            </span>
            <span className="text-zinc-400">
              {position.leverage}x · {position.marginMode} · entry {fmt(position.entryPrice)}
            </span>
          </div>
          <div className="flex items-center justify-between text-2xs">
            <span className="text-zinc-500">Unrealized PnL</span>
            <span className={position.unrealizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{fmt(position.unrealizedPnl)} USDT</span>
          </div>

          <div>
            <div className="flex items-center justify-between text-2xs uppercase tracking-wider mb-1">
              <span className="text-zinc-500">Liquidation distance</span>
              {risk && distance !== null && (
                <span className={`${RISK_STYLES[risk].text} font-bold`}>
                  {RISK_STYLES[risk].label} · {(distance * 100).toFixed(2)}%
                </span>
              )}
            </div>
            <div
              role="meter"
              aria-label="Distance from mark price to liquidation price"
              aria-valuemin={0}
              aria-valuemax={METER_RANGE * 100}
              aria-valuenow={distance !== null ? Math.min(distance, METER_RANGE) * 100 : undefined}
              aria-valuetext={distance !== null && risk ? `${(distance * 100).toFixed(2)}% (${RISK_STYLES[risk].label})` : "unknown"}
              className="h-2 w-full bg-zinc-800 rounded overflow-hidden"
            >
              {risk && distance !== null && (
                <div className={`h-full ${RISK_STYLES[risk].bar}`} style={{ width: `${Math.min(distance / METER_RANGE, 1) * 100}%` }} />
              )}
            </div>
            <p className="text-2xs text-zinc-500 mt-1">
              Liq. price {position.liquidationPrice !== null ? fmt(position.liquidationPrice) : "— (no bracket data)"}
            </p>
          </div>

          <div className="flex items-center justify-between text-2xs">
            <span className="text-zinc-500">Margin ratio (MM / balance)</span>
            <span className={`font-bold ${ratioColor}`}>{ratioPct !== null ? `${ratioPct.toFixed(2)}%` : "—"}</span>
          </div>
        </>
      ) : (
        <p className="text-2xs text-zinc-500">No open position on this perpetual.</p>
      )}

      <div className="flex items-center justify-between text-2xs border-t border-white/5 pt-2">
        <span className="flex items-center gap-1 text-zinc-500">
          <Timer className="w-3 h-3" aria-hidden /> Funding
        </span>
        {mark && fundingPct !== null && Number.isFinite(fundingPct) ? (
          <span className="text-zinc-300">
            <span className={fundingPct >= 0 ? "text-rose-300" : "text-emerald-300"}>
              {fundingPct >= 0 ? "+" : ""}{fundingPct.toFixed(4)}%
            </span>
            <span className="text-zinc-500"> ({fundingPct >= 0 ? "longs pay" : "shorts pay"}) · </span>
            <span role="timer" aria-label="Time to next funding">{formatCountdown(mark.nextFundingTime - now)}</span>
          </span>
        ) : (
          <span className="text-zinc-500">awaiting mark feed</span>
        )}
      </div>
    </section>
  );
}
