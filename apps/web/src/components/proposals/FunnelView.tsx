"use client";

import { useCallback, useEffect, useState } from "react";
import { useVisiblePoll } from "@/components/autotrade/AutoTradeWorkspace";
import { fetchFunnel } from "@/lib/proposals/client";
import { countsLine, reasonText, STAGE_TEXT, whatIfText, type Funnel, type FunnelRow } from "@/lib/proposals/funnel";

const POLL_MS = 60_000;

const price = (v: number | null) => (v === null ? "—" : v.toLocaleString("en-US", { maximumFractionDigits: 8 }));
const when = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });

function Row({ row }: { row: FunnelRow }) {
  const unaccounted = row.stage === "UNACCOUNTED";
  const reason = reasonText(row.reason);
  return (
    <tr
      data-testid="funnel-row"
      data-stage={row.stage}
      className={`border-t align-top ${unaccounted ? "border-amber-500/60 bg-amber-950/60" : "border-zinc-800"}`}
    >
      <td className="whitespace-nowrap py-2 pr-2 text-zinc-300">{when(row.time)}</td>
      <td className="min-w-0 py-2 pr-2">
        <div className="font-semibold text-zinc-100">
          {row.coin} {row.direction === "SHORT" ? "Short" : row.direction === "LONG" ? "Long" : ""} · {row.timeframe}
        </div>
        {row.preset && <div className="break-all text-zinc-400">{row.preset}</div>}
        <div className="text-zinc-400">
          {price(row.entry)} / {price(row.stop)} / {price(row.target)}
        </div>
        <div className="text-zinc-400">
          A {row.policy_a ?? "—"} · B {row.policy_b ?? "—"} · C {row.policy_c ?? "—"}
        </div>
      </td>
      <td className="min-w-0 py-2 pr-2">
        <div className={unaccounted ? "font-semibold text-amber-200" : "text-zinc-100"}>{STAGE_TEXT[row.stage] ?? row.stage}</div>
        {reason && <div className="break-words text-zinc-300">{reason}</div>}
      </td>
      <td className="py-2 text-zinc-100" data-testid="funnel-what-if">{whatIfText(row)}</td>
    </tr>
  );
}

export function FunnelView() {
  const [funnel, setFunnel] = useState<Funnel | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setFunnel(await fetchFunnel());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    }
  }, []);

  useVisiblePoll(() => void load(), POLL_MS);
  useEffect(() => {
    void load();
  }, [load]);

  if (error && !funnel) return <p className="text-xs text-amber-300" data-testid="funnel-error">Could not load what happened to setups: {error}</p>;
  if (!funnel) return <p className="text-xs text-zinc-400">Loading…</p>;
  return (
    <div className="space-y-2" data-testid="funnel-view">
      <p className="break-words text-xs text-zinc-200" data-testid="funnel-counts">{countsLine(funnel.counts, funnel.hours)}</p>
      <ul className="list-disc space-y-0.5 pl-4 text-2xs text-zinc-300" data-testid="funnel-caveats">
        {funnel.caveats.map((c) => <li key={c}>{c}</li>)}
      </ul>
      {error && <p className="text-xs text-amber-300">Showing the last figures: {error}</p>}
      {funnel.rows.length === 0 ? (
        <p className="text-xs text-zinc-400">The scanner found no setups in this period.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-2xs">
            <caption className="sr-only">What happened to each setup, newest first</caption>
            <thead className="text-zinc-400">
              <tr>
                <th scope="col" className="py-1 pr-2 font-medium">UTC</th>
                <th scope="col" className="py-1 pr-2 font-medium">Setup (entry / stop / target)</th>
                <th scope="col" className="py-1 pr-2 font-medium">What happened</th>
                <th scope="col" className="py-1 font-medium">What-if</th>
              </tr>
            </thead>
            <tbody>{funnel.rows.map((r) => <Row key={r.candidate_id} row={r} />)}</tbody>
          </table>
        </div>
      )}
      {funnel.rows_truncated && <p className="text-2xs text-zinc-400">Only the newest {funnel.rows.length} setups are listed; the counts cover all of them.</p>}
    </div>
  );
}
