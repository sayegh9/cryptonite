"use client";

import React, { useState, useEffect, useMemo } from "react";
import { getStreamClient } from "@/lib/ws";
import { BookOpen, ArrowUpRight } from "lucide-react";

interface OrderbookEntry {
  price: number;
  size: number;
  total: number;
}

interface OrderbookPanelProps {
  symbol: string;
}

export function OrderbookPanel({ symbol }: OrderbookPanelProps) {
  const [isMounted, setIsMounted] = useState(false);
  const [basePrice, setBasePrice] = useState<number>(85000);
  const [spreadPct] = useState<number>(0.01);
  const [grouping, setGrouping] = useState<number>(1);
  const [lastUpdate, setLastUpdate] = useState<number>(0);

  useEffect(() => {
    setIsMounted(true);
  }, []);

  // Listen to live stream for real price updates
  useEffect(() => {
    const cleanSym = symbol.split(".")[0].toUpperCase();
    const client = getStreamClient();
    const unsub = client.subscribe((msg: any) => {
      const msgSym = (msg.symbol || (msg.instrument_id ? msg.instrument_id.split(".")[0] : "")).toUpperCase();
      if (
        (msg.type === "open_kline" || msg.type === "closed_bar_committed") &&
        msgSym === cleanSym &&
        msg.close
      ) {
        setBasePrice(Number(msg.close));
        setLastUpdate(Date.now());
      }
    });

    return () => unsub();
  }, [symbol]);

  // Fetch initial 24h ticker price for the symbol
  useEffect(() => {
    let isMounted = true;
    const cleanSym = symbol.split(".")[0].toUpperCase();
    fetch(`/api/v1/market/prices?symbols=${cleanSym}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!isMounted || !data) return;
        const item = Array.isArray(data)
          ? data.find((r: any) => r.symbol === cleanSym)
          : data[cleanSym];
        const price = item?.last_price ?? item?.price;
        if (price) {
          setBasePrice(Number(price));
          setLastUpdate(Date.now());
        }
      })
      .catch(() => {});

    return () => {
      isMounted = false;
    };
  }, [symbol]);

  // Format prices consistently between SSR and client
  const formatPrice = (val: number) => {
    return Number(val).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };

  // Generate synthetic high-resolution L2 depth anchored to live basePrice
  const { asks, bids, spread, spreadPercent } = useMemo(() => {
    const depthLevels = 10;
    const tickStep = grouping > 0 ? grouping : basePrice * 0.0001;
    const mid = basePrice;
    
    // Seedable deterministic pseudo-random variance based on lastUpdate, or 0 on initial render
    const seed = isMounted && lastUpdate > 0 ? Math.sin(lastUpdate / 5000) : 0;
    const halfSpread = Math.max(0.5, mid * (spreadPct / 100) * 0.5);

    const generatedAsks: OrderbookEntry[] = [];
    let askTotal = 0;
    for (let i = depthLevels; i >= 1; i--) {
      const price = Math.round((mid + halfSpread + i * tickStep) * 100) / 100;
      const size = Math.round((0.15 + Math.abs(Math.sin(i * 1.7 + seed)) * 1.8) * 1000) / 1000;
      askTotal += size;
      generatedAsks.push({ price, size, total: Math.round(askTotal * 1000) / 1000 });
    }

    const generatedBids: OrderbookEntry[] = [];
    let bidTotal = 0;
    for (let i = 1; i <= depthLevels; i++) {
      const price = Math.round((mid - halfSpread - i * tickStep) * 100) / 100;
      const size = Math.round((0.15 + Math.abs(Math.cos(i * 1.3 + seed)) * 1.8) * 1000) / 1000;
      bidTotal += size;
      generatedBids.push({ price, size, total: Math.round(bidTotal * 1000) / 1000 });
    }

    const currentSpread = generatedAsks[generatedAsks.length - 1].price - generatedBids[0].price;
    const currentSpreadPct = (currentSpread / mid) * 100;

    return {
      asks: generatedAsks,
      bids: generatedBids,
      spread: Math.max(0.01, currentSpread),
      spreadPercent: Math.max(0.001, currentSpreadPct),
    };
  }, [basePrice, grouping, spreadPct, lastUpdate, isMounted]);

  const maxTotal = Math.max(
    asks.length > 0 ? asks[0].total : 1,
    bids.length > 0 ? bids[bids.length - 1].total : 1
  );

  return (
    <div className="flex flex-col h-full bg-zinc-950 text-zinc-200 font-mono text-[11px] select-none">
      {/* Header & Symbol Bar */}
      <div className="px-3 py-1.5 border-b border-white/5 bg-zinc-900/60 flex items-center justify-between flex-none">
        <div className="flex items-center space-x-1.5">
          <BookOpen className="w-3.5 h-3.5 text-cyan-400" />
          <span className="font-bold text-zinc-100 uppercase tracking-wider text-[10px]">
            {symbol.replace(".BINANCE", "")}
          </span>
          <span className="text-[9px] px-1.5 py-0.2 rounded bg-cyan-950/60 text-cyan-400 border border-cyan-800/40">
            L2 Depth
          </span>
        </div>

        {/* Grouping Select */}
        <div className="flex items-center space-x-1 text-[10px]">
          <span className="text-zinc-500">Step:</span>
          {[0.1, 1, 10].map((step) => (
            <button
              key={step}
              onClick={() => setGrouping(step)}
              className={`px-1.5 py-0.5 rounded transition-colors ${
                grouping === step
                  ? "bg-zinc-800 text-cyan-400 font-bold border border-cyan-500/30"
                  : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {step}
            </button>
          ))}
        </div>
      </div>

      {/* Table Headers */}
      <div className="grid grid-cols-3 px-3 py-1 border-b border-white/5 text-zinc-500 text-[9px] uppercase tracking-wider flex-none">
        <span className="text-left">Price (USDT)</span>
        <span className="text-right">Size</span>
        <span className="text-right">Total</span>
      </div>

      {/* Asks (Sells / Red) */}
      <div className="flex-1 min-h-0 flex flex-col justify-end overflow-hidden px-1">
        {asks.map((ask) => {
          const depthPct = Math.min(100, Math.round((ask.total / maxTotal) * 100));
          return (
            <div
              key={ask.price}
              className="relative grid grid-cols-3 px-2 py-0.5 hover:bg-rose-950/20 text-[10px] transition-colors"
            >
              {/* Depth Visual Bar */}
              <div
                className="absolute inset-y-0 right-0 bg-rose-500/10 pointer-events-none transition-all duration-300"
                style={{ width: `${depthPct}%` }}
              />
              <span className="text-rose-400 font-medium z-10">
                {formatPrice(ask.price)}
              </span>
              <span className="text-zinc-300 text-right z-10">{ask.size.toFixed(3)}</span>
              <span className="text-zinc-500 text-right z-10">{ask.total.toFixed(3)}</span>
            </div>
          );
        })}
      </div>

      {/* Mid Market Price & Spread Banner */}
      <div className="my-1 py-1.5 px-3 bg-zinc-900/90 border-y border-white/5 flex items-center justify-between flex-none">
        <div className="flex items-center space-x-1.5">
          <span className="text-xs font-bold font-mono text-zinc-100">
            ${formatPrice(basePrice)}
          </span>
          <span className="text-emerald-400 text-[10px] flex items-center">
            <ArrowUpRight className="w-3 h-3" /> Live
          </span>
        </div>

        <div className="text-[10px] text-zinc-400 font-mono">
          <span className="text-zinc-500 mr-1">Spread:</span>
          <span>{spread.toFixed(2)} ({spreadPercent.toFixed(3)}%)</span>
        </div>
      </div>

      {/* Bids (Buys / Green) */}
      <div className="flex-1 min-h-0 flex flex-col justify-start overflow-hidden px-1">
        {bids.map((bid) => {
          const depthPct = Math.min(100, Math.round((bid.total / maxTotal) * 100));
          return (
            <div
              key={bid.price}
              className="relative grid grid-cols-3 px-2 py-0.5 hover:bg-emerald-950/20 text-[10px] transition-colors"
            >
              {/* Depth Visual Bar */}
              <div
                className="absolute inset-y-0 right-0 bg-emerald-500/10 pointer-events-none transition-all duration-300"
                style={{ width: `${depthPct}%` }}
              />
              <span className="text-emerald-400 font-medium z-10">
                {formatPrice(bid.price)}
              </span>
              <span className="text-zinc-300 text-right z-10">{bid.size.toFixed(3)}</span>
              <span className="text-zinc-500 text-right z-10">{bid.total.toFixed(3)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
