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
  const [isLoaded, setIsLoaded] = useState(false);

  useEffect(() => {
    const saved = localStorage.getItem("watchlist_tickers");
    if (saved) {
      try {
        setTickers(JSON.parse(saved));
      } catch (e) {}
    }
    setIsLoaded(true);
  }, []);

  useEffect(() => {
    if (!isLoaded) return;
    localStorage.setItem("watchlist_tickers", JSON.stringify(tickers));
    const client = getStreamClient();
    client.updateSubscriptions(
      ["open_kline", "closed_bar_committed", "signal_candidate"],
      tickers.map(t => `${t.symbol}.BINANCE`)
    );
  }, [tickers, isLoaded]);
  const [search, setSearch] = useState("");
  const [availableInstruments, setAvailableInstruments] = useState<any[]>([]);

  useEffect(() => {
    // Fetch available instruments
    fetch("/api/v1/instruments")
      .then((res) => res.json())
      .then((data) => {
        if (Array.isArray(data)) setAvailableInstruments(data);
      })
      .catch((err) => console.error("Failed to load instruments", err));

    // Hydrate real 24h stats from Binance (once on mount)
    const hydrateStats = async () => {
      const watchlistSymbols = tickers.map((t) => t.symbol).join(",");
      if (!watchlistSymbols) return;
      try {
        const res = await fetch(`/api/v1/market/prices?symbols=${watchlistSymbols}`);
        if (!res.ok) return;
        const stats: Array<{
          symbol: string;
          last_price: number;
          price_change_pct: number;
          high_24h: number;
          low_24h: number;
          volume: number;
          quote_volume: number;
        }> = await res.json();
        setTickers((prev) =>
          prev.map((t) => {
            const s = stats.find((r) => r.symbol === t.symbol);
            if (!s) return t;
            return {
              ...t,
              price: s.last_price,
              change24h: s.price_change_pct,
              high24h: s.high_24h,
              low24h: s.low_24h,
              volume: s.volume,
            };
          })
        );
      } catch (_e) {
        // Silently ignore — live WS stream will hydrate price as bars arrive
      }
    };
    hydrateStats();

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
                high24h: Math.max(t.high24h, Number(msg.high)),
                low24h: Math.min(t.low24h, Number(msg.low)),
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

  const searchResults = search.trim().length > 0
    ? availableInstruments.filter(
        (inst) =>
          (inst.symbol.toLowerCase().includes(search.toLowerCase()) ||
           inst.base_currency.toLowerCase().includes(search.toLowerCase())) &&
          !tickers.some((t) => t.symbol === inst.symbol)
      )
    : [];

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
      <div className="flex-1 overflow-y-auto relative">
        {search.trim().length > 0 && searchResults.length > 0 && (
          <div className="bg-zinc-900 border-b border-white/5 py-2">
            <div className="px-4 text-[10px] font-bold text-zinc-500 uppercase tracking-wider mb-1">
              Add new asset
            </div>
            {searchResults.map((inst) => (
              <div
                key={inst.instrument_id}
                onClick={async () => {
                  let initialPrice = 0;
                  try {
                    const barsRes = await fetch(`/api/v1/bars?symbol=${inst.symbol}&timeframe=1m&limit=1`);
                    const barsData = await barsRes.json();
                    if (barsData && barsData.length > 0) {
                      initialPrice = Number(barsData[0].close) || 0;
                    }
                  } catch(e) {}
                  
                  const newTicker: TickerItem = {
                    symbol: inst.symbol,
                    name: `${inst.base_currency} / ${inst.quote_currency}`,
                    price: initialPrice,
                    change24h: 0,
                    high24h: 0,
                    low24h: 0,
                    volume: 0,
                  };
                  setTickers((prev) => {
                    if (prev.some(t => t.symbol === inst.symbol)) return prev;
                    return [...prev, newTicker];
                  });
                  setSearch("");
                  onSelectSymbol(inst.symbol);
                }}
                className="flex items-center justify-between px-4 py-2 cursor-pointer hover:bg-zinc-800 transition-colors"
              >
                <div className="flex flex-col">
                  <span className="font-mono text-xs font-bold text-zinc-200">{inst.symbol}</span>
                  <span className="text-[10px] text-zinc-500">{inst.base_currency} / {inst.quote_currency}</span>
                </div>
                <div className="text-cyan-500 text-xs font-bold">+</div>
              </div>
            ))}
          </div>
        )}

        {filtered.map((item) => {
          const isSelected = selectedSymbol.toUpperCase().replace(".BINANCE", "") === item.symbol;
          const isUp = item.change24h >= 0;

          return (
            <div
              key={item.symbol}
              onClick={() => onSelectSymbol(item.symbol)}
              className={`group flex items-center justify-between px-4 py-3 cursor-pointer transition-colors border-b border-white/5 ${
                isSelected
                  ? "bg-cyan-950/20"
                  : "hover:bg-zinc-900/60"
              }`}
            >
              <div 
                className="flex flex-col hover:bg-zinc-800/50 rounded px-1 -mx-1 transition-colors"
                onClick={(e) => {
                  e.stopPropagation();
                  window.dispatchEvent(new CustomEvent("cryptonite:open-profile", { detail: { symbol: item.symbol } }));
                }}
              >
                <span className="font-mono text-xs font-bold text-zinc-200">
                  {item.symbol}
                </span>
                <span className="text-[10px] text-zinc-500 truncate max-w-[110px]">
                  {item.name}
                </span>
              </div>

              <div className="flex items-center gap-3">
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setTickers(prev => prev.filter(t => t.symbol !== item.symbol));
                  }}
                  className="opacity-0 group-hover:opacity-100 text-zinc-500 hover:text-red-400 text-xs px-1"
                >
                  ×
                </button>
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
            </div>
          );
        })}

        {filtered.length === 0 && searchResults.length === 0 && search.trim().length > 0 && (
          <div className="flex flex-col items-center justify-center p-6 text-center space-y-3">
            <span className="text-xs text-zinc-500">Not found in local database.</span>
            <button
              onClick={async (e) => {
                const btn = e.currentTarget;
                let newSym = search.trim().toUpperCase();
                if (!newSym.endsWith("USDT") && !newSym.endsWith("BTC") && !newSym.endsWith("FDUSD")) {
                  newSym = `${newSym}USDT`;
                }
                
                try {
                  btn.disabled = true;
                  btn.innerHTML = `Onboarding ${newSym}...`;
                  
                  const res = await fetch("/api/v1/instruments/onboard", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ symbol: newSym }),
                  });
                  
                  if (!res.ok) throw new Error("Failed to onboard");
                  
                  // Wait a brief moment for the backend background task to create the initial catalog entry
                  await new Promise(r => setTimeout(r, 1500));
                  
                  let initialPrice = 0;
                  try {
                    const barsRes = await fetch(`/api/v1/bars?symbol=${newSym}&timeframe=1m&limit=1`);
                    const barsData = await barsRes.json();
                    if (barsData && barsData.length > 0) {
                      initialPrice = Number(barsData[0].close) || 0;
                    }
                  } catch(e) {}
                  
                  const newTicker: TickerItem = {
                    symbol: newSym,
                    name: `${newSym} Market`,
                    price: initialPrice,
                    change24h: 0,
                    high24h: 0,
                    low24h: 0,
                    volume: 0,
                  };
                  setTickers((prev) => {
                    if (prev.some(t => t.symbol === newSym)) return prev;
                    return [...prev, newTicker];
                  });
                  setSearch("");
                  onSelectSymbol(newSym);
                  
                  // Re-fetch available instruments to update the local cache
                  fetch("/api/v1/instruments")
                    .then((res) => res.json())
                    .then((data) => {
                      if (Array.isArray(data)) setAvailableInstruments(data);
                    });
                } catch (err) {
                  console.error(err);
                  btn.innerHTML = `Failed! Try again`;
                  btn.disabled = false;
                }
              }}
              className="px-4 py-2 bg-cyan-900/40 hover:bg-cyan-900/60 text-cyan-400 border border-cyan-800/50 text-xs font-bold rounded transition-colors"
            >
              Onboard from Binance
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
