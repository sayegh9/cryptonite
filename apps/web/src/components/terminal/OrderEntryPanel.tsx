"use client";

import { useId, useState } from "react";
import { ArrowDownRight, ArrowUpRight, Info } from "lucide-react";
import { fetchApi } from "@/lib/api";
import { usePerpetualStore } from "@/lib/stores/usePerpetualStore";
import {
  MAX_PERP_LEVERAGE,
  hasBrackets,
  initialMargin,
  liquidationPrice,
  toPerpInstrumentId,
  type PerpSide,
} from "@/lib/perp/margin";

type Market = "SPOT" | "PERP";
type Side = "BUY" | "SELL";
const LEVERAGE_CHIPS = [1, 5, 10, 20];

const fmt = (v: number | null, digits = 2) =>
  v !== null && Number.isFinite(v)
    ? v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : "—";

/** Resulting signed size and entry price if the order fills at ``price`` (flip = close then open). */
function projectPosition(current: { signed: number; entry: number } | null, orderSigned: number, price: number) {
  const q0 = current?.signed ?? 0;
  const q = q0 + orderSigned;
  if (q === 0) return null;
  if (q0 === 0 || Math.sign(q0) === Math.sign(orderSigned)) {
    const entry = q0 === 0 ? price : (Math.abs(q0) * (current?.entry ?? price) + Math.abs(orderSigned) * price) / Math.abs(q);
    return { signed: q, entry };
  }
  // Opposite direction: a partial reduce keeps the entry; a flip opens the remainder at price
  return { signed: q, entry: Math.sign(q) === Math.sign(q0) ? (current?.entry ?? price) : price };
}

export function OrderEntryPanel({ symbol = "BTCUSDT", onOrderSubmitted }: { symbol?: string; onOrderSubmitted?: () => void }) {
  const perpInstrument = toPerpInstrumentId(symbol);
  const perpAvailable = hasBrackets(perpInstrument);
  const [market, setMarket] = useState<Market>("SPOT");
  const [side, setSide] = useState<Side>("BUY");
  const [orderType, setOrderType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [quantity, setQuantity] = useState("0.01");
  const [price, setPrice] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const reduceOnlyHelpId = useId();

  const leverage = usePerpetualStore((s) => s.leverage);
  const marginMode = usePerpetualStore((s) => s.marginMode);
  const reduceOnly = usePerpetualStore((s) => s.reduceOnly);
  const setLeverage = usePerpetualStore((s) => s.setLeverage);
  const setMarginMode = usePerpetualStore((s) => s.setMarginMode);
  const setReduceOnly = usePerpetualStore((s) => s.setReduceOnly);
  const position = usePerpetualStore((s) => s.positions[perpInstrument]);
  const mark = usePerpetualStore((s) => s.marks[perpInstrument]);
  const crossBalance = usePerpetualStore((s) => s.crossMarginBalance);

  const isPerp = market === "PERP" && perpAvailable;
  const instrumentId = isPerp ? perpInstrument : `${symbol.toUpperCase().replace(".BINANCE", "")}.BINANCE`;
  const qty = parseFloat(quantity);
  const limitPx = parseFloat(price);
  const refPrice = orderType === "LIMIT" && Number.isFinite(limitPx) ? limitPx : mark?.markPrice ?? NaN;
  const effectiveMode = position?.marginMode ?? marginMode; // margin mode is locked while a position is open

  // Reduce-only validation mirrors the dispatcher's REDUCE_ONLY_* rejections
  let reduceOnlyError = "";
  if (isPerp && reduceOnly) {
    const closeSide: Side | null = position ? (position.side === "LONG" ? "SELL" : "BUY") : null;
    if (!position) reduceOnlyError = "No open position to reduce.";
    else if (side !== closeSide) reduceOnlyError = `Reduce-only must ${closeSide} to reduce the ${position.side} position.`;
    else if (qty > position.size) reduceOnlyError = `Quantity exceeds the open size (${position.size}).`;
  }

  // Pre-flight summary (estimates; the dispatcher is authoritative)
  const notional = Number.isFinite(qty) && Number.isFinite(refPrice) ? qty * refPrice : null;
  const requiredMargin = notional === null ? null : reduceOnly ? 0 : initialMargin(notional, leverage);
  let projectedLiq: number | null = null;
  if (isPerp && notional !== null && !reduceOnly) {
    const current = position ? { signed: position.side === "LONG" ? position.size : -position.size, entry: position.entryPrice } : null;
    const projected = projectPosition(current, side === "BUY" ? qty : -qty, refPrice);
    if (projected) {
      const size = Math.abs(projected.signed);
      const posSide: PerpSide = projected.signed > 0 ? "LONG" : "SHORT";
      const wallet = effectiveMode === "ISOLATED" ? initialMargin(size * projected.entry, leverage) : crossBalance;
      projectedLiq = wallet !== null ? liquidationPrice(perpInstrument, posSide, size, projected.entry, wallet) : null;
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (reduceOnlyError) return;
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const body: Record<string, unknown> = {
        instrument_id: instrumentId,
        side,
        order_type: orderType,
        quantity: qty,
        price: orderType === "LIMIT" ? limitPx : null,
      };
      if (isPerp) {
        body.leverage = leverage;
        body.margin_mode = effectiveMode;
        body.reduce_only = reduceOnly;
      }
      const res = await fetchApi("/paper/orders", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify(body),
      });
      setNotice(`Order queued${res?.command_id ? ` (${String(res.command_id).slice(0, 8)})` : ""}`);
      onOrderSubmitted?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to submit order");
    } finally {
      setLoading(false);
    }
  };

  const toggleBtn = (active: boolean) =>
    `flex-1 py-1 text-xs font-bold rounded ${active ? "bg-zinc-700 text-white" : "text-zinc-500 hover:text-zinc-300"}`;

  return (
    <form onSubmit={handleSubmit} className="bg-zinc-950 border border-white/10 rounded p-3 space-y-3" aria-label="Order entry">
      <div className="flex bg-zinc-900 rounded p-1 border border-white/5" role="group" aria-label="Market">
        <button type="button" onClick={() => setMarket("SPOT")} className={toggleBtn(market === "SPOT")} aria-pressed={market === "SPOT"}>
          SPOT
        </button>
        <button
          type="button"
          onClick={() => setMarket("PERP")}
          disabled={!perpAvailable}
          title={perpAvailable ? undefined : `No perpetual margin tiers for ${symbol} yet`}
          className={`${toggleBtn(market === "PERP")} disabled:opacity-40 disabled:cursor-not-allowed`}
          aria-pressed={market === "PERP"}
        >
          PERP
        </button>
      </div>

      <p aria-label="Order instrument" className="text-2xs font-mono font-bold text-zinc-300">{instrumentId}</p>

      {error && <div className="bg-rose-950/40 border border-rose-900/50 text-rose-400 text-2xs font-mono p-2 rounded" role="alert">{error}</div>}
      {notice && <div className="bg-emerald-950/40 border border-emerald-900/50 text-emerald-400 text-2xs font-mono p-2 rounded" role="status">{notice}</div>}

      <div className="flex space-x-2">
        {(["BUY", "SELL"] as const).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSide(s)}
            aria-pressed={side === s}
            className={`flex-1 flex justify-center items-center py-2 rounded font-black text-sm tracking-wider transition-colors ${
              side === s
                ? s === "BUY" ? "bg-emerald-600 text-white" : "bg-rose-600 text-white"
                : "bg-zinc-900 border border-white/5 text-zinc-500 hover:bg-zinc-800"
            }`}
          >
            {s === "BUY" ? <ArrowUpRight className="w-4 h-4 mr-1" aria-hidden /> : <ArrowDownRight className="w-4 h-4 mr-1" aria-hidden />}
            {isPerp ? (s === "BUY" ? "BUY / LONG" : "SELL / SHORT") : s}
          </button>
        ))}
      </div>

      <div className="flex bg-zinc-900 rounded p-1 border border-white/5" role="group" aria-label="Order type">
        <button type="button" onClick={() => setOrderType("MARKET")} className={toggleBtn(orderType === "MARKET")} aria-pressed={orderType === "MARKET"}>MARKET</button>
        <button type="button" onClick={() => setOrderType("LIMIT")} className={toggleBtn(orderType === "LIMIT")} aria-pressed={orderType === "LIMIT"}>LIMIT</button>
      </div>

      <label className="block">
        <span className="block text-2xs font-bold font-mono text-zinc-400 mb-1 uppercase tracking-wider">Quantity</span>
        <input
          type="number" step="0.0001" min="0.0001" value={quantity} required
          onChange={(e) => setQuantity(e.target.value)}
          className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-sm font-mono text-zinc-200 focus:border-cyan-500 focus:outline-none"
        />
      </label>

      {orderType === "LIMIT" && (
        <label className="block">
          <span className="block text-2xs font-bold font-mono text-zinc-400 mb-1 uppercase tracking-wider">Limit Price</span>
          <input
            type="number" step="0.01" min="0.01" value={price} required
            onChange={(e) => setPrice(e.target.value)}
            className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-sm font-mono text-zinc-200 focus:border-cyan-500 focus:outline-none"
          />
        </label>
      )}

      {isPerp && (
        <div className="space-y-3 border-t border-white/5 pt-3">
          <div>
            <div className="flex items-center justify-between mb-1">
              <label htmlFor="perp-leverage" className="text-2xs font-bold font-mono text-zinc-400 uppercase tracking-wider">Leverage</label>
              <span className="text-sm font-mono font-bold text-cyan-300">{leverage}x</span>
            </div>
            <input
              id="perp-leverage" type="range" min={1} max={MAX_PERP_LEVERAGE} step={1} value={leverage}
              onChange={(e) => setLeverage(Number(e.target.value))}
              className="w-full accent-cyan-500"
              aria-valuetext={`${leverage}x`}
            />
            <div className="flex gap-1 mt-1" role="group" aria-label="Leverage presets">
              {LEVERAGE_CHIPS.map((l) => (
                <button
                  key={l} type="button" onClick={() => setLeverage(l)} aria-pressed={leverage === l}
                  className={`flex-1 py-0.5 text-2xs font-mono rounded border ${
                    leverage === l ? "border-cyan-500 text-cyan-300 bg-cyan-950/40" : "border-white/5 text-zinc-500 hover:text-zinc-300"
                  }`}
                >
                  {l}x
                </button>
              ))}
            </div>
          </div>

          <div>
            <div className="flex bg-zinc-900 rounded p-1 border border-white/5" role="group" aria-label="Margin mode">
              {(["CROSS", "ISOLATED"] as const).map((m) => (
                <button
                  key={m} type="button" onClick={() => setMarginMode(m)} disabled={!!position}
                  aria-pressed={effectiveMode === m}
                  className={`${toggleBtn(effectiveMode === m)} disabled:cursor-not-allowed`}
                >
                  {m}
                </button>
              ))}
            </div>
            {position && <p className="text-2xs text-zinc-500 mt-1">Margin mode is locked while a position is open.</p>}
          </div>

          <div>
            <label className="flex items-center gap-2 text-2xs font-mono text-zinc-300">
              <input
                type="checkbox" checked={reduceOnly} onChange={(e) => setReduceOnly(e.target.checked)}
                aria-describedby={reduceOnlyHelpId} className="accent-cyan-500"
              />
              Reduce-Only
              <Info className="w-3 h-3 text-zinc-500" aria-hidden />
            </label>
            <p id={reduceOnlyHelpId} className={`text-2xs mt-1 ${reduceOnlyError ? "text-rose-400" : "text-zinc-500"}`}>
              {reduceOnlyError || "Only shrinks the open position; never opens or flips it."}
            </p>
          </div>

          <dl className="grid grid-cols-2 gap-y-1 text-2xs font-mono bg-zinc-900/60 rounded p-2" aria-label="Pre-trade estimate">
            <dt className="text-zinc-500">Order notional</dt>
            <dd className="text-right text-zinc-200">{fmt(notional)} USDT</dd>
            <dt className="text-zinc-500">Initial margin</dt>
            <dd className="text-right text-zinc-200">{fmt(requiredMargin)} USDT</dd>
            <dt className="text-zinc-500">Projected liq.</dt>
            <dd className="text-right text-amber-300">
              {reduceOnly ? "n/a" : fmt(projectedLiq)}
              {!reduceOnly && effectiveMode === "CROSS" && projectedLiq !== null ? " (est.)" : ""}
            </dd>
          </dl>
          {!mark && <p className="text-2xs text-zinc-500">Waiting for the mark price feed; estimates need a mark.</p>}
        </div>
      )}

      <button
        type="submit"
        disabled={loading || !!reduceOnlyError || !(qty > 0)}
        className={`w-full py-2.5 font-black text-sm tracking-widest uppercase transition-colors rounded disabled:opacity-50 disabled:cursor-not-allowed ${
          side === "BUY" ? "bg-emerald-600 hover:bg-emerald-500 text-white" : "bg-rose-600 hover:bg-rose-500 text-white"
        }`}
      >
        {loading ? "SUBMITTING..." : `SUBMIT ${side}${isPerp ? ` ${leverage}x` : ""}`}
      </button>
    </form>
  );
}
