"use client";

import React, { useState, useEffect } from "react";
import { getStreamClient } from "@/lib/ws";
import { BookOpen } from "lucide-react";
import { Fresh } from "@/components/Fresh";
import { isKnownNumber } from "@/lib/freshness";

interface OrderbookPanelProps {
  symbol: string;
}

const PRICE_MAX_AGE_MS = 10_000;

/**
 * Last traded price for the symbol. The platform has no L2 order-book feed, so no depth is shown:
 * an earlier version drew synthetic levels around the price, which looked like market data.
 */
export function OrderbookPanel({ symbol }: OrderbookPanelProps) {
  const [price, setPrice] = useState<number | null>(null);
  const [asOf, setAsOf] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Reset on symbol change: the previous symbol's price is not this one's
  useEffect(() => {
    setPrice(null);
    setAsOf(null);
    setError(null);
  }, [symbol]);

  // Live stream ticks
  useEffect(() => {
    const cleanSym = symbol.split(".")[0].toUpperCase();
    const client = getStreamClient();
    const unsub = client.subscribe((msg: any) => {
      const msgSym = String(msg.symbol || (msg.instrument_id ? msg.instrument_id.split(".")[0] : "")).toUpperCase();
      if ((msg.type === "open_kline" || msg.type === "closed_bar_committed") && msgSym === cleanSym) {
        const close = Number(msg.close);
        if (!isKnownNumber(close)) return;
        setPrice(close);
        setAsOf(Date.now());
      }
    });
    return () => unsub();
  }, [symbol]);

  // Initial price from the API
  useEffect(() => {
    let active = true;
    const cleanSym = symbol.split(".")[0].toUpperCase();
    fetch(`/api/v1/market/prices?symbols=${encodeURIComponent(cleanSym)}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`price unavailable: ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (!active) return;
        const item = Array.isArray(data) ? data.find((r: any) => r.symbol === cleanSym) : data?.[cleanSym];
        const p = Number(item?.last_price ?? item?.price);
        if (isKnownNumber(p) && p > 0) {
          setPrice(p);
          setAsOf(Date.now());
        }
      })
      .catch((err) => {
        if (active) setError(err instanceof Error && err.message.startsWith("price") ? err.message : "price unavailable: network error");
      });
    return () => {
      active = false;
    };
  }, [symbol]);

  return (
    <div className="flex flex-col h-full bg-zinc-950 text-zinc-200 font-mono text-2xs">
      <div className="px-3 py-1.5 border-b border-white/5 bg-zinc-900/60 flex items-center justify-between flex-none">
        <div className="flex items-center space-x-1.5">
          <BookOpen className="w-3.5 h-3.5 text-cyan-400" aria-hidden="true" />
          <span className="font-bold text-zinc-100 uppercase tracking-wider text-2xs">
            {symbol.replace(".BINANCE", "")}
          </span>
        </div>
      </div>

      <div className="flex items-center justify-between px-3 py-2 border-b border-white/5">
        <span className="text-zinc-500">Last price</span>
        <span className="text-xs font-bold tabular-nums" data-testid="orderbook-last-price">
          <Fresh
            value={price}
            asOf={asOf}
            maxAgeMs={PRICE_MAX_AGE_MS}
            quietWhenLive={false}
            format={(p) => `$${p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
          />
        </span>
      </div>
      {error && price === null && <p className="px-3 py-1 text-rose-400">{error}</p>}

      <div className="flex-1 flex items-center justify-center p-4 text-center text-zinc-500 leading-relaxed">
        No L2 order-book feed is connected, so depth and spread are not shown.
      </div>
    </div>
  );
}
