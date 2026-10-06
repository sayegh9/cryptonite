"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Layers, Minus, Repeat, X } from "lucide-react";
import { usePortfolioStore, type PaperPosition } from "@/lib/stores/usePortfolioStore";
import { usePerpetualStore } from "@/lib/stores/usePerpetualStore";
import { closePosition, reducePosition, reversePosition } from "@/lib/portfolio/actions";
import { fmtPrice, fmtQty, fmtSignedUsd, isHeldPosition, isPerp, liqDistanceOf, markOf, notionalOf } from "@/lib/portfolio/metrics";
import { LiquidationGauge } from "./LiquidationGauge";
import { FreshnessBadge } from "./FreshnessBadge";

const actionBtn =
  "inline-flex items-center gap-1 px-1.5 py-1 rounded border text-2xs font-bold transition-colors outline-none focus-visible:ring-2 disabled:opacity-40 disabled:cursor-not-allowed";

interface RowProps {
  pos: PaperPosition;
  latched: boolean;
  onSelect: (id: string) => void;
}

const PositionRow = React.memo(function PositionRow({ pos, latched, onSelect }: RowProps) {
  const perp = isPerp(pos.instrument_id);
  // Liquidation is mark-based: use the live perp mark tick when the perpetual stream has one
  const liveMark = usePerpetualStore((s) => (perp ? s.marks[pos.instrument_id]?.markPrice : undefined));
  const mark = liveMark ?? markOf(pos);
  const [pending, setPending] = useState<null | "reduce" | "close" | "reverse">(null);
  const [confirmReverse, setConfirmReverse] = useState(false);

  useEffect(() => {
    if (!confirmReverse) return;
    const t = setTimeout(() => setConfirmReverse(false), 4000);
    return () => clearTimeout(t);
  }, [confirmReverse]);

  const run = async (kind: "reduce" | "close" | "reverse", fn: () => Promise<unknown>) => {
    setPending(kind);
    try { await fn(); } finally { setPending(null); }
  };

  const isShort = pos.side === "SHORT";
  const pnl = pos.unrealized_pnl;
  const costBasis = Math.abs(pos.quantity * pos.entry_price);
  const margin = perp && pos.leverage ? costBasis / pos.leverage : costBasis;
  const roe = margin > 0 ? pnl / margin : 0;

  return (
    <tr className="group hover:bg-zinc-800/30 cursor-pointer" onClick={() => onSelect(pos.position_id)}>
      <td className="py-2 pr-2">
        <div className="font-bold text-white">{pos.instrument_id}</div>
        <div className="mt-0.5 flex items-center gap-1 text-2xs uppercase tracking-wider">
          <span className={perp ? "text-violet-300" : "text-zinc-500"}>{perp ? "Perp" : "Spot"}</span>
          {perp && pos.leverage ? <span className="text-zinc-500">· {pos.leverage}x {pos.margin_mode ?? "CROSS"}</span> : null}
        </div>
      </td>
      <td className="pr-2">
        <span
          className={`inline-block min-w-[44px] text-center px-1.5 py-0.5 rounded text-2xs font-black tracking-wider border ${
            isShort ? "bg-red-500/15 text-red-300 border-red-500/40" : "bg-emerald-500/15 text-emerald-300 border-emerald-500/40"
          }`}
        >
          {pos.side}
        </span>
      </td>
      <td className="pr-2">
        <span className="px-1.5 py-0.5 rounded text-2xs border bg-indigo-500/15 text-indigo-300 border-indigo-500/30" title={pos.instance_id}>
          {pos.instance_id.length > 15 ? `${pos.instance_id.substring(0, 15)}…` : pos.instance_id}
        </span>
      </td>
      <td className="pr-2 text-right tabular-nums">{fmtQty(pos.quantity)}</td>
      <td className="pr-2 text-right tabular-nums text-zinc-400">{fmtPrice(pos.entry_price)}</td>
      <td className="pr-2 text-right tabular-nums">{fmtPrice(mark)}</td>
      <td className={`pr-2 text-right tabular-nums ${pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
        <span className="font-black text-[13px]">{fmtSignedUsd(pnl)}</span>
        <span className="block text-2xs opacity-75">{`${roe >= 0 ? "+" : ""}${(roe * 100).toFixed(2)}%`}</span>
      </td>
      <td className="pr-2">
        <LiquidationGauge isPerp={perp} distance={perp ? liqDistanceOf(pos, mark) : null} liquidationPrice={pos.liquidation_price} />
      </td>
      <td className="text-right whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
        <div className="inline-flex gap-1">
          <button
            type="button"
            disabled={pending !== null}
            onClick={() => run("reduce", () => reducePosition(pos, 0.5))}
            className={`${actionBtn} bg-zinc-900 border-zinc-700 text-zinc-300 hover:bg-zinc-800 focus-visible:ring-zinc-500/50`}
            title="Reduce by 50% with a market order"
            aria-label={`Reduce ${pos.instrument_id} by half`}
          >
            <Minus className="w-3 h-3" />50%
          </button>
          <button
            type="button"
            disabled={pending !== null}
            onClick={() => run("close", () => closePosition(pos.instance_id, pos.instrument_id))}
            className={`${actionBtn} bg-red-950 border-red-800 text-red-300 hover:bg-red-900 focus-visible:ring-red-500/50`}
            title="Close the full position at market"
          >
            <X className="w-3 h-3" />
            {pending === "close" ? "Closing…" : "Close"}
          </button>
          <button
            type="button"
            disabled={!perp || latched || pending !== null}
            onClick={() => {
              if (!confirmReverse) return setConfirmReverse(true);
              setConfirmReverse(false);
              void run("reverse", () => reversePosition(pos));
            }}
            className={`${actionBtn} ${
              confirmReverse
                ? "bg-amber-500/20 border-amber-500 text-amber-200"
                : "bg-zinc-900 border-zinc-700 text-zinc-300 hover:bg-zinc-800"
            } focus-visible:ring-amber-500/50`}
            title={!perp ? "Spot positions are long-only" : latched ? "Kill switch latched: new exposure blocked" : "Flip to the opposite side at the same size and leverage"}
            aria-label={confirmReverse ? `Confirm reverse ${pos.instrument_id}` : `Reverse ${pos.instrument_id}`}
          >
            <Repeat className="w-3 h-3" />
            {confirmReverse ? "Confirm?" : "Rev"}
          </button>
        </div>
      </td>
    </tr>
  );
});

export function ActivePositionsTable({ onSelect }: { onSelect: (positionId: string) => void }) {
  const positions = usePortfolioStore((s) => s.positions);
  const loaded = usePortfolioStore((s) => s.loaded);
  const latched = usePortfolioStore((s) => s.killSwitch.is_latched);
  const open = useMemo(
    () => positions.filter(isHeldPosition).sort((a, b) => notionalOf(b) - notionalOf(a) || a.position_id.localeCompare(b.position_id)),
    [positions],
  );
  const totalPnl = open.reduce((acc, p) => acc + p.unrealized_pnl, 0);

  return (
    <section className="bg-zinc-900/50 border border-zinc-800 rounded flex flex-col min-w-0" aria-labelledby="active-positions-title">
      <div className="p-3 border-b border-zinc-800 flex items-center justify-between gap-2 font-bold text-xs uppercase tracking-wider text-zinc-300">
        <span className="flex items-center gap-2">
          <Layers className="w-4 h-4 text-cyan-400" />
          <span id="active-positions-title">Active Positions</span>
          {open.length > 0 && (
            <span className={`normal-case tracking-normal text-2xs font-black tabular-nums ${totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
              {fmtSignedUsd(totalPnl)} uPnL
            </span>
          )}
        </span>
        <FreshnessBadge />
      </div>
      <div className="p-3 overflow-x-auto">
        {!loaded ? (
          <div className="space-y-2" aria-busy="true" aria-label="Loading positions">
            {[0, 1, 2].map((i) => <div key={i} className="h-8 rounded bg-zinc-800/40 animate-pulse" />)}
          </div>
        ) : open.length === 0 ? (
          <div className="text-center py-8 text-zinc-500 italic">No open positions.</div>
        ) : (
          <table className="w-full text-left">
            <thead>
              <tr className="text-zinc-500 border-b border-zinc-800 text-2xs uppercase tracking-wider">
                <th className="py-1 font-bold">Instrument</th>
                <th className="font-bold">Side</th>
                <th className="font-bold">Origin</th>
                <th className="font-bold text-right pr-2">Size</th>
                <th className="font-bold text-right pr-2">Entry</th>
                <th className="font-bold text-right pr-2">Mark</th>
                <th className="font-bold text-right pr-2">uPnL / ROE</th>
                <th className="font-bold">Liq. Proximity</th>
                <th className="font-bold text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/50">
              {open.map((pos) => (
                <PositionRow key={pos.position_id} pos={pos} latched={latched} onSelect={onSelect} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
