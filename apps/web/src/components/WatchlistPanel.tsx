"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { getStreamClient } from "@/lib/ws";
import { Search, TrendingUp, TrendingDown } from "lucide-react";
import { Fresh } from "@/components/Fresh";
import { isKnownNumber, UNKNOWN_TEXT } from "@/lib/freshness";

interface TickerItem {
  symbol: string;
  name: string;
  /** null until a price arrives from /market/prices or the kline stream */
  price: number | null;
  change24h: number | null;
  high24h: number | null;
  low24h: number | null;
  volume: number | null;
  /** When `price` was observed (epoch ms); null = never */
  asOf: number | null;
}

interface Props {
  selectedSymbol: string;
  onSelectSymbol: (symbol: string) => void;
}

/** A price older than this is "delayed"; 3× older is "stale". The REST poll runs every 15 s. */
const PRICE_MAX_AGE_MS = 20_000;
const POLL_MS = 15_000;

const seed = (symbol: string, name: string): TickerItem => ({
  symbol, name, price: null, change24h: null, high24h: null, low24h: null, volume: null, asOf: null,
});

// Symbols only: prices are unknown until the API or the stream reports them
const DEFAULT_TICKERS: TickerItem[] = [
  seed("BTCUSDT", "Bitcoin / Tether"),
  seed("ETHUSDT", "Ethereum / Tether"),
  seed("SOLUSDT", "Solana / Tether"),
  seed("BNBUSDT", "BNB / Tether"),
];

/** Only symbols and names persist; prices saved by older builds are discarded. */
function loadSaved(): TickerItem[] | null {
  try {
    const raw = localStorage.getItem("watchlist_tickers");
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const rows = parsed
      .filter((t) => t && typeof t.symbol === "string")
      .map((t) => seed(t.symbol, typeof t.name === "string" ? t.name : t.symbol));
    return rows.length ? rows : null;
  } catch {
    return null;
  }
}

export const WatchlistPanel: React.FC<Props> = ({ selectedSymbol, onSelectSymbol }) => {
  const [tickers, setTickers] = useState<TickerItem[]>(DEFAULT_TICKERS);
  const [isLoaded, setIsLoaded] = useState(false);
  const [priceError, setPriceError] = useState<string | null>(null);
  const tickersRef = useRef(tickers);
  tickersRef.current = tickers;

  useEffect(() => {
    const saved = loadSaved();
    if (saved) setTickers(saved);
    setIsLoaded(true);
  }, []);

  useEffect(() => {
    if (!isLoaded) return;
    try {
      localStorage.setItem("watchlist_tickers", JSON.stringify(tickers.map(({ symbol, name }) => ({ symbol, name }))));
    } catch {}
    const client = getStreamClient();
    client.updateSubscriptions(
      ["open_kline", "closed_bar_committed", "signal_candidate"],
      tickers.map(t => `${t.symbol}.BINANCE`)
    );
  }, [tickers, isLoaded]);
  const [search, setSearch] = useState("");
  const [availableInstruments, setAvailableInstruments] = useState<any[]>([]);

  // Real 24h stats from the API; a failure is shown, and rows keep their last observed value + age
  const hydrateStats = useCallback(async () => {
    const watchlistSymbols = tickersRef.current.map((t) => t.symbol).join(",");
    if (!watchlistSymbols) return;
    try {
      const res = await fetch(`/api/v1/market/prices?symbols=${encodeURIComponent(watchlistSymbols)}`);
      if (!res.ok) {
        setPriceError(`Prices are not updating (server error ${res.status}). Last values are kept and it will retry.`);
        return;
      }
      const stats: Array<{
        symbol: string;
        last_price: number | string | null;
        price_change_pct: number | string | null;
        high_24h: number | string | null;
        low_24h: number | string | null;
        volume: number | string | null;
      }> = await res.json();
      if (!Array.isArray(stats)) {
        setPriceError("Prices are not updating (unexpected response). Last values are kept and it will retry.");
        return;
      }
      setPriceError(null);
      const at = Date.now();
      const num = (v: unknown) => (v === null || v === undefined || v === "" ? null : Number(v));
      setTickers((prev) =>
        prev.map((t) => {
          const s = stats.find((r) => r.symbol === t.symbol);
          const price = s ? num(s.last_price) : null;
          if (!s || !isKnownNumber(price)) return t;
          return {
            ...t,
            price,
            change24h: num(s.price_change_pct),
            high24h: num(s.high_24h),
            low24h: num(s.low_24h),
            volume: num(s.volume),
            asOf: at,
          };
        })
      );
    } catch {
      setPriceError("Prices are not updating (network error). Last values are kept and it will retry.");
    }
  }, []);

  useEffect(() => {
    // Fetch available instruments
    fetch("/api/v1/instruments")
      .then((res) => res.json())
      .then((data) => {
        if (Array.isArray(data)) setAvailableInstruments(data);
      })
      .catch((err) => console.error("Failed to load instruments", err));

    void hydrateStats();
    const interval = setInterval(hydrateStats, POLL_MS);

    // Live WebSocket stream updates the price (and its observation time)
    const client = getStreamClient();
    const unsubscribe = client.subscribe((msg: any) => {
      if (msg.type === "open_kline" && typeof msg.instrument_id === "string") {
        const rawSym = msg.instrument_id.split(".")[0];
        const close = Number(msg.close);
        if (!isKnownNumber(close)) return;
        setTickers((prev) =>
          prev.map((t) => {
            if (t.symbol !== rawSym) return t;
            const high = Number(msg.high);
            const low = Number(msg.low);
            return {
              ...t,
              price: close,
              high24h: t.high24h !== null && isKnownNumber(high) ? Math.max(t.high24h, high) : t.high24h,
              low24h: t.low24h !== null && isKnownNumber(low) ? Math.min(t.low24h, low) : t.low24h,
              asOf: Date.now(),
            };
          })
        );
      }
    });

    return () => {
      clearInterval(interval);
      unsubscribe();
    };
  }, [isLoaded, hydrateStats]);

  const addTicker = (symbol: string, name: string) => {
    setTickers((prev) => (prev.some((t) => t.symbol === symbol) ? prev : [...prev, seed(symbol, name)]));
    setSearch("");
    onSelectSymbol(symbol);
    // Price stays unknown until the next poll or stream tick reports it
    setTimeout(() => void hydrateStats(), 0);
  };

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
          <Search className="w-3.5 h-3.5 absolute left-3 text-zinc-500" aria-hidden="true" />
          <input
            type="text"
            placeholder="Search markets..."
            aria-label="Search markets"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full bg-zinc-900 border border-white/5 rounded pl-8 pr-3 py-1.5 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/20 transition-all font-mono"
          />
        </div>
      </div>

      {priceError && (
        <div role="status" className="px-3 py-1.5 border-b border-rose-900/50 bg-rose-950/30 text-2xs font-mono text-rose-300" data-testid="watchlist-error">
          {priceError}
        </div>
      )}

      {/* Watchlist Items */}
      <div className="flex-1 overflow-y-auto relative">
        {search.trim().length > 0 && searchResults.length > 0 && (
          <div className="bg-zinc-900 border-b border-white/5 py-2">
            <div className="px-4 text-2xs font-bold text-zinc-500 uppercase tracking-wider mb-1">
              Add new asset
            </div>
            {searchResults.map((inst) => (
              <button
                type="button"
                key={inst.instrument_id}
                onClick={() => addTicker(inst.symbol, `${inst.base_currency} / ${inst.quote_currency}`)}
                className="w-full flex items-center justify-between px-4 py-2 cursor-pointer hover:bg-zinc-800 transition-colors text-left"
              >
                <div className="flex flex-col">
                  <span className="font-mono text-xs font-bold text-zinc-200">{inst.symbol}</span>
                  <span className="text-2xs text-zinc-500">{inst.base_currency} / {inst.quote_currency}</span>
                </div>
                <div className="text-cyan-500 text-xs font-bold" aria-hidden="true">+</div>
              </button>
            ))}
          </div>
        )}

        {filtered.map((item) => {
          const isSelected = selectedSymbol.toUpperCase().replace(".BINANCE", "") === item.symbol;
          const change = item.change24h;
          const isUp = change !== null && change >= 0;

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
              <button
                type="button"
                className="flex flex-col hover:bg-zinc-800/50 rounded px-1 -mx-1 transition-colors text-left"
                title={`Open ${item.symbol} profile`}
                onClick={(e) => {
                  e.stopPropagation();
                  window.dispatchEvent(new CustomEvent("cryptonite:open-profile", { detail: { symbol: item.symbol } }));
                }}
              >
                <span className="font-mono text-xs font-bold text-zinc-200">
                  {item.symbol}
                </span>
                <span className="text-2xs text-zinc-500 truncate max-w-[110px]">
                  {item.name}
                </span>
              </button>

              <div className="flex items-center gap-3">
                <button
                  type="button"
                  aria-label={`Remove ${item.symbol} from watchlist`}
                  onClick={(e) => {
                    e.stopPropagation();
                    setTickers(prev => prev.filter(t => t.symbol !== item.symbol));
                  }}
                  className="opacity-0 group-hover:opacity-100 focus:opacity-100 text-zinc-500 hover:text-red-400 text-xs px-1"
                >
                  ×
                </button>
                <div className="flex flex-col items-end">
                <span className="font-mono text-xs font-semibold text-zinc-100 tabular-nums" data-testid={`watchlist-price-${item.symbol}`}>
                  <Fresh
                    value={item.price}
                    asOf={item.asOf}
                    maxAgeMs={PRICE_MAX_AGE_MS}
                    format={(p) => `$${p.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
                  />
                </span>
                <div
                  className={`flex items-center space-x-0.5 text-2xs font-mono font-medium tabular-nums ${
                    change === null ? "text-zinc-500" : isUp ? "text-emerald-400" : "text-red-400"
                  }`}
                >
                  {change === null ? (
                    <span>{UNKNOWN_TEXT}</span>
                  ) : (
                    <>
                      {isUp ? <TrendingUp className="w-2.5 h-2.5" aria-hidden="true" /> : <TrendingDown className="w-2.5 h-2.5" aria-hidden="true" />}
                      <span>
                        {isUp ? "+" : ""}
                        {change.toFixed(2)}%
                      </span>
                    </>
                  )}
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
                    credentials: "include",
                    headers: {
                      "Content-Type": "application/json",
                      "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "",
                    },
                    body: JSON.stringify({ symbol: newSym }),
                  });

                  if (!res.ok) throw new Error("Failed to onboard");

                  addTicker(newSym, `${newSym} Market`);

                  // Re-fetch available instruments to update the local cache
                  fetch("/api/v1/instruments")
                    .then((res) => res.json())
                    .then((data) => {
                      if (Array.isArray(data)) setAvailableInstruments(data);
                    })
                    .catch(() => {});
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
