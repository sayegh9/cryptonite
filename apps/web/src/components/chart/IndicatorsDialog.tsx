"use client";

import React, { useState } from "react";
import { Plus, Settings2, X } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { DEFAULT_COLORS, INDICATOR_OPTIONS, paneFor, type ChartIndicator, type IndicatorType } from "./indicators";

interface Props {
  indicators: ChartIndicator[];
  onAdd: (ind: ChartIndicator) => void;
  onRemove: (id: string) => void;
}

/** The chart's indicator picker: add a type with a period, remove active ones. */
export function IndicatorsDialog({ indicators, onAdd, onRemove }: Props) {
  const [open, setOpen] = useState(false);
  const [type, setType] = useState<IndicatorType>("SMA");
  const [period, setPeriod] = useState(14);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger className="flex items-center space-x-1 px-2 py-0.5 text-2xs font-medium rounded text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60 transition-colors border border-transparent hover:border-zinc-700">
        <Settings2 className="w-3 h-3" aria-hidden="true" />
        <span>Indicators</span>
      </DialogTrigger>
      <DialogContent className="bg-zinc-950 border border-zinc-800 text-zinc-200 max-w-sm">
        <DialogHeader>
          <DialogTitle>Chart Indicators</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 pt-2">
          <div className="flex items-center space-x-2">
            <select
              aria-label="Select indicator type"
              className="bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1.5 text-sm text-zinc-100 focus-visible:ring-2 focus-visible:ring-cyan-500/50 focus:outline-none transition-all"
              value={type}
              onChange={(e) => setType(e.target.value as IndicatorType)}
            >
              {INDICATOR_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
            <input
              aria-label="Indicator period"
              type="number"
              min="2"
              max="200"
              className="bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1.5 text-sm text-zinc-100 w-20 focus-visible:ring-2 focus-visible:ring-cyan-500/50 focus:outline-none font-mono tabular-nums transition-all"
              value={period}
              onChange={(e) => setPeriod(Number(e.target.value))}
            />
            <Button
              size="sm"
              variant="secondary"
              className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 border-0"
              onClick={() =>
                onAdd({
                  id: crypto.randomUUID(),
                  type,
                  period,
                  pane: paneFor(type),
                  color: DEFAULT_COLORS[indicators.length % DEFAULT_COLORS.length],
                })
              }
            >
              <Plus className="w-4 h-4 mr-1" aria-hidden="true" /> Add
            </Button>
          </div>

          <div className="space-y-2 mt-4">
            {indicators.map((ind) => (
              <div key={ind.id} className="flex items-center justify-between bg-zinc-900/50 border border-zinc-800/50 rounded px-2 py-1.5">
                <div className="flex items-center space-x-2">
                  <div className="w-3 h-3 rounded-full" style={{ backgroundColor: ind.color }} />
                  <span className="text-xs font-mono">{ind.type} ({ind.period})</span>
                </div>
                <button
                  type="button"
                  onClick={() => onRemove(ind.id)}
                  aria-label={`Remove ${ind.type} (${ind.period})`}
                  className="text-zinc-500 hover:text-red-400"
                >
                  <X className="w-3.5 h-3.5" aria-hidden="true" />
                </button>
              </div>
            ))}
            {indicators.length === 0 && <div className="text-xs text-zinc-500 text-center py-2">No active indicators</div>}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
