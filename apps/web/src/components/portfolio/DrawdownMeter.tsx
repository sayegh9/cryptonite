"use client";

import React from "react";
import { Gauge, OctagonAlert, TriangleAlert } from "lucide-react";
import { usePortfolioStore } from "@/lib/stores/usePortfolioStore";
import { drawdownFrom, fmtCompactUsd, fmtPct, fmtUsd } from "@/lib/portfolio/metrics";
import { pctFraction, useEnforcedLimits } from "@/lib/limits";

type Level = "calm" | "caution" | "danger" | "latched" | "unknown";

const COPY: Record<Level, { title: string; body: string }> = {
  calm: { title: "Within daily loss budget", body: "Drawdown is measured from today's equity peak." },
  caution: { title: "Half the daily loss budget is used", body: "Consider smaller sizes and fewer new entries for the rest of the session." },
  danger: { title: "Close to the daily loss latch", body: "New entries are blocked for the rest of the UTC day once the limit is hit. Cut risk now or pause strategies." },
  latched: { title: "Daily loss latch engaged", body: "New entries are blocked until the 00:00 UTC reset. Exits are still allowed." },
  unknown: { title: "Daily loss limit unknown", body: "The enforced limit is not available (orchestrator not published or API unreachable), so the budget used cannot be shown." },
};

const TONE: Record<Level, { bar: string; text: string; box: string }> = {
  calm: { bar: "bg-emerald-500", text: "text-emerald-400", box: "border-zinc-800" },
  caution: { bar: "bg-amber-400", text: "text-amber-400", box: "border-amber-500/40" },
  danger: { bar: "bg-red-500", text: "text-red-400", box: "border-red-500/60 shadow-[0_0_0_1px_rgba(239,68,68,0.15),0_0_18px_-6px_rgba(239,68,68,0.5)]" },
  latched: { bar: "bg-red-600", text: "text-red-400", box: "border-red-600" },
  unknown: { bar: "bg-zinc-600", text: "text-zinc-300", box: "border-zinc-800" },
};

/**
 * Session drawdown vs. the daily loss limit the paper engine actually enforces
 * (GET /control/enforced-limits). Without that limit the budget is shown as unknown.
 */
export function DrawdownMeter() {
  const equity = usePortfolioStore((s) => s.account?.total_equity ?? null);
  const latched = usePortfolioStore((s) => s.account?.daily_loss_latched ?? false);
  const hwm = usePortfolioStore((s) => s.equityHwm?.value ?? null);
  const limits = useEnforcedLimits();
  const limit = limits.status === "ok" ? pctFraction(limits.data.paper?.max_daily_loss_pct) : null;

  const dd = drawdownFrom(hwm, equity);
  const ddKnown = hwm !== null && equity !== null;
  const used = limit !== null ? Math.min(dd / limit, 1) : null;
  const level: Level = latched ? "latched" : used === null ? "unknown" : used >= 0.8 ? "danger" : used >= 0.5 ? "caution" : "calm";
  const tone = TONE[level];
  const buffer = limit !== null && hwm && equity !== null ? Math.max(0, equity - hwm * (1 - limit)) : null;
  const Icon = level === "calm" || level === "unknown" ? Gauge : level === "caution" ? TriangleAlert : OctagonAlert;

  return (
    <section className={`p-4 bg-zinc-900/50 border rounded transition-colors duration-500 ${tone.box}`} aria-labelledby="drawdown-title">
      <div className="flex justify-between items-center mb-3">
        <h2 id="drawdown-title" className="flex items-center gap-2 text-zinc-400 text-xs font-bold uppercase tracking-wider">
          <Icon className={`w-4 h-4 ${tone.text}`} /> Session Drawdown
        </h2>
        <span className="text-2xs text-zinc-500" data-testid="drawdown-limit">
          {limit !== null ? `Hard limit ${fmtPct(limit, 0)} from peak` : "Hard limit —"}
        </span>
      </div>

      <div className="flex items-baseline justify-between mb-1.5 tabular-nums">
        <span className={`text-2xl font-black ${tone.text}`}>{ddKnown ? fmtPct(latched && limit !== null ? Math.max(dd, limit) : dd, 2) : "—"}</span>
        <span className="text-2xs text-zinc-500">
          {latched ? "Budget exhausted" : buffer !== null ? `${fmtUsd(buffer)} to latch` : limit === null ? "Distance to latch —" : "Awaiting equity"}
        </span>
      </div>

      <div
        role={limit !== null ? "meter" : "img"}
        aria-label={limit !== null ? "Daily drawdown used" : `Daily drawdown ${fmtPct(dd, 2)}; daily limit unknown`}
        aria-valuemin={limit !== null ? 0 : undefined}
        aria-valuemax={limit !== null ? limit * 100 : undefined}
        aria-valuenow={limit !== null ? Number((Math.min(dd, limit) * 100).toFixed(2)) : undefined}
        aria-valuetext={limit !== null ? `${fmtPct(dd, 2)} of ${fmtPct(limit, 0)} daily limit` : undefined}
        className="relative h-2 rounded-full bg-zinc-800 overflow-hidden"
      >
        <div className={`h-full rounded-full ${tone.bar} transition-[width] duration-700`} style={{ width: `${(latched ? 1 : used ?? 0) * 100}%` }} />
        {limit !== null && <span className="absolute inset-y-0 left-1/2 w-px bg-zinc-950/80" aria-hidden />}
        {limit !== null && <span className="absolute inset-y-0 left-[80%] w-px bg-zinc-950/80" aria-hidden />}
      </div>
      {limit !== null && (
        <div className="relative mt-1 h-3 text-2xs text-zinc-500 tabular-nums" aria-hidden>
          <span className="absolute left-0">0%</span>
          <span className="absolute left-1/2 -translate-x-1/2">{fmtPct(limit / 2)}</span>
          <span className="absolute left-[80%] -translate-x-1/2">{fmtPct(limit * 0.8)}</span>
          <span className="absolute right-0">{fmtPct(limit, 0)}</span>
        </div>
      )}

      <div role="status" aria-live="polite" className="mt-2">
        <div className={`text-2xs font-bold ${level === "calm" ? "text-zinc-300" : tone.text}`}>{COPY[level].title}</div>
        <div className="text-2xs text-zinc-500 leading-snug">
          {COPY[level].body}
          {hwm ? ` Peak ${fmtCompactUsd(hwm)}.` : ""}
        </div>
      </div>
    </section>
  );
}
