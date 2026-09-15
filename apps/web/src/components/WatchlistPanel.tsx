"use client";

import React, { useEffect, useState } from "react";
import { getStreamClient } from "@/lib/ws";
import { Search, TrendingUp, TrendingDown } from "lucide-react";

interface TickerItem {
  symbol: string;
  name: string;
  price: number;
  change24h: number;
  high24h: number;
  low24h: number;
  volume: number;
}

interface Props {
  selectedSymbol: string;
  onSelectSymbol: (symbol: string) => void;
}

const DEFAULT_TICKERS: TickerItem[] = [
  {
    symbol: "BTCUSDT",
    name: "Bitcoin / Tether",
    price: 64250.0,
    change24h: 1.84,
    high24h: 65100.0,
    low24h: 63800.0,
    volume: 12450.2,
  },
  {
    symbol: "ETHUSDT",
    name: "Ethereum / Tether",
    price: 3480.5,
    change24h: -0.92,
    high24h: 3550.0,
    low24h: 3420.0,
    volume: 48920.8,
  },
  {
    symbol: "SOLUSDT",
    name: "Solana / Tether",
    price: 142.2,
    change24h: 3.45,
    high24h: 146.0,
    low24h: 137.5,
    volume: 98120.0,
  },
  {
    symbol: "BNBUSDT",
    name: "BNB / Tether",
    price: 580.1,
    change24h: 0.15,
    high24h: 588.0,
    low24h: 574.0,
    volume: 8230.4,
  },
];

export const WatchlistPanel: React.FC<Props> = ({ selectedSymbol, onSelectSymbol }) => {
  const [tickers, setTickers] = useState<TickerItem[]>(DEFAULT_TICKERS);
  const [search, setSearch] = useState("");

  useEffect(() => {
    // Listen to live WebSocket stream to update prices in realtime
    const client = getStreamClient();
    const unsubscribe = client.subscribe((msg: any) => {
      if (msg.type === "open_kline") {
        const rawSym = msg.instrument_id.split(".")[0];
        setTickers((prev) =>
          prev.map((t) => {
            if (t.symbol === rawSym) {
              const prevPrice = t.price;
              const newPrice = Number(msg.close);
              const change = prevPrice > 0 ? ((newPrice - prevPrice) / prevPrice) * 100 : t.change24h;
              return {
                ...t,
                price: newPrice,
                change24h: change,
                high24h: Math.max(t.high24h, msg.high),
                low24h: Math.min(t.low24h, msg.low),
              };
            }
            return t;
          })
        );
      }
    });

    return () => {
      unsubscribe();
    };
  }, []);

  const filtered = tickers.filter(
    (t) =>
      t.symbol.toLowerCase().includes(search.toLowerCase()) ||
      t.name.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div className="flex flex-col h-full w-full bg-zinc-950 border-r border-white/5">
      {/* Search Header */}
      <div className="p-3 border-b border-white/5 bg-zinc-950">
        <div className="relative flex items-center">
          <Search className="w-3.5 h-3.5 absolute left-3 text-zinc-500" />
          <input
            type="text"
            placeholder="Search markets..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full bg-zinc-900 border border-white/5 rounded pl-8 pr-3 py-1.5 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/20 transition-all font-mono"
          />
        </div>
      </div>

      {/* Watchlist Items */}
      <div className="flex-1 overflow-y-auto">
        {filtered.map((item) => {
          const isSelected = selectedSymbol.toUpperCase().replace(".BINANCE", "") === item.symbol;
          const isUp = item.change24h >= 0;

          return (
            <div
              key={item.symbol}
              onClick={() => onSelectSymbol(item.symbol)}
              className={`flex items-center justify-between px-4 py-3 cursor-pointer transition-colors border-b border-white/5 ${
                isSelected
                  ? "bg-cyan-950/20"
                  : "hover:bg-zinc-900/60"
              }`}
            >
              <div className="flex flex-col">
                <span className="font-mono text-xs font-bold text-zinc-200">
                  {item.symbol}
                </span>
                <span className="text-[10px] text-zinc-500 truncate max-w-[110px]">
                  {item.name}
                </span>
              </div>

              <div className="flex flex-col items-end">
                <span className="font-mono text-xs font-semibold text-zinc-100">
                  ${item.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </span>
                <div
                  className={`flex items-center space-x-0.5 text-[10px] font-mono font-medium ${
                    isUp ? "text-emerald-400" : "text-red-400"
                  }`}
                >
                  {isUp ? <TrendingUp className="w-2.5 h-2.5" /> : <TrendingDown className="w-2.5 h-2.5" />}
                  <span>
                    {isUp ? "+" : ""}
                    {item.change24h.toFixed(2)}%
                  </span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
