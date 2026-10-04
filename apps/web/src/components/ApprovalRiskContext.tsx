"use client";

import { useEffect, useState } from "react";
import { Fresh } from "@/components/Fresh";
import { metricOrNull, UNKNOWN_TEXT } from "@/lib/freshness";

/** One past decision on the same instrument and side, with its outcome (approvals API `similar_decisions`). */
export interface SimilarDecision {
  created_at?: string | null;
  side?: string | null;
  recommendation?: string | null;
  /** Signed outcome as a fraction (0.012 = +1.2%) or null while unlabelled */
  return_pct?: number | string | null;
  outcome?: string | null;
}

export interface ApprovalRiskInput {
  /** Symbol without venue, e.g. BTCUSDT */
  symbol: string | null;
  side: "BUY" | "SELL" | null;
  /** Price the decision was made at */
  decisionPrice: number | null;
  /** Order notional in USD when known */
  notionalUsd: number | null;
  /** Stop distance as a fraction of entry (0.02 = 2%) when the proposal carries one */
  stopPct: number | null;
  /** Mandate budget room left in USD, when known */
  roomUsd: number | null;
  /** Requested size in % of equity (shown in the resize note) */
  requestedPct: number | null;
  similar?: SimilarDecision[] | null;
}

const QUOTE_MAX_AGE_MS = 15_000;
const usd = (v: number) => `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

/** Live quote for a symbol from /api/v1/market/prices, polled; null price while unknown. */
function useLiveQuote(symbol: string | null) {
  const [quote, setQuote] = useState<{ price: number | null; at: number | null; error: boolean }>({ price: null, at: null, error: false });
  useEffect(() => {
    if (!symbol) return;
    let active = true;
    setQuote({ price: null, at: null, error: false });
    const load = async () => {
      try {
        const res = await fetch(`/api/v1/market/prices?symbols=${encodeURIComponent(symbol)}`);
        if (!res.ok) throw new Error(String(res.status));
        const data = await res.json();
        const row = Array.isArray(data) ? data.find((r: { symbol?: string }) => r.symbol === symbol) : data?.[symbol];
        const p = metricOrNull(row?.last_price ?? row?.price);
        if (active) setQuote(p !== null && p > 0 ? { price: p, at: Date.now(), error: false } : (q) => ({ ...q, error: true }));
      } catch {
        if (active) setQuote((q) => ({ ...q, error: true }));
      }
    };
    void load();
    const t = setInterval(load, 10_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [symbol]);
  return quote;
}

/**
 * Context an operator needs before approving (R6.4): live quote and slippage vs the decision price,
 * mandate room, worst-case loss at the stop, a resize note and the last decisions on the same
 * instrument and side. Anything unknown shows "—"; nothing here blocks the approval.
 */
export function ApprovalRiskContext(props: ApprovalRiskInput) {
  const { symbol, side, decisionPrice, notionalUsd, stopPct, roomUsd, requestedPct, similar } = props;
  const quote = useLiveQuote(symbol);

  // Slippage in bps, signed so that positive = worse for the operator (paying up on a BUY)
  const slipBps =
    quote.price !== null && decisionPrice !== null && decisionPrice > 0 && side
      ? ((quote.price - decisionPrice) / decisionPrice) * 10_000 * (side === "BUY" ? 1 : -1)
      : null;
  const worstLoss = notionalUsd !== null && stopPct !== null ? notionalUsd * stopPct : null;
  const resized = roomUsd !== null && notionalUsd !== null && notionalUsd > roomUsd;

  return (
    <div className="mt-2 rounded border border-zinc-800 bg-zinc-950/60 p-2 font-mono text-2xs" data-testid="approval-context">
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 tabular-nums">
        <dt className="text-zinc-500">Live quote</dt>
        <dd className="text-right text-zinc-200" data-testid="approval-quote">
          <Fresh value={quote.price} asOf={quote.at} maxAgeMs={QUOTE_MAX_AGE_MS} format={usd} unknownText={quote.error ? "unavailable" : UNKNOWN_TEXT} />
        </dd>
        <dt className="text-zinc-500">Decision price</dt>
        <dd className="text-right text-zinc-200">{decisionPrice !== null ? usd(decisionPrice) : UNKNOWN_TEXT}</dd>
        <dt className="text-zinc-500">Slippage since decision</dt>
        <dd className={`text-right ${slipBps === null ? "text-zinc-400" : slipBps > 0 ? "text-amber-300" : "text-emerald-300"}`} data-testid="approval-slippage">
          {slipBps === null ? UNKNOWN_TEXT : `${slipBps > 0 ? "+" : ""}${slipBps.toFixed(1)} bps ${slipBps > 0 ? "against" : "in favour"}`}
        </dd>
        <dt className="text-zinc-500">Mandate room left</dt>
        <dd className="text-right text-zinc-200" data-testid="approval-room">{roomUsd !== null ? usd(roomUsd) : UNKNOWN_TEXT}</dd>
        <dt className="text-zinc-500">Worst-case loss at stop</dt>
        <dd className="text-right text-rose-300" data-testid="approval-worst-loss">
          {worstLoss !== null ? `-${usd(worstLoss)}` : stopPct === null ? `${UNKNOWN_TEXT} (no stop in proposal)` : UNKNOWN_TEXT}
        </dd>
      </dl>
      <p className="mt-1 text-2xs text-zinc-500" data-testid="approval-resize-note">
        {resized
          ? `Larger than the mandate's room: it will be resized to fit ${usd(roomUsd!)}.`
          : `May be resized to ${requestedPct !== null ? `less than ${requestedPct}%` : "a smaller size"} to fit the mandate's room and the 1–25% clamp.`}
      </p>
      {similar && similar.length > 0 && (
        <div className="mt-1 border-t border-zinc-800 pt-1" data-testid="approval-similar">
          <div className="text-2xs uppercase tracking-wider text-zinc-500">Last decisions, same instrument and side</div>
          <ul>
            {similar.slice(0, 3).map((d, i) => {
              const r = metricOrNull(d.return_pct);
              return (
                <li key={i} className="flex justify-between gap-2">
                  <span className="text-zinc-400">{d.created_at ? new Date(d.created_at).toISOString().slice(0, 10) : UNKNOWN_TEXT} · {d.recommendation ?? d.side ?? ""}</span>
                  <span className={r === null ? "text-zinc-500" : r >= 0 ? "text-emerald-300" : "text-rose-300"}>
                    {r === null ? d.outcome ?? "not labelled" : `${r >= 0 ? "+" : ""}${(r * 100).toFixed(2)}%`}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

/** "BUY"/"SELL" from a recommendation or side string, else null. */
export function sideOf(v: unknown): "BUY" | "SELL" | null {
  const s = String(v ?? "").toUpperCase();
  if (["BUY", "LONG", "OVERWEIGHT", "STRONG_BUY", "ADD"].some((k) => s.includes(k))) return "BUY";
  if (["SELL", "SHORT", "UNDERWEIGHT", "EXIT", "REDUCE"].some((k) => s.includes(k))) return "SELL";
  return null;
}
