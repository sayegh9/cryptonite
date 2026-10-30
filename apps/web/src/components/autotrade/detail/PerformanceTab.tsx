"use client";

import { useEffect } from "react";
import { errorCopy, fmtUsd, signedUsd } from "@/lib/autotrade/copy";
import type { PerformanceRange, ProfilePerformance } from "@/lib/autotrade/types";
import { useAutoTradeStore } from "@/lib/stores/useAutoTradeStore";
import { btnSecondary } from "../Dialogs";

const RANGES: { id: PerformanceRange; label: string }[] = [
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
  { id: "all", label: "All time" },
];

/** The API's equity samples drawn as a line; the text summary beside it carries the same numbers. */
function EquityCurve({ curve }: { curve: ProfilePerformance["equity_curve"] }) {
  const pts = curve.filter((c) => c.equity != null).map((c) => ({ t: new Date(c.ts).getTime(), v: Number(c.equity) }));
  if (pts.length < 2) return <p className="text-xs text-zinc-400">Not enough equity samples to draw a curve.</p>;
  const [t0, t1] = [pts[0].t, pts[pts.length - 1].t];
  const lo = Math.min(...pts.map((p) => p.v));
  const hi = Math.max(...pts.map((p) => p.v));
  const W = 600;
  const H = 160;
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * W;
  const y = (v: number) => H - ((v - lo) / (hi - lo || 1)) * H;
  const first = pts[0].v;
  const last = pts[pts.length - 1].v;
  return (
    <figure className="space-y-1">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-40 w-full" role="img" aria-label={`Equity from ${fmtUsd(first)} to ${fmtUsd(last)}, low ${fmtUsd(lo)}, high ${fmtUsd(hi)}`}>
        <polyline fill="none" stroke="currentColor" strokeWidth="2" className="text-cyan-400" points={pts.map((p) => `${x(p.t)},${y(p.v)}`).join(" ")} />
      </svg>
      <figcaption className="text-xs text-zinc-400">
        Equity (USD, paper) {fmtUsd(first)} → {fmtUsd(last)} · low {fmtUsd(lo)} · high {fmtUsd(hi)}
      </figcaption>
    </figure>
  );
}

export function PerformanceTab({ id }: { id: string }) {
  const range = useAutoTradeStore((s) => s.performanceRange);
  const setRange = useAutoTradeStore((s) => s.setPerformanceRange);
  const perf = useAutoTradeStore((s) => s.performanceById[id]);
  const load = useAutoTradeStore((s) => s.loadPerformance);
  useEffect(() => {
    void load(id, range);
  }, [id, range, load]);

  const data = perf?.data && perf.data.range === range ? perf.data : null;
  return (
    <div className="space-y-4">
      <fieldset className="flex flex-wrap gap-2">
        <legend className="sr-only">Period</legend>
        {RANGES.map((r) => (
          <label key={r.id} className={`cursor-pointer rounded-full border px-3 py-1 text-xs ${range === r.id ? "border-cyan-500 text-cyan-200" : "border-zinc-700 text-zinc-300"}`}>
            <input type="radio" name="perf-range" className="sr-only" checked={range === r.id} onChange={() => setRange(r.id)} />
            {r.label}
          </label>
        ))}
      </fieldset>

      {!data && perf?.status !== "error" && <p role="status" className="text-sm text-zinc-400">Loading performance…</p>}
      {!data && perf?.status === "error" && (
        <div role="alert" className="space-y-2 text-sm">
          <p className="font-bold text-amber-200">Performance unavailable.</p>
          {perf.error && <p className="text-zinc-400">{errorCopy(perf.error)}</p>}
          <button type="button" className={btnSecondary} onClick={() => void load(id, range)}>
            Retry
          </button>
        </div>
      )}
      {data && <PerformanceBody data={data} />}
    </div>
  );
}

function PerformanceBody({ data }: { data: ProfilePerformance }) {
  const s = data.stats;
  const period = RANGES.find((r) => r.id === data.range)?.label ?? data.range;
  const interest = Object.entries(s.margin_interest_by_asset);
  const fees = Object.entries(s.fees.by_currency);
  return (
    <div className="space-y-4">
      <EquityCurve curve={data.equity_curve} />
      {data.equity_curve_sampled && <p className="text-2xs text-zinc-500">The curve is sampled across the whole period.</p>}
      <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <Stat label={`Avg R (${period})`} value={s.avg_r == null ? "Unavailable" : `${Number(s.avg_r) >= 0 ? "+" : ""}${Number(s.avg_r).toFixed(2)}R`}
          note={`Avg R over ${s.r_known} of ${s.audited_exit_count} audited exits${s.r_unknown ? ` (${s.r_unknown} without R)` : ""}`} />
        <Stat label={`Win rate (${period})`} value={s.audited_win_rate == null ? "Unavailable" : `${(Number(s.audited_win_rate) * 100).toFixed(1)}%`}
          note={`${s.audited_wins} wins in ${s.audited_exit_count} audited exits`} />
        <Stat label="Profile fills" value={String(s.outcome_coverage.profile_fill_count)} note="All fills tagged to this profile" />
        <Stat label="Max drawdown" value={s.max_drawdown_pct == null ? "Unavailable" : `${Number(s.max_drawdown_pct).toFixed(2)}%`} note="Paper equity" />
        <Stat label="Fees" value={fees.length ? fees.map(([c, v]) => `${v} ${c}`).join(" · ") : "None"} note={period} />
        <Stat label="Funding (USD)" value={s.funding == null ? "Unavailable" : signedUsd(s.funding)} note={`Perpetuals · ${period}`} />
        <Stat label="Margin interest" value={interest.length ? interest.map(([a, v]) => `${v} ${a}`).join(" · ") : "None"} note={`Spot Margin · ${period}`} />
      </dl>
      <p className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 text-xs text-zinc-300">
        Outcome metrics cover {s.outcome_coverage.scope.toLowerCase()}: {s.outcome_coverage.note}
      </p>
      {data.fills_truncated && <p className="text-2xs text-zinc-500">Showing the newest {data.fills.length} of {s.outcome_coverage.profile_fill_count} fills.</p>}
      {data.fills.length === 0 ? (
        <p className="text-sm text-zinc-400">No profile fills in this period.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-zinc-800">
          <table className="w-full min-w-[560px] text-left text-xs">
            <caption className="sr-only">Profile fills, {period}</caption>
            <thead className="bg-zinc-900 text-zinc-400">
              <tr>
                <th scope="col" className="px-2 py-1.5">Time</th>
                <th scope="col" className="px-2 py-1.5">Instrument</th>
                <th scope="col" className="px-2 py-1.5">Side</th>
                <th scope="col" className="px-2 py-1.5 text-right">Quantity</th>
                <th scope="col" className="px-2 py-1.5 text-right">Price</th>
                <th scope="col" className="px-2 py-1.5 text-right">Fee</th>
              </tr>
            </thead>
            <tbody>
              {data.fills.map((f) => (
                <tr key={f.trade_id} className="border-t border-zinc-800">
                  <td className="px-2 py-1.5 font-mono text-zinc-400">{f.executed_at ? new Date(f.executed_at).toLocaleString() : "—"}</td>
                  <td className="px-2 py-1.5 text-zinc-200">{f.instrument_id}</td>
                  <td className="px-2 py-1.5 text-zinc-200">{f.side}</td>
                  <td className="px-2 py-1.5 text-right text-zinc-200">{f.quantity ?? "—"}</td>
                  <td className="px-2 py-1.5 text-right text-zinc-200">{f.price ?? "—"}</td>
                  <td className="px-2 py-1.5 text-right text-zinc-400">{f.fee == null ? "—" : `${f.fee} ${f.fee_currency}`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="rounded-lg border border-zinc-800 p-3">
      <dt className="text-zinc-500">{label}</dt>
      <dd className="mt-0.5 text-sm font-bold text-zinc-100">{value}</dd>
      <dd className="text-2xs text-zinc-500">{note}</dd>
    </div>
  );
}
