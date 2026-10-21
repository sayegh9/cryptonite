"use client";

import { useEffect, useId, useState } from "react";
import { ArrowDownRight, ArrowUpRight, Info } from "lucide-react";
import { fetchApi } from "@/lib/api";
import { useSessionStore, type SessionStatus } from "@/lib/stores/useSessionStore";
import { useKillSwitch, type KillSwitchView } from "@/lib/stores/useKillSwitchStore";
import { usePerpetualStore } from "@/lib/stores/usePerpetualStore";
import {
  MAX_PERP_LEVERAGE,
  hasBrackets,
  initialMargin,
  liquidationPrice,
  toPerpInstrumentId,
  type PerpSide,
} from "@/lib/perp/margin";
import { leverageTier, pct, pillsWith, TIER_CLASSES, usd } from "@/lib/leverage";

type Market = "SPOT" | "PERP";
type Side = "BUY" | "SELL";

/**
 * The server's preview of a manual perp entry (GET /paper/perp/plan, leverage plan 4.5): each pill judged by the
 * dispatcher's own check for this quantity (null = accepted, else the refusal code), and the margin, ISOLATED
 * liquidation price, buffer and losses at the chosen leverage. Decimals arrive as strings.
 */
interface TicketPreview {
  ok: boolean;
  reject: string | null;
  leverage: number;
  pills: Record<string, string | null>;
  recommended_leverage: number | null;
  margin_usd?: string | null;
  notional_usd?: string | null;
  liquidation_price?: string | null;
  liquidation_distance_pct?: string | null;
  buffer_used_pct?: string | null;
  loss_at_stop_usd?: string | null;
  loss_at_stop_pct?: string | null;
  loss_if_liquidated_usd?: string | null;
  funding_8h_usd?: string | null;
}

const PREVIEW_DEBOUNCE_MS = 300;

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

/** Why orders cannot be sent right now, or null when they can. Unknown is blocked, never assumed safe. */
function orderGateReason(session: SessionStatus, killSwitch: KillSwitchView): string | null {
  if (session === "anonymous") return "Sign in as operator to place orders.";
  if (session === "checking") return "Verifying your operator session before orders can be placed.";
  if (session === "unreachable") return "Could not verify the operator session: orders are blocked.";
  if (killSwitch === "latched") return "Kill switch latched: trading is halted.";
  if (killSwitch === "checking") return "Checking the kill switch before orders can be placed.";
  if (killSwitch === "unknown" || killSwitch === "signed-out") return "Kill-switch status unknown: orders are blocked until it is confirmed.";
  return null;
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
  // The paper engine takes orders only from a confirmed operator session and never while the kill switch is
  // latched or its state is unconfirmed. The reason is shown, so a disabled button is never a mystery.
  const session = useSessionStore((s) => s.status);
  const killSwitch = useKillSwitch("PAPER", "PAPER-ACCOUNT-001");
  const gateReason = orderGateReason(session, killSwitch.view);
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
  // "Attach stop": a reduce-only STOP_MARKET on the mark, placed by the dispatcher once the entry fills
  const [attachStop, setAttachStop] = useState(false);
  const [stopPrice, setStopPrice] = useState("");
  const [preview, setPreview] = useState<TicketPreview | null>(null);

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

  const stopPx = parseFloat(stopPrice);
  const stopValue = isPerp && attachStop && !reduceOnly && stopPx > 0 ? stopPx : null;
  const stopMissing = isPerp && attachStop && !reduceOnly && !(stopPx > 0);

  // The dispatcher's view of this entry, read again (debounced) whenever the ticket changes. It fails quiet: no
  // preview shows nothing and never blocks the ticket on its own; the dispatcher checks the order again anyway
  const previewKey = isPerp && !reduceOnly && qty > 0
    ? [instrumentId, side, qty, leverage, orderType === "LIMIT" && limitPx > 0 ? limitPx : "", stopValue ?? ""].join("|")
    : "";
  useEffect(() => {
    if (!previewKey) {
      setPreview(null);
      return;
    }
    const [inst, s, q, lev, px, stop] = previewKey.split("|");
    const params = new URLSearchParams({ instrument_id: inst, side: s, quantity: q, leverage: lev });
    if (px) params.set("price", px);
    if (stop) params.set("stop_loss", stop);
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      fetchApi(`/paper/perp/plan?${params.toString()}`, { signal: ctrl.signal })
        .then((p: TicketPreview) => {
          if (!ctrl.signal.aborted) setPreview(p && typeof p.ok === "boolean" ? p : null);
        })
        .catch(() => {
          if (!ctrl.signal.aborted) setPreview(null);
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [previewKey]);
  const previewRefused = !!preview && !preview.ok && isPerp && !reduceOnly;

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
    if (reduceOnlyError || gateReason || stopMissing) return;
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
        if (stopValue !== null) body.stop_loss = stopValue;
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
              {pillsWith(preview?.recommended_leverage).map((l) => {
                // A pill the dispatcher would refuse for this quantity is disabled, with its reason
                const refusal = preview && !reduceOnly ? preview.pills[String(l)] ?? null : null;
                const recommended = !reduceOnly && preview?.recommended_leverage === l;
                return (
                  <button
                    key={l} type="button" onClick={() => setLeverage(l)} aria-pressed={leverage === l}
                    disabled={!!refusal && leverage !== l}
                    title={refusal ? `Refused at ${l}x: ${refusal}` : recommended ? "The highest leverage the checks allow" : undefined}
                    className={`flex-1 py-0.5 text-2xs font-mono rounded border disabled:opacity-40 disabled:cursor-not-allowed ${
                      leverage === l ? TIER_CLASSES[leverageTier(l)] : "border-white/5 text-zinc-500 hover:text-zinc-300"
                    }`}
                  >
                    {l}x{recommended ? " ★" : ""}
                  </button>
                );
              })}
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

          {!reduceOnly && (
            <div>
              <label className="flex items-center gap-2 text-2xs font-mono text-zinc-300">
                <input type="checkbox" checked={attachStop} onChange={(e) => setAttachStop(e.target.checked)} className="accent-cyan-500" />
                Attach stop
              </label>
              {attachStop && (
                <label className="mt-1 block">
                  <span className="block text-2xs font-bold font-mono text-zinc-400 mb-1 uppercase tracking-wider">Stop price</span>
                  <input
                    type="number" step="0.01" min="0.01" value={stopPrice} required
                    onChange={(e) => setStopPrice(e.target.value)}
                    className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-sm font-mono text-zinc-200 focus:border-cyan-500 focus:outline-none"
                  />
                </label>
              )}
              <p className="text-2xs mt-1 text-zinc-500">
                {attachStop
                  ? "Rests as a reduce-only stop on the mark once the entry fills; its loss must stay within 2% of equity."
                  : "No stop: the position is only closed by you, the kill switch or liquidation."}
              </p>
            </div>
          )}

          {preview && !reduceOnly && (
            <div className="rounded border border-white/5 bg-zinc-900/60 p-2 text-2xs font-mono" aria-label="Leverage plan" role="group">
              {preview.ok ? (
                <>
                  <span className={`rounded border px-1.5 py-0.5 font-black tracking-wider ${TIER_CLASSES[leverageTier(preview.leverage)]}`}>
                    {preview.leverage}x ISOLATED
                  </span>
                  <dl className="mt-1.5 grid grid-cols-2 gap-y-1">
                    <dt className="text-zinc-500">Loss at stop</dt>
                    <dd className="text-right text-rose-300">
                      {preview.loss_at_stop_usd ? `${usd(preview.loss_at_stop_usd)} (${pct(preview.loss_at_stop_pct, 2)} of equity)` : "no stop"}
                    </dd>
                    <dt className="text-zinc-500">Loss if liquidated</dt>
                    <dd className="text-right text-leverage-liquidation">{usd(preview.loss_if_liquidated_usd)}</dd>
                    <dt className="text-zinc-500">Liquidation (ISOLATED)</dt>
                    <dd className="text-right text-leverage-liquidation">
                      {preview.liquidation_price ? fmt(Number(preview.liquidation_price)) : "none"}
                      {preview.liquidation_distance_pct ? ` (${pct(preview.liquidation_distance_pct)})` : ""}
                    </dd>
                    {preview.buffer_used_pct && (
                      <>
                        <dt className="text-zinc-500">Stop uses</dt>
                        <dd className="text-right text-zinc-200">{pct(preview.buffer_used_pct, 0)} of the distance (limit 50%)</dd>
                      </>
                    )}
                    <dt className="text-zinc-500">Funding / 8h</dt>
                    <dd className="text-right text-zinc-200">{preview.funding_8h_usd ? usd(preview.funding_8h_usd, 3) : "unknown"}</dd>
                  </dl>
                  {effectiveMode === "CROSS" && (
                    <p className="mt-1 text-zinc-500">CROSS: the liquidation price depends on the whole wallet; the ISOLATED one is shown.</p>
                  )}
                </>
              ) : (
                <p role="alert" className="text-rose-300">Would be refused: {preview.reject}</p>
              )}
            </div>
          )}

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
        disabled={loading || !!reduceOnlyError || !!gateReason || !(qty > 0) || stopMissing || previewRefused}
        className={`w-full py-2.5 font-black text-sm tracking-widest uppercase transition-colors rounded disabled:opacity-50 disabled:cursor-not-allowed ${
          // emerald-700 keeps the white label above 4.5:1 (emerald-600 was 3.8:1)
          side === "BUY" ? "bg-emerald-700 hover:bg-emerald-600 text-white" : "bg-rose-600 hover:bg-rose-500 text-white"
        }`}
      >
        {loading ? "SUBMITTING..." : `SUBMIT ${side}${isPerp ? ` ${leverage}x` : ""}`}
      </button>
      {gateReason && (
        <p role="status" className="text-2xs font-mono text-amber-200">
          {gateReason}
        </p>
      )}
    </form>
  );
}
