"use client";

import { signedUsd } from "@/lib/autotrade/copy";
import { staleMarkText } from "@/lib/mission/marks";
import type { Profile } from "@/lib/autotrade/types";
import { btnSecondary } from "../Dialogs";

const marketOf = (instrument: string) => {
  const local = instrument.split(".")[0];
  return local.endsWith("-PERP") ? "Perpetuals" : local.endsWith("-MARGIN") ? "Spot Margin" : "Spot";
};

/** The profile's open positions from the mandate view; borrow and liquidation details only as the API returns them. */
export function PositionsTab({ profile: p, status, onRetry }: { profile: Profile; status: string; onRetry: () => void }) {
  const positions = p.positions ?? [];
  if (status === "error" && positions.length === 0)
    return (
      <div role="alert" className="space-y-2 text-sm">
        <p className="font-bold text-amber-200">Positions unavailable.</p>
        <button type="button" className={btnSecondary} onClick={onRetry}>
          Retry
        </button>
      </div>
    );
  if (positions.length === 0) return <p className="text-sm text-zinc-400">No open positions.</p>;
  return (
    <div className="space-y-2">
      {p.status === "STOPPING" && <p className="text-xs text-amber-200">Stopping: these positions run on their exits (or close, if you chose to close them).</p>}
      {p.marks_stale && <p className="text-xs text-amber-300">{staleMarkText(p.marks_age_s)}</p>}
      <ul className="space-y-2">
        {positions.map((pos) => {
          const market = marketOf(pos.instrument_id);
          return (
            <li key={`${pos.instrument_id}:${pos.side}`} className="rounded-lg border border-zinc-800 p-3 text-xs">
              <p className="flex flex-wrap gap-x-3 font-semibold text-zinc-100">
                <span>{pos.instrument_id.split(".")[0]}</span>
                <span>{pos.side}</span>
                <span className="font-normal text-zinc-400">{market}</span>
              </p>
              <dl className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 sm:grid-cols-4">
                <dt className="text-zinc-500">Quantity</dt>
                <dd className="text-zinc-200">{pos.quantity}</dd>
                <dt className="text-zinc-500">Entry / mark</dt>
                <dd className="text-zinc-200">
                  {pos.entry_price} / {pos.current_price}
                  {pos.mark_stale ? " (stale)" : ""}
                </dd>
                <dt className="text-zinc-500">Unrealized P&amp;L (USD)</dt>
                <dd className={Number(pos.unrealized_pnl) < 0 ? "text-red-300" : "text-emerald-300"}>{signedUsd(pos.unrealized_pnl)}</dd>
                {market === "Perpetuals" && (
                  <>
                    <dt className="text-zinc-500">Leverage</dt>
                    <dd className="text-zinc-200">{pos.leverage ? `${pos.leverage}× ${pos.margin_mode ?? ""}` : "Unavailable"}</dd>
                    <dt className="text-zinc-500">Liquidation</dt>
                    <dd className="text-zinc-200">
                      {pos.liquidation_price
                        ? `${pos.liquidation_price}${pos.liquidation_distance_pct != null ? ` (${pos.liquidation_distance_pct}% away)` : ""}`
                        : "Unavailable"}
                    </dd>
                  </>
                )}
                {market === "Spot Margin" && (
                  <>
                    <dt className="text-zinc-500">Borrow</dt>
                    <dd className="text-zinc-400">Borrow details unavailable</dd>
                  </>
                )}
              </dl>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
