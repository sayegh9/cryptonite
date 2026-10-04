"use client";

import { useState } from "react";
import { fetchApi } from "@/lib/api";
import { X, ArrowUpRight, ArrowDownRight } from "lucide-react";

export function ManualTradeModal({ onClose, onOrderSubmitted }: { onClose: () => void, onOrderSubmitted: () => void }) {
  const [instrumentId, setInstrumentId] = useState("BTCUSDT.BINANCE");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [orderType, setOrderType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [quantity, setQuantity] = useState("0.01");
  const [price, setPrice] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    try {
      await fetchApi("/paper/orders", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": localStorage.getItem("csrf_token") || "",
        },
        body: JSON.stringify({
          instrument_id: instrumentId,
          side: side,
          order_type: orderType,
          quantity: parseFloat(quantity),
          price: orderType === "LIMIT" ? parseFloat(price) : null,
        }),
      });
      onOrderSubmitted();
    } catch (err: any) {
      setError(err.message || "Failed to submit order");
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-zinc-950 border border-white/10 w-full max-w-md rounded shadow-2xl overflow-hidden flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-white/5 bg-zinc-900/50">
          <h2 className="text-sm font-bold text-zinc-100 uppercase tracking-wider">Manual Trade Entry</h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300 transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5">
          {error && (
            <div className="mb-4 bg-rose-950/40 border border-rose-900/50 text-rose-400 text-2xs font-mono p-3 rounded">
              {error}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label className="block text-2xs font-bold font-mono text-zinc-400 mb-1.5 uppercase tracking-wider">Instrument</label>
              <input
                type="text"
                value={instrumentId}
                onChange={(e) => setInstrumentId(e.target.value)}
                className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-sm font-bold text-zinc-200 focus:border-cyan-500 focus:outline-none uppercase"
                required
              />
            </div>

            <div className="flex space-x-2">
              <button
                type="button"
                onClick={() => setSide("BUY")}
                className={`flex-1 flex justify-center items-center py-2.5 rounded font-black text-sm tracking-wider transition-colors ${
                  side === "BUY" ? "bg-emerald-600 text-white border border-transparent" : "bg-zinc-900 border border-white/5 text-zinc-500 hover:bg-zinc-800"
                }`}
              >
                <ArrowUpRight className="w-4 h-4 mr-1" />
                BUY
              </button>
              <button
                type="button"
                onClick={() => setSide("SELL")}
                className={`flex-1 flex justify-center items-center py-2.5 rounded font-black text-sm tracking-wider transition-colors ${
                  side === "SELL" ? "bg-rose-600 text-white border border-transparent" : "bg-zinc-900 border border-white/5 text-zinc-500 hover:bg-zinc-800"
                }`}
              >
                <ArrowDownRight className="w-4 h-4 mr-1" />
                SELL
              </button>
            </div>

            <div className="flex bg-zinc-900 rounded p-1 border border-white/5">
              <button
                type="button"
                onClick={() => setOrderType("MARKET")}
                className={`flex-1 py-1 text-xs font-bold rounded ${
                  orderType === "MARKET" ? "bg-zinc-700 text-white" : "text-zinc-500 hover:text-zinc-300"
                }`}
              >
                MARKET
              </button>
              <button
                type="button"
                onClick={() => setOrderType("LIMIT")}
                className={`flex-1 py-1 text-xs font-bold rounded ${
                  orderType === "LIMIT" ? "bg-zinc-700 text-white" : "text-zinc-500 hover:text-zinc-300"
                }`}
              >
                LIMIT
              </button>
            </div>

            <div>
              <label className="block text-2xs font-bold font-mono text-zinc-400 mb-1.5 uppercase tracking-wider">Quantity</label>
              <input
                type="number"
                step="0.0001"
                min="0.0001"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
                className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2.5 text-sm font-mono text-zinc-200 focus:border-cyan-500 focus:outline-none"
                required
              />
            </div>

            {orderType === "LIMIT" && (
              <div>
                <label className="block text-2xs font-bold font-mono text-zinc-400 mb-1.5 uppercase tracking-wider">Limit Price</label>
                <input
                  type="number"
                  step="0.01"
                  min="0.01"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2.5 text-sm font-mono text-zinc-200 focus:border-cyan-500 focus:outline-none"
                  required
                />
              </div>
            )}
          </div>

          <div className="mt-8">
            <button
              type="submit"
              disabled={loading}
              className={`w-full py-3 font-black text-sm tracking-widest uppercase transition-colors rounded ${
                side === "BUY"
                  ? "bg-emerald-600 hover:bg-emerald-500 text-white"
                  : "bg-rose-600 hover:bg-rose-500 text-white"
              }`}
            >
              {loading ? "SUBMITTING..." : `SUBMIT ${side} ORDER`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
