"use client";

import React from "react";
import { Search, ExternalLink, Globe, Cpu } from "lucide-react";
import { dispatchSwitchWorkspace } from "@/lib/navigation";
import { Button } from "@/components/ui/button";
import type { RegistryEntry } from "../controls/SettingCard";
import { SettingList } from "../controls/SettingList";

const CORE_SYMBOLS = [
  { symbol: "BTCUSDT", name: "Bitcoin", status: "Active Primary", tier: "L1" },
  { symbol: "ETHUSDT", name: "Ethereum", status: "Active Primary", tier: "L1" },
  { symbol: "SOLUSDT", name: "Solana", status: "Active Volatility", tier: "L2" },
  { symbol: "BNBUSDT", name: "BNB", status: "Active Core", tier: "L2" },
  { symbol: "DOGEUSDT", name: "Dogecoin", status: "Active Momentum", tier: "L3" },
  { symbol: "XRPUSDT", name: "XRP", status: "Active Momentum", tier: "L3" },
];

export interface ScannerUniverseSectionProps {
  entries: RegistryEntry[];
  onReload: () => Promise<void>;
  highlightedKey?: string;
}

export const ScannerUniverseSection: React.FC<ScannerUniverseSectionProps> = ({
  entries,
  onReload,
  highlightedKey,
}) => {
  const scannerEntry = entries.find((e) => e.key === "scanner.max_active_specs");
  const universeEntry = entries.find((e) => e.key === "universe.symbols");

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="border-b border-white/10 pb-4">
        <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
          <Search className="w-5 h-5 text-cyan-400" aria-hidden="true" />
          Scanner Parameters & Ingested Symbol Universe
        </h2>
        <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
          Vectorized Polars scanning concurrency limits, candle poll intervals, and certified exchange symbol streams.
        </p>
      </div>

      {/* Grid: Scanner Spec Concurrency & Scanner Studio Card */}
      <div className="grid grid-cols-1 gap-4 @4xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] @4xl:items-start">
        {scannerEntry && (
          <SettingList
            entries={[scannerEntry]}
            allEntries={entries}
            onSaved={() => void onReload()}
            highlightedKey={highlightedKey}
          />
        )}

        {/* Scanner Studio Quick Control Card */}
        <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-5 space-y-4 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between">
              <span className="font-mono text-xs text-cyan-300">SCANNER_PRESETS_RUNTIME</span>
              <span className="text-[10px] font-bold px-2 py-0.5 rounded border border-cyan-900/60 bg-cyan-950/40 text-cyan-300 uppercase">
                Interactive Studio
              </span>
            </div>
            <h3 className="text-sm font-semibold text-zinc-100 mt-2 flex items-center gap-2">
              <Cpu className="w-4 h-4 text-cyan-400" aria-hidden="true" />
              Scanner Studio Preset Management (ADR 0007 §12)
            </h3>
            <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
              Preset screening rules (Breakout Momentum, Mean Reversion, Funding Skew) activate and deactivate as concurrent specs in Scanner Studio without interrupting live engine loops.
            </p>
          </div>

          <div className="pt-3 border-t border-white/5">
            <Button
              type="button"
              onClick={() => dispatchSwitchWorkspace({ mode: "terminal", view: "scanner" })}
              className="w-full bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold text-xs gap-1.5"
            >
              <ExternalLink className="w-3.5 h-3.5" aria-hidden="true" />
              Open Scanner Studio
            </Button>
          </div>
        </div>
      </div>

      {/* Symbol Universe Card */}
      <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-5 space-y-5">
        <div>
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold uppercase tracking-wider text-white flex items-center gap-2">
              <Globe className="w-4 h-4 text-emerald-400" aria-hidden="true" />
              Active Symbol Universe (universe.symbols)
            </h3>
            <span className="text-[10px] font-mono text-zinc-400 uppercase tracking-wider border border-white/10 px-2 py-0.5 rounded">
              Binance USDT-M Futures
            </span>
          </div>
          <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
            Symbols ingested via Binance live WebSocket multiplexers. Symbol additions require formal asset onboarding (ADR 0007 §5) and liquidity parameter verification before trading permission is granted.
          </p>
        </div>

        {universeEntry && (
          <SettingList
            entries={[universeEntry]}
            allEntries={entries}
            onSaved={() => void onReload()}
            highlightedKey={highlightedKey}
          />
        )}

        {/* Visual Symbol Cards */}
        <div className="space-y-2">
          <span className="text-[11px] font-semibold text-zinc-300 uppercase tracking-wider block">
            Certified Core Instrument Portfolio
          </span>
          <div className="grid grid-cols-2 @xl:grid-cols-3 @4xl:grid-cols-6 gap-2.5">
            {CORE_SYMBOLS.map((s) => (
              <div
                key={s.symbol}
                className="rounded-lg border border-white/10 bg-zinc-900/50 p-2.5 text-center flex flex-col justify-between gap-1"
              >
                <div className="font-mono text-xs font-bold text-zinc-100">{s.symbol}</div>
                <div className="text-[10px] text-zinc-400">{s.name}</div>
                <div className="text-[9px] font-mono text-emerald-400 mt-1">{s.status}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};
