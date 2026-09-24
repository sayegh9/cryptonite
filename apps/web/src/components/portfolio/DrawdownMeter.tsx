"use client";

import React from "react";
import { Gauge, OctagonAlert, TriangleAlert } from "lucide-react";
import { DAILY_DRAWDOWN_LIMIT, usePortfolioStore } from "@/lib/stores/usePortfolioStore";
import { drawdownFrom, fmtCompactUsd, fmtPct, fmtUsd } from "@/lib/portfolio/metrics";

type Level = "calm" | "caution" | "danger" | "latched";

const COPY: Record<Level, { title: string; body: string }> = {
  calm: { title: "Within daily loss budget", body: "Drawdown is measured from today's equity peak." },
  caution: { title: "Half the daily loss budget is used", body: "Consider smaller sizes and fewer new entries for the rest of the session." },
  danger: { title: "Close to the daily loss latch", body: "New entries are blocked for the rest of the UTC day once the limit is hit. Cut risk now or pause strategies." },
  latched: { title: "Daily loss latch engaged", body: "New entries are blocked until the 00:00 UTC reset. Exits are still allowed." },
};

const TONE: Record<Level, { bar: string; text: string; box: string }> = {
  calm: { bar: "bg-emerald-500", text: "text-emerald-400", box: "border-zinc-800" },
  caution: { bar: "bg-amber-400", text: "text-amber-400", box: "border-amber-500/40" },
  danger: { bar: "bg-red-500", text: "text-red-400", box: "border-red-500/60 shadow-[0_0_0_1px_rgba(239,68,68,0.15),0_0_18px_-6px_rgba(239,68,68,0.5)]" },
  latched: { bar: "bg-red-600", text: "text-red-400", box: "border-red-600" },
};

/** Session drawdown vs. the risk engine's 5% daily hard limit, with graded risk-awareness cues. */
export function DrawdownMeter() {
  const equity = usePortfolioStore((s) => s.account?.total_equity ?? null);
  const latched = usePortfolioStore((s) => s.account?.daily_loss_latched ?? false);
  const hwm = usePortfolioStore((s) => s.equityHwm?.value ?? null);

  const dd = drawdownFrom(hwm, equity);
  const used = Math.min(dd / DAILY_DRAWDOWN_LIMIT, 1);
  const level: Level = latched ? "latched" : used >= 0.8 ? "danger" : used >= 0.5 ? "caution" : "calm";
  const tone = TONE[level];
  const buffer = hwm && equity !== null ? Math.max(0, equity - hwm * (1 - DAILY_DRAWDOWN_LIMIT)) : null;
  const Icon = level === "calm" ? Gauge : level === "caution" ? TriangleAlert : OctagonAlert;

  return (
    <section className={`p-4 bg-zinc-900/50 border rounded transition-colors duration-500 ${tone.box}`} aria-labelledby="drawdown-title">
      <div className="flex justify-between items-center mb-3">
        <h2 id="drawdown-title" className="flex items-center gap-2 text-zinc-400 text-xs font-bold uppercase tracking-wider">
          <Icon className={`w-4 h-4 ${tone.text}`} /> Session Drawdown
        </h2>
        <span className="text-[10px] text-zinc-500">Hard limit {fmtPct(DAILY_DRAWDOWN_LIMIT, 0)} from peak</span>
      </div>

      <div className="flex items-baseline justify-between mb-1.5 tabular-nums">
        <span className={`text-2xl font-black ${tone.text}`}>{fmtPct(latched ? Math.max(dd, DAILY_DRAWDOWN_LIMIT) : dd, 2)}</span>
        <span className="text-[10px] text-zinc-500">
          {latched ? "Budget exhausted" : buffer !== null ? `${fmtUsd(buffer)} to latch` : "Awaiting equity"}
        </span>
      </div>

      <div
        role="meter"
        aria-label="Daily drawdown used"
        aria-valuemin={0}
        aria-valuemax={DAILY_DRAWDOWN_LIMIT * 100}
        aria-valuenow={Number((Math.min(dd, DAILY_DRAWDOWN_LIMIT) * 100).toFixed(2))}
        aria-valuetext={`${fmtPct(dd, 2)} of ${fmtPct(DAILY_DRAWDOWN_LIMIT, 0)} daily limit`}
        className="relative h-2 rounded-full bg-zinc-800 overflow-hidden"
      >
        <div className={`h-full rounded-full ${tone.bar} transition-[width] duration-700`} style={{ width: `${(latched ? 1 : used) * 100}%` }} />
        <span className="absolute inset-y-0 left-1/2 w-px bg-zinc-950/80" aria-hidden />
        <span className="absolute inset-y-0 left-[80%] w-px bg-zinc-950/80" aria-hidden />
      </div>
      <div className="relative mt-1 h-3 text-[9px] text-zinc-600 tabular-nums" aria-hidden>
        <span className="absolute left-0">0%</span>
        <span className="absolute left-1/2 -translate-x-1/2">{fmtPct(DAILY_DRAWDOWN_LIMIT / 2)}</span>
        <span className="absolute left-[80%] -translate-x-1/2">{fmtPct(DAILY_DRAWDOWN_LIMIT * 0.8)}</span>
        <span className="absolute right-0">{fmtPct(DAILY_DRAWDOWN_LIMIT, 0)}</span>
      </div>

      <div role="status" aria-live="polite" className="mt-2">
        <div className={`text-[11px] font-bold ${level === "calm" ? "text-zinc-300" : tone.text}`}>{COPY[level].title}</div>
        <div className="text-[10px] text-zinc-500 leading-snug">
          {COPY[level].body}
          {hwm ? ` Peak ${fmtCompactUsd(hwm)}.` : ""}
        </div>
      </div>
    </section>
  );
}
