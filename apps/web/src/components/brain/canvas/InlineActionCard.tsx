"use client";

import React, { useState } from "react";
import { BrainCircuit, Sliders, Sparkles, Zap } from "lucide-react";
import { toast } from "sonner";
import { usePaperEquity, EQUITY_MAX_AGE_MS } from "@/lib/portfolio/usePaperEquity";
import { Fresh } from "@/components/Fresh";

/** Symbol named by a manifest JSON (instrument_id / symbol), else null. */
function manifestSymbol(codeStr: string): string | null {
  try {
    const j = JSON.parse(codeStr);
    const raw = j?.instrument_id ?? j?.symbol;
    return typeof raw === "string" && raw ? raw.split(".")[0].toUpperCase() : null;
  } catch {
    const m = codeStr.match(/"(?:instrument_id|symbol)"\s*:\s*"([A-Z0-9]+)/i);
    return m ? m[1].toUpperCase() : null;
  }
}

export function InlineActionCard({ codeStr, onTriggerDebate, fallbackSymbol }: { codeStr: string; onTriggerDebate?: (symbol: string) => void; fallbackSymbol: string }) {
  // Allocation in % of the paper account's equity; USD only when that equity is known
  const [pct, setPct] = useState<number>(5);
  const equity = usePaperEquity();
  const symbol = manifestSymbol(codeStr) ?? fallbackSymbol;

  return (
    <div className="my-4 p-4 rounded-xl border border-cyan-500/30 bg-cyan-950/20 font-sans shadow-lg">
      <div className="flex items-center justify-between mb-3 border-b border-cyan-800/50 pb-2">
        <div className="font-bold text-cyan-300 flex items-center gap-2">
          <Sparkles className="w-4 h-4" /> Strategy Manifest
        </div>
      </div>
      <pre className="text-2xs text-zinc-300 bg-zinc-900/80 p-3 rounded-lg overflow-x-auto mb-4 border border-zinc-800">
        <code>{codeStr}</code>
      </pre>

      {/* Sizing preview: % of paper equity, disabled while equity is unknown */}
      <div className="p-3 bg-zinc-950/50 border border-zinc-800/60 rounded-lg space-y-2 mb-4 font-mono text-2xs" data-testid="manifest-sizing">
        <div className="flex items-center justify-between text-zinc-400">
          <span className="flex items-center space-x-1">
            <Sliders className="w-3 h-3 text-cyan-400" aria-hidden="true" />
            <span>Allocation: {pct}% of equity (1–25%)</span>
          </span>
          <span className="text-cyan-300 font-bold tabular-nums">
            <Fresh
              value={equity.usable ? equity.equity : null}
              asOf={equity.asOf}
              maxAgeMs={EQUITY_MAX_AGE_MS}
              format={(e) => `≈ $${((e * pct) / 100).toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
              unknownText="equity unavailable"
            />
          </span>
        </div>
        <input
          type="range"
          min={1}
          max={25}
          step={1}
          value={pct}
          disabled={!equity.usable}
          aria-label="Allocation in percent of equity"
          onChange={(e) => setPct(Number(e.target.value))}
          className="w-full accent-cyan-400 h-1.5 bg-zinc-800 rounded-full cursor-pointer appearance-none disabled:cursor-not-allowed disabled:opacity-40"
        />
      </div>

      <div className="flex flex-wrap gap-2 mt-2">
        <button onClick={() => {
          window.dispatchEvent(new CustomEvent("cryptonite:load-strategy", {
            detail: { name: "Generated", code: codeStr }
          }));
          toast.success("Strategy pushed to Research Lab");
        }} className="px-3 py-1.5 bg-blue-600/20 hover:bg-blue-600/40 text-blue-300 border border-blue-500/50 rounded flex items-center gap-1.5 text-xs font-semibold transition-colors">
          <BrainCircuit className="w-3.5 h-3.5" /> Backtest in Lab
        </button>
        {onTriggerDebate && (
          <button onClick={() => onTriggerDebate(symbol)} className="px-3 py-1.5 bg-purple-600/20 hover:bg-purple-600/40 text-purple-300 border border-purple-500/50 rounded flex items-center gap-1.5 text-xs font-semibold transition-colors">
            <Zap className="w-3.5 h-3.5" /> Trigger {symbol} Debate
          </button>
        )}
      </div>
    </div>
  );
}
